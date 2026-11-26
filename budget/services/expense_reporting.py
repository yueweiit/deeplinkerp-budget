"""Permission-scoped SQL queries over the last complete reporting publication."""
from calendar import monthrange
from collections import defaultdict
from decimal import Decimal
import json

import frappe
from frappe import _

from . import expense_management as service
from . import expense_report_store as store
from .expense_report_refresh import read_context


def prepare(values):
	f = service.filters(values)
	context, rules, metadata = read_context()
	if f["company"] and f["company"] not in context["companies"]:
		frappe.throw(_("You do not have permission to view this subsidiary."), frappe.PermissionError)
	context = {**context, "company_regions": service.regions.company_regions(context)}
	metadata = {**metadata, "company_regions": context["company_regions"]}
	condition, params = store.where(context, f, categories=False)
	condition += " and " + store.period(f, undated=f["view"] == "review")
	if f["view"] not in ("review", "budget"):
		condition += " and kind!='budget'"
	options = frappe.db.sql("select distinct source_category_key, kind, source_path_json from `tabExpense Report Line` where "
		+ condition, {**params, "month": f["month"]}, as_dict=True)
	# The shared "missing" category can occur in multiple approval kinds.
	# Keep one option, as in the existing snapshot projection.
	options = {row.source_category_key: {"key": row.source_category_key, "kind": row.kind,
		"source_path": json.loads(row.source_path_json)} for row in options}
	metadata["source_categories"] = sorted(options.values(), key=lambda row: (row["source_path"], row["kind"]))
	condition, params = store.where(context, f)
	return f, context, rules, metadata, condition, {**params, "month": f["month"]}


def comparison(condition, params, f, metadata, undated, *, currency=None):
	from deeplinkerp_branding.services.mexico_expense_insights import comparison_period
	month, previous = f["month"], service.management.shift_month(f["month"], -1)
	last_day = monthrange(int(month[:4]), int(month[5:]))[1]
	window = comparison_period(month + "-01", f"{month}-{last_day:02}", frappe.utils.today())
	if not window:
		return {"percent": None, "window": None}
	summaries = [store.summaries(condition + " and reporting_month=%(month)s and application_date<=%(end)s",
		{**params, "month": period, "end": window[end]}, review_currency=currency)[0]
		for period, end in ((month, "to_date"), (previous, "previous_to"))]
	current, prior = summaries
	currency = currency or "CNY"
	known = metadata["complete"] and not undated and all(row["count"] and not row["review"]
		and row["amounts"][currency] is not None for row in summaries)
	baseline = Decimal(prior["amounts"][currency] or 0)
	percent = str(((Decimal(current["amounts"][currency]) / baseline - 1) * 100).quantize(Decimal("0.1"))) if known and baseline > 0 else None
	return {"percent": percent, "window": window}


def query(values=None, start=0):
	f, context, rules, metadata, condition, params = prepare(values)
	current = condition + " and " + store.period(f)
	undated = store.count(condition + " and reporting_month='' and approval_status in ('approved','pending','unknown')", params)
	response = {"filters": f, "companies": sorted(context["companies"]),
		"categories": service.management.rules_service.CATEGORIES, "source_categories": metadata["source_categories"],
		"issues": service.ISSUES, "metadata": metadata, "summary": store.summaries(current, params)[0], "undated": undated}
	names = service.regions.company_names(metadata["company_regions"], f)
	groups = {row["company"]: row for row in store.summaries(current, params, group="company")}
	response["company_rows"] = [groups.get(name) or {"company": name, **service.summarize([])} for name in names]
	response["company_rows"].sort(key=lambda row: (row["amounts"]["CNY"] is None, -Decimal(row["amounts"]["CNY"] or 0), row["company"]))
	response["unassigned"] = groups.get("", {}).get("review", 0)
	if f["view"] == "budget":
		# Reuse the financial completeness/version rules on this month's small
		# authorized set. No raw approval parsing or exchange-rate IO occurs.
		budget_groups = defaultdict(list)
		for row in store.select(current, params):
			budget_groups[row["company"]].append(row)
		response["budgets"] = [{"company": name, "metrics": service.budget_metrics(budget_groups[name],
			rules["warning_percent"], category_scope=f["category"], source_scope=f["source_category"])} for name in names]
		if not metadata["complete"]:
			for company in response["budgets"]:
				for metrics in company["metrics"].values():
					metrics.update(balance=None, forecast_balance=None, execution_rate=None)
					metrics["flags"] = sorted(set(metrics["flags"]) | {"incomplete"})
	if f["view"] in ("overview", "analysis"):
		spending = current + " and kind!='budget'"
		categories = store.summaries(spending, params, group="management_category")
		for row in categories:
			row["category"] = row.pop("management_category") or "unclassified"
		categories.sort(key=lambda row: -Decimal(row["amounts"]["CNY"] or 0))
		response["category_rows"] = categories
		options = {row["key"]: row for row in metadata["source_categories"]}
		sources = store.summaries(spending, params, group="source_category_key")
		for row in sources:
			key = row.pop("source_category_key")
			option = options[key]
			row.update(category=key, kind=option["kind"], source_path=option["source_path"])
		sources.sort(key=lambda row: -Decimal(row["amounts"]["CNY"] or 0))
		response["source_category_rows"] = sources
		months = [month for delta in range(5, -1, -1) if (month := service.management.shift_month(f["month"], -delta)) >= "1900-01"]
		trend = {row["reporting_month"]: row for row in store.summaries(condition + " and reporting_month in %(months)s",
			{**params, "months": months}, group="reporting_month")}
		response["trend"] = []
		for month in months:
			row = trend.get(month) or service.summarize([])
			row.pop("reporting_month", None)
			response["trend"].append({"month": month, **row})
		response["comparison"] = comparison(condition, params, f, metadata, bool(undated))
	if f["view"] in ("details", "review"):
		start = service.pagination(start)
		selection = condition + " and " + store.period(f, undated=f["view"] == "review") + " and " + store.detail_condition(f)
		total = store.count(selection, params)
		response.update(total=total, start=start, has_more=start + 50 < total,
			rows=[service.public(row) for row in store.select(selection, params, start=start, limit=50)])
	response["regions"] = []
	for region in service.regions.descriptors(metadata["company_regions"], f):
		regional_params = {**params, "regional_companies": region.pop("companies")}
		regional_condition = condition + " and company in %(regional_companies)s"
		regional_current = regional_condition + " and " + store.period(f)
		region["summary"] = store.summaries(regional_current, regional_params)[0]
		if f["view"] in ("overview", "analysis"):
			sources = store.summaries(regional_current + " and kind!='budget'", regional_params, group="source_category_key")
			for row in sources:
				key = row.pop("source_category_key")
				option = options[key]
				row.update(category=key, kind=option["kind"], source_path=option["source_path"])
			sources.sort(key=lambda row: -Decimal(row["amounts"][region["currency"]] or 0))
			region["source_category_rows"] = sources
			trend = {row.pop("reporting_month"): row for row in store.summaries(
				regional_condition + " and reporting_month in %(months)s", {**regional_params, "months": months}, group="reporting_month")}
			region["trend"] = [{"month": month, **(trend.get(month) or service.summarize([]))} for month in months]
			regional_undated = store.count(regional_condition + " and reporting_month='' and approval_status in ('approved','pending','unknown')", regional_params)
			region["comparison"] = comparison(regional_condition, regional_params, f, metadata,
				bool(regional_undated), currency=region["currency"])
		response["regions"].append(region)
	return service.regions.annotate_response(response, metadata["company_regions"])


def detail(key, values=None):
	from .expense_approval_links import original_url
	f, _context, _rules, metadata, condition, params = prepare(values)
	condition += " and name=%(key)s and " + store.period(f, undated=f["view"] == "review")
	if f["view"] == "details" and f["measure"] == "approved":
		condition += " and " + store.detail_condition(f)
	rows = store.select(condition, {**params, "key": key}, limit=1)
	if not rows:
		frappe.throw(_("Expense detail is unavailable or outside your permitted scope."), frappe.PermissionError)
	return {"row": service.regions.annotate(service.public(rows[0]), metadata["company_regions"]),
		"metadata": metadata, "approval_url": original_url(rows[0], _context, _rules)}


def export_rows(values=None):
	f, _context, _rules, metadata, condition, params = prepare(values)
	if not metadata["has_data"]:
		frappe.throw(_("No local approval snapshot is available yet. Wait for synchronization before exporting."))
	if f["view"] in ("overview", "analysis"):
		f = {**f, "measure": "approved"}
	condition += " and " + store.period(f, undated=f["view"] == "review") + " and " + store.detail_condition(f)
	rows = store.select(condition, params, limit=20001)
	if len(rows) > 20000:
		frappe.throw(_("Expense export exceeds 20000 rows. Narrow the subsidiary or category."))
	return [service.regions.annotate(service.public(row), metadata["company_regions"]) for row in rows], f, metadata
