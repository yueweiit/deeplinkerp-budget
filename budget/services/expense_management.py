"""Unified, company-authorized management reporting over local approval facts.

Application month is the expense period; budget month keeps its existing meaning.
The ownership registry wins over native CN snapshots, so an approval read by both
feeds or split across subsidiaries is counted once. No payment facts are inferred.
"""
from collections import defaultdict
from calendar import monthrange
from decimal import Decimal
from functools import lru_cache
import hashlib
import json
import re

import frappe
from frappe import _

from deeplinkerp_branding.services import mexico_expense_fx as fx
from deeplinkerp_branding.services import regional_expenses as regional
from . import cn_approval_management as management
from . import cn_approval_settings as settings
from . import cn_approval_workbench as approvals
from .expense_sync import cn_revision

CURRENCIES = ("CNY", "MXN")
BLOCKING = {"amount", "amount_pending", "total", "currency", "structure", "identity",
	"allocation", "regional_allocation", "organization", "period", "detail_amount", "detail_mismatch", "detail_currency"}
ISSUES = {**approvals.ISSUES, **management.ISSUES, "amount_pending": "Amount is not finalized",
	"total": "Detail total differs from the application amount", "identity": "Expense line identity needs review"}
ISSUES.pop("management_category", None)
VIEWS = {"overview", "analysis", "budget", "details", "review"}
PUBLIC = ("key", "company", "number", "application_date", "month", "kind", "status",
	"management_category", "source_path", "source_category_key", "description", "line_label", "amount", "currency", "issues",
	"amounts", "rates", "rate_date", "eligible", "budget_state", "date_basis")


def filters(values=None):
	values = frappe.parse_json(values) if isinstance(values, str) else {} if values is None else values
	if not isinstance(values, dict) or set(values) - {"month", "company", "category", "source_category", "view", "measure"}:
		frappe.throw(_("Invalid expense management filters."))
	result = {key: str(values.get(key) or "").strip() for key in ("month", "company", "category", "source_category", "view", "measure")}
	result["month"] = result["month"] or frappe.utils.today()[:7]
	result["view"] = result["view"] or "overview"
	if (not re.fullmatch(r"\d{4}-(0[1-9]|1[0-2])", result["month"])
			or not "1900-01" <= result["month"] <= frappe.utils.today()[:7]
			or result["view"] not in VIEWS
			or result["measure"] not in ("", "approved")
			or len(result["company"]) > 140
			or result["source_category"] and not re.fullmatch(r"missing|[0-9a-f]{64}", result["source_category"])
			or result["category"] and result["category"] not in management.rules_service.CATEGORIES | {"unclassified": ""}):
		frappe.throw(_("Invalid expense management filters."))
	return result


def load():
	approvals.require_reader()
	from ..integrations.cn_approval_history import read_snapshot
	from deeplinkerp_branding.services import regional_expense_automatic as automatic, mexico_expenses as mx
	if automatic.ensure_rebuild():
		frappe.throw(_("Expense ownership is being updated automatically. Please wait."))
	if not regional.installed():
		frappe.throw(_("Expense allocation storage is not installed. Run the application migration."))
	context = approvals.access_context()
	snapshot, rules = read_snapshot(), settings.management_rules()
	identities, allocated = regional.management_projection(context, include_legacy=False,
		submission_fallback=True, management_rules=rules)
	legacy_ids = automatic.legacy_manual_identities()
	native = [row for row in snapshot["rows"] if row["kind"] == "budget"
		or (row["corp_id"], row["process_instance_id"]) not in identities | legacy_ids]
	rows = management.analytical_lines(native, context, rules) + allocated
	mx_settings = mx._settings()
	templates = mx._templates(mx_settings)
	if legacy_ids - identities:
		rows += legacy_projection(legacy_ids - identities, context, rules, templates)
	if len(rows) > 50000:
		frappe.throw(_("Expense analysis exceeds 50000 detail lines. Narrow the configured scope."))
	management.annotate_budget_completeness(rows)
	mx_complete = bool(templates) and all(item.get("enabled") and item.get("last_sync_at")
		and not any(item.get(key) for key in ("last_error", "cursor", "sync_until")) for item in templates)
	sync = snapshot.get("sync") or {}
	complete = bool(snapshot.get("has_snapshot")) and not snapshot.get("missing_history_templates") \
		and sync.get("status") not in ("queued", "running", "failed") and mx_complete
	metadata = {"complete": complete, "has_data": bool(snapshot.get("has_snapshot") or rows),
		"has_snapshot": bool(snapshot.get("has_snapshot")),
		"updated_at": {"cn": snapshot.get("fetched_at"),
			"mx": str(min((item.get("last_sync_at") for item in templates if item.get("last_sync_at")), default=""))},
		"missing_history": snapshot.get("missing_history_templates", 0),
		"sync_revision": {"cn": cn_revision(sync), "mx": mx.sync_revision(mx_settings)}}
	return [normalize(row) for row in rows], context, rules, metadata


def legacy_projection(legacy_ids, context, rules, templates=()):
	"""Honor existing MX manual decisions without rewriting their source data."""
	from deeplinkerp_branding.services import mexico_expenses as mx
	if not legacy_ids:
		return []
	# A manual exception keeps the entire approval outside the shared registry.
	# Use the MX parser's stable source identity, then retain every authorized
	# active sibling, including mapped lines and lines in another subsidiary.
	source_names = sorted(regional.digest([corp, instance]) for corp, instance in legacy_ids)
	lines = frappe.get_list(mx.LINE, filters={"is_active": 1, "source": ["in", source_names]},
		fields=["name", "source", "source_version", "company", "regional_company", "approval_no", "approval_status",
			"amount", "currency", "description", "approval_type", "review_state", "issues", "classification_path"],
		limit_page_length=50001)
	if len(lines) > 50000:
		frappe.throw(_("Expense analysis exceeds 50000 detail lines. Narrow the configured scope."))
	# Only explicit authorized companies are reportable. Unassigned legacy
	# exceptions remain in the existing MX review workbench.
	lines = [line for line in lines if line.company in context["companies"]
		and (not line.regional_company or line.regional_company in context["companies"])]
	if not lines:
		return []
	sources = {row.name: row for row in frappe.get_all(mx.SOURCE, filters={"name": ["in", sorted({line.source for line in lines})]},
		fields=["name", "corp_id", "process_code", "process_instance_id", "raw_json", "source_version", "request_date"], limit_page_length=50001)}
	by_template = {(row.get("corp_id"), row.get("process_code")): row for row in templates}
	dates = {name: regional.application_date_info(regional.decode(source.raw_json),
		by_template.get((source.corp_id, source.process_code))) for name, source in sources.items()
		if (source.corp_id, source.process_instance_id) in legacy_ids}
	output = []
	for line in lines:
		if line.source not in dates or line.review_state == "excluded":
			continue
		source = sources[line.source]
		issues = set(regional.decode(line.issues) or []) - {"period", "finance"}
		if line.source_version != source.source_version:
			issues.add("regional_allocation")
		applied, date_state = dates[line.source]
		applied, date_basis = regional.management_date(applied, date_state, source.request_date, allow_submission=True)
		if not applied:
			issues.add("period")
		path = regional.contract.management_path(line)
		kind = "purchase" if line.approval_type == "purchase" else "operating"
		category = management.rules_service.classify(kind, path, rules)
		if not category:
			issues.add("management_category")
		output.append({"key": line.name, "source_key": line.source, "company": line.company,
			"is_legacy": True,
			"scope_company": line.regional_company or "",
			"number": line.approval_no, "application_date": applied, "month": applied[:7] if applied else None,
			"date_basis": date_basis,
			"kind": kind, "status": line.approval_status, "amount": str(line.amount) if line.amount is not None else None,
			"currency": line.currency, "description": line.description, "line_label": line.description,
			"management_category": category, "source_path": path, "issues": sorted(issues), "budget_state": "",
			"allocation_amounts": [], "budget_categories_complete": False})
	return output


def normalize(row):
	# Unified classification is optional here. Keep the underlying CN/MX
	# projections and all amount/company/date validation unchanged.
	row = {**row, "issues": sorted(set(row.get("issues") or []) - {"management_category"}),
		"date_basis": row.get("date_basis") or ("budget" if row.get("kind") == "budget" else "application")}
	path = [part for part in row.get("source_path") or [] if isinstance(part, str) and part.strip()]
	if path == ["unitemized"] and row.get("kind") == "budget":
		path = []
	row["source_path"] = path
	row["source_category_key"] = hashlib.sha256(json.dumps([row.get("kind"), path], ensure_ascii=False).encode()).hexdigest() if path else "missing"
	# Financial-approval period is irrelevant to this view. It remains intact
	# in the MX reconciliation API. The explicit application date is required.
	original = set(row.get("original_issues") or []) & (BLOCKING - {"period", "allocation", "organization"})
	row["issues"] = sorted(set(row["issues"]) | original)
	if not row.get("company"):
		row["issues"] = sorted(set(row["issues"]) | {"organization"})
	if not row.get("month"):
		row["issues"] = sorted(set(row["issues"]) | {"period"})
	row["eligible"] = bool(row.get("company") and row.get("month")
		and row.get("amount") is not None and not BLOCKING.intersection(row["issues"]))
	return row


def source_category_options(rows, f):
	# Offer alternatives from this user's company/month scope, before category
	# filtering, so choosing one option does not hide the other options.
	options = {}
	for row in rows:
		if f["company"] and row.get("company") != f["company"]:
			continue
		if row.get("month") != f["month"] and not (f["view"] == "review" and not row.get("month")):
			continue
		if row["kind"] == "budget" and f["view"] not in ("review", "budget"):
			continue
		options[row["source_category_key"]] = {"key": row["source_category_key"], "kind": row["kind"], "source_path": row["source_path"]}
	return sorted(options.values(), key=lambda row: (row["source_path"], row["kind"]))


def converter(loader=None):
	@lru_cache(maxsize=256)
	def rate(currency, day):
		try:
			return (loader or (lambda code, stamp: fx.load_month_rate(code, stamp, quiet=True)))(currency, day)
		except fx.RateUnavailable:
			return None

	def required(currency, day):
		value = rate(currency, day)
		if value is None:
			raise fx.RateUnavailable()
		return value

	converters = {currency: fx.Conversion(currency, required) for currency in CURRENCIES}

	def convert(rows):
		output = []
		for row in rows:
			amounts, rates = {}, {}
			day = row["month"] + "-01" if row.get("month") else None
			issues = set(row["issues"])
			for currency in CURRENCIES:
				try:
					factor = converters[currency].rate(row.get("currency"), day) if row["eligible"] else None
				except fx.RateUnavailable:
					factor = None
				value = fx.converted_amount(row.get("amount"), factor)
				amounts[currency] = str(value) if value is not None else None
				rates[currency] = str(factor) if factor is not None else None
				if row["eligible"] and value is None:
					issues.add("exchange_rate")
			output.append({**row, "amounts": amounts, "rates": rates, "rate_date": day, "issues": sorted(issues)})
		for currency in CURRENCIES:
			adjusted = management.reconcile_converted_lines([{**row, "exchange_rate": row["rates"][currency],
				"converted_amount": row["amounts"][currency]} for row in output])
			for row, adjustment in zip(output, adjusted):
				row["amounts"][currency] = adjustment["converted_amount"]
		return output
	return convert


def totals(rows):
	rows = list(rows)
	return {currency: None if any(row["amounts"][currency] is None for row in rows)
		else str(sum((Decimal(row["amounts"][currency]) for row in rows), Decimal("0.00"))) for currency in CURRENCIES}


def summarize(rows):
	spending = [row for row in rows if row["kind"] != "budget"]
	approved = [row for row in spending if row["status"] == "approved" and row["eligible"]]
	return {"amounts": totals(approved), "count": len(approved),
		"applications": len({row["source_key"] for row in approved}),
		"pending": len([row for row in spending if row["status"] == "pending"]),
		"review": len([row for row in spending if row["status"] in ("approved", "pending", "unknown") and row["issues"]]),
		"excluded": len([row for row in spending if row["status"] == "approved" and not row["eligible"]])}


def budget_metrics(rows, threshold, *, category_scope="", source_scope=""):
	result = {}
	for currency in CURRENCIES:
		projected = [{**row, "converted_amount": row["amounts"][currency]} for row in rows]
		result[currency] = management.metrics(projected, threshold)
		if category_scope == "unclassified" or source_scope:
			management.unclassified_comparison(result[currency])
	return result


def scope(rows, f):
	return [row for row in rows if (not f["company"] or row["company"] == f["company"])
		and (not f["category"] or (row.get("management_category") or "unclassified") == f["category"])
		and (not f["source_category"] or row["source_category_key"] == f["source_category"])]


def prepare(values=None):
	f = filters(values)
	rows, context, rules, metadata = load()
	if f["company"] and f["company"] not in context["companies"]:
		frappe.throw(_("You do not have permission to view this subsidiary."), frappe.PermissionError)
	metadata = {**metadata, "source_categories": source_category_options(rows, f)}
	return scope(rows, f), context, rules, metadata, f


def detail_rows(rows, f):
	selected = [row for row in rows if row.get("month") == f["month"] or f["view"] == "review" and not row.get("month")]
	if f["view"] == "review":
		selected = [row for row in selected if row["issues"] and row["status"] in ("approved", "pending", "unknown")]
	else:
		selected = [row for row in selected if row["kind"] != "budget"]
		if f["measure"] == "approved":
			selected = [row for row in selected if row["status"] == "approved" and row["eligible"]]
	return sorted(selected, key=lambda row: (row.get("application_date") or "", row["key"]), reverse=True)


def pagination(start):
	try:
		value = int(start)
	except (TypeError, ValueError):
		value = -1
	if not 0 <= value <= 50000:
		frappe.throw(_("Invalid expense page offset."))
	return value


def query(values=None, start=0):
	from . import expense_report_store, expense_reporting
	if expense_report_store.installed():
		return expense_reporting.query(values, start)
	rows, context, rules, metadata, f = prepare(values)
	convert = converter()
	current = convert([row for row in rows if row.get("month") == f["month"]])
	undated = [row for row in rows if not row.get("month")
		and row["status"] in ("approved", "pending", "unknown")]
	response = {"filters": f, "companies": sorted(context["companies"]),
		"categories": management.rules_service.CATEGORIES, "source_categories": metadata["source_categories"], "issues": ISSUES, "metadata": metadata,
		"summary": summarize(current), "undated": len(undated)}
	names = [f["company"]] if f["company"] else sorted(context["companies"])
	groups = defaultdict(list)
	for row in current:
		groups[row["company"]].append(row)
	companies = [{"company": name, **summarize(groups[name])} for name in names]
	companies.sort(key=lambda item: (item["amounts"]["CNY"] is None, -Decimal(item["amounts"]["CNY"] or 0), item["company"]))
	response["company_rows"] = companies
	response["unassigned"] = summarize(groups[""])["review"]
	if f["view"] == "budget":
		response["budgets"] = [{"company": name,
				"metrics": budget_metrics(groups[name], rules["warning_percent"], category_scope=f["category"], source_scope=f["source_category"])} for name in names]
		if not metadata["complete"]:
			for company in response["budgets"]:
				for metrics in company["metrics"].values():
					metrics.update(balance=None, forecast_balance=None, execution_rate=None)
					metrics["flags"] = sorted(set(metrics["flags"]) | {"incomplete"})
	if f["view"] in ("overview", "analysis"):
		category_groups = defaultdict(list)
		for row in current:
			if row["kind"] != "budget":
				category_groups[row.get("management_category") or "unclassified"].append(row)
		response["category_rows"] = [{"category": key, **summarize(group)} for key, group in category_groups.items()]
		response["category_rows"].sort(key=lambda item: -Decimal(item["amounts"]["CNY"] or 0))
		# Preserve management-category API fields for existing consumers; the
		# unified UI uses this additional exact-source breakdown by default.
		source_groups = defaultdict(list)
		for row in current:
			if row["kind"] != "budget":
				source_groups[row["source_category_key"]].append(row)
		response["source_category_rows"] = [{"category": key, "kind": group[0]["kind"],
			"source_path": group[0]["source_path"], **summarize(group)} for key, group in source_groups.items()]
		response["source_category_rows"].sort(key=lambda item: -Decimal(item["amounts"]["CNY"] or 0))
		months = [month for delta in range(5, -1, -1) if (month := management.shift_month(f["month"], -delta)) >= "1900-01"]
		history = convert([row for row in rows if row.get("month") in months and row.get("month") != f["month"]]) + current
		history_groups = defaultdict(list)
		for row in history:
			history_groups[row["month"]].append(row)
		response["trend"] = [{"month": month, **summarize(history_groups[month])} for month in months]
		response["comparison"] = comparison(history, f, metadata, bool(undated))
	if f["view"] in ("details", "review"):
		selected = detail_rows(current + convert(undated), f)
		start = pagination(start)
		response.update(total=len(selected), start=start, has_more=start + 50 < len(selected),
			rows=[public(row) for row in selected[start:start + 50]])
	return response


def comparison(rows, f, metadata, undated):
	from deeplinkerp_branding.services.mexico_expense_insights import comparison_period
	month, previous = f["month"], management.shift_month(f["month"], -1)
	last_day = monthrange(int(month[:4]), int(month[5:]))[1]
	window = comparison_period(month + "-01", f"{month}-{last_day:02}", frappe.utils.today())
	if not window:
		return {"percent": None, "window": None}
	groups = {period: [row for row in rows if row.get("month") == period and row.get("application_date")
		and row["application_date"] <= window[end]] for period, end in ((month, "to_date"), (previous, "previous_to"))}
	current, prior = summarize(groups[month]), summarize(groups[previous])
	known = metadata["complete"] and not undated and all(value["count"] and not value["review"]
		and value["amounts"]["CNY"] is not None for value in (current, prior))
	baseline = Decimal(prior["amounts"]["CNY"] or 0)
	percent = str(((Decimal(current["amounts"]["CNY"]) / baseline - 1) * 100).quantize(Decimal("0.1"))) if known and baseline > 0 else None
	return {"percent": percent, "window": window}


def public(row):
	return {key: row.get(key) for key in PUBLIC}


def detail(key, values=None):
	from . import expense_report_store, expense_reporting
	if expense_report_store.installed():
		return expense_reporting.detail(key, values)
	rows, _context, _rules, metadata, f = prepare(values)
	row = next((item for item in rows if item["key"] == key and (item.get("month") == f["month"]
		or f["view"] == "review" and not item.get("month"))), None)
	if row is None:
		frappe.throw(_("Expense detail is unavailable or outside your permitted scope."), frappe.PermissionError)
	if f["view"] == "details" and f["measure"] == "approved" and (row["status"] != "approved" or not row["eligible"] or row["kind"] == "budget"):
		frappe.throw(_("Expense detail is unavailable or outside your permitted scope."), frappe.PermissionError)
	return {"row": public(converter()([row])[0]), "metadata": metadata}


def export_rows(values=None):
	from . import expense_report_store, expense_reporting
	if expense_report_store.installed():
		return expense_reporting.export_rows(values)
	rows, _context, _rules, metadata, f = prepare(values)
	if not metadata["has_data"]:
		frappe.throw(_("No local approval snapshot is available yet. Wait for synchronization before exporting."))
	if f["view"] in ("overview", "analysis"):
		f = {**f, "measure": "approved"}
	# Restrict before FX lookup; exports use the very same scope and amounts.
	candidates = [row for row in rows if row.get("month") == f["month"]
		or f["view"] == "review" and not row.get("month")]
	selected = detail_rows(converter()(candidates), f)
	if len(selected) > 20000:
		frappe.throw(_("Expense export exceeds 20000 rows. Narrow the subsidiary or category."))
	return [public(row) for row in selected], f, metadata
