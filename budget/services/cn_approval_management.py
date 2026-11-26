"""Company/month budget execution over permission-scoped OA analytical lines.

The user-defined policy is latest approved budget per company/month, and all
approved operating AND purchase applications occupy that budget. Payment and
accounting settlement are deliberately not inferred from approval data.
"""
from collections import defaultdict
from copy import deepcopy
from decimal import Decimal, ROUND_HALF_UP, ROUND_DOWN
import hashlib

import frappe
from frappe import _

from . import cn_approval_management_rules as rules_service
from . import cn_approval_workbench as approvals
from . import cn_approval_fx as fx
from . import cn_approval_settings as settings

CENT = Decimal("0.01")
REPORTS = {
	"company": "Company budget execution report",
	"budget": "Budget category variance report",
	"trend": "Monthly budget and expenditure report",
	"hr": "Personnel expenditure report",
	"purchase": "Purchase expenditure report",
}
ISSUES = {
	"management_category": "Management category is not mapped",
	"budget_version": "The latest approved budget cannot be determined",
	"allocation": "Company allocation or detail total needs review",
	"budget_detail": "Budget details do not reconcile to the approved total",
	"history": "Some disabled templates have no verified local history",
}
MEASURES = {"budget", "declared_budget", "approved", "pending", "operating", "purchase", "spending", "superseded"}
METRICS = {"budget": "Effective budget", "approved": "Approved budget occupation", "pending": "Pending expenditure applications",
	"operating": "Approved operating expenditure", "purchase": "Approved purchase expenditure",
	"balance": "Budget difference", "forecast_balance": "Difference if all pending applications pass", "execution_rate": "Budget execution rate"}


def cell_value(detail, prefixes):
	values = [str(c.get("value") or "").strip() for c in detail.get("cells", [])
		if any(c.get("label", "").startswith(prefix) for prefix in prefixes) and c.get("value") not in (None, "")]
	return values[0] if len(set(values)) == 1 else ""


def source_path(row, table=None, detail=None):
	if row["kind"] != "budget":
		return list(row.get("classification") or [])
	if table is None:
		return ["unitemized"]
	group = table.get("group") or table.get("label") or "unitemized"
	label = detail.get("label") or cell_value(detail, ("明细项目", "预算明细", "Ítem", "Detalle presupuestario"))
	return [group, label] if label else [group]


def allocation_sources(row):
	if row["kind"] == "operating":
		return {detail.get("entity", cell_value(detail, ("部门", "Departamento"))) for table in row.get("tables", []) if table.get("is_amount")
			for detail in table.get("rows", [])} - {""}
	return set()


def company_for(row, context):
	company = context["aliases"].get((row["corp_id"], row.get("entity", "")))
	if not company and row.get("entity_id"):
		company = context.get("organizations", {}).get((row["corp_id"], row["entity_id"]))
	if not company and row.get("entity") in context["known_companies"]:
		company = row["entity"]
	return company or ""


def budget_versions(rows, context):
	groups, states = defaultdict(list), {}
	for row in rows:
		if row["kind"] != "budget" or row["status"] != "approved" or not row.get("month"):
			continue
		company = company_for(row, context)
		# Unmapped departments cannot stand in for a company when selecting its
		# last budget. Retain declarations, but do not invent an effective limit.
		if not company:
			states[row["key"]] = "unresolved"
		else:
			groups[(company, row["month"])].append(row)
	for group in groups.values():
		if len(group) == 1:
			states[group[0]["key"]] = "effective"
			continue
		latest = max((row.get("approved_at") or "" for row in group), default="")
		winners = [row for row in group if row.get("approved_at") == latest]
		if any(not row.get("approved_at") for row in group) or len(winners) != 1:
			states.update({row["key"]: "unresolved" for row in group})
		else:
			states.update({row["key"]: "effective" if row is winners[0] else "superseded" for row in group})
	return states


def analytical_lines(rows, context, rules):
	states = budget_versions(rows, context)
	output = []
	for original in rows:
		base = {key: deepcopy(value) for key, value in original.items() if key not in ("tables", "facts", "attachments", "budget_snapshot", "_routing_source")}
		base["source_key"] = original["key"]
		base["budget_state"] = states.get(original["key"], "")
		base["project"] = next((item["value"] for item in original.get("facts", []) if item["label"] == "Project"), "")
		parts = [(table, detail, ti, di) for ti, table in enumerate(original.get("tables", [])) if table.get("is_amount")
			for di, detail in enumerate(table.get("rows", []))]
		complete = (bool(parts) and original.get("amount") is not None and original.get("currency")
			and all(detail.get("amount_currency", "CNY") == original["currency"] for _, detail, _, _ in parts)
			and all(detail.get("amount") is not None for _, detail, _, _ in parts)
			and sum((Decimal(detail["amount"]) for _, detail, _, _ in parts), Decimal(0)) == Decimal(original["amount"]))
		if original["kind"] == "budget" and not complete:
			base["issues"] = sorted(set(base["issues"]) | {"budget_detail"})
		if parts and original["kind"] == "operating" and not complete:
			base.update(entity="", entity_id="")
			base["issues"] = sorted(set(base["issues"]) | {"allocation"})
		if not complete:
			parts = [(None, None, -1, -1)]
		allocation_amounts = [detail["amount"] for _, detail, _, _ in parts] if complete else []
		for part_index, (table, detail, ti, di) in enumerate(parts):
			item = deepcopy(base)
			item["allocation_amounts"] = allocation_amounts
			item["allocation_index"] = part_index
			item["header_amount"] = original.get("amount")
			path = source_path(original, table, detail)
			if table is not None:
				item["amount"] = detail["amount"]
				item["line_label"] = detail.get("label") or table.get("label") or ""
				if original["kind"] == "operating":
					item.update(entity=detail.get("entity", cell_value(detail, ("部门", "Departamento"))), entity_id=detail.get("entity_id", ""))
			else:
				item["line_label"] = ""
			item["source_path"] = path
			item["management_category"] = rules_service.classify(item["kind"], path, rules)
			item["key"] = hashlib.sha256(f"{original['key']}:{ti}:{di}".encode()).hexdigest()
			item["table_index"], item["detail_index"] = ti, di
			item["company"] = company_for(item, context)
			if item["company"] and item["company"] not in context["companies"] or not item["company"] and not context["manager"]:
				continue
			issues = set(item["issues"]) - {"organization"}
			if not item["company"]:
				issues.add("organization")
			if not item["management_category"]:
				issues.add("management_category")
			if item["budget_state"] == "unresolved":
				issues.add("budget_version")
			item["issues"] = sorted(issues)
			output.append(item)
			if len(output) > 50000:
				frappe.throw(_("CN management analysis exceeds 50000 detail lines. Narrow the configured scope."))
	annotate_budget_completeness(output)
	return output


def annotate_budget_completeness(output):
	budget_groups = defaultdict(list)
	for row in output:
		if row["kind"] == "budget" and row["status"] == "approved" and row["budget_state"] in ("effective", "unresolved"):
			budget_groups[(row["company"], row["month"])].append(row)
	for row in output:
		budget = budget_groups.get((row["company"], row["month"]), [])
		row["budget_categories_complete"] = bool(row["company"] and budget and all(item["budget_state"] == "effective"
			and item["management_category"] and item.get("amount") is not None and not {"budget_detail", "structure"} & set(item["issues"]) for item in budget))


def validate_filters(values):
	f = approvals.filters(values)
	if not f["reporting_currency"]:
		f["reporting_currency"] = "CNY"
	if (int(f["to_month"][:4]) - int(f["from_month"][:4])) * 12 + int(f["to_month"][5:]) - int(f["from_month"][5:]) >= 60:
		frappe.throw(_("Select at most 60 months for CN management reports."))
	return f


def matches_measure(row, measure):
	if not measure:
		return True
	if measure == "budget":
		return row["kind"] == "budget" and row["status"] == "approved" and row["budget_state"] in ("effective", "unresolved")
	if measure == "declared_budget":
		return row["kind"] == "budget" and row["status"] == "approved"
	if measure == "superseded":
		return row["budget_state"] == "superseded"
	if measure == "spending":
		return row["kind"] != "budget" and row["status"] in ("approved", "pending")
	return row["kind"] != "budget" and row["status"] == ("pending" if measure == "pending" else "approved") and (measure not in ("operating", "purchase") or row["kind"] == measure)


def selected_lines(lines, f, *, details=False):
	selected = []
	for row in lines:
		if not row.get("month"):
			if not details or not f["review"]:
				continue
		elif not f["from_month"] <= row["month"] <= f["to_month"]:
			continue
		if f["company"] and row["company"] != f["company"]:
			continue
		if f.get("unassigned") == "1" and row["company"]:
			continue
		category = f.get("management_category")
		if category and row["management_category"] != ("" if category == "unclassified" else category):
			continue
		if f.get("project") and row["project"] != f["project"] or f.get("no_project") == "1" and row["project"]:
			continue
		if details:
			if f.get("category") and row["category"] != f["category"]:
				continue
			if not matches_measure(row, f.get("measure")):
				continue
			if f["kind"] and row["kind"] != f["kind"] or f["status"] and row["status"] != f["status"]:
				continue
			if f["review"] and not row["issues"] or f["issue"] and f["issue"] not in row["issues"]:
				continue
			if f["keyword"] and f["keyword"].casefold() not in " ".join(str(row.get(key) or "") for key in ("number", "entity", "applicant", "description", "line_label", "project")).casefold():
				continue
		selected.append(row)
	return selected


def load_lines():
	from ..integrations.cn_approval_history import read_snapshot
	approvals.require_reader()
	from deeplinkerp_branding.services.regional_expense_automatic import ensure_rebuild
	if ensure_rebuild():
		frappe.throw(_("Expense ownership is being updated automatically. Please wait."))
	context = approvals.access_context()
	snapshot = read_snapshot()
	rules = settings.management_rules()
	lines, snapshot = with_regional_allocations(snapshot, context, rules)
	if snapshot.get("missing_history_templates"):
		for row in lines:
			row["issues"] = sorted(set(row["issues"]) | {"history"})
	return snapshot, lines, context, rules


def with_regional_allocations(snapshot, context, rules):
	from deeplinkerp_branding.services.regional_expenses import cn_projection
	identities, incoming = cn_projection(context)
	rows = [row for row in snapshot["rows"] if row["kind"] == "budget" or
		(row["corp_id"], row["process_instance_id"]) not in identities]
	lines = analytical_lines(rows, context, rules) + incoming
	if len(lines) > 50000:
		frappe.throw(_("CN management analysis exceeds 50000 detail lines. Narrow the configured scope."))
	annotate_budget_completeness(lines)
	if incoming:
		snapshot = {**snapshot, "has_snapshot": True,
			"fetched_at": max([snapshot.get("fetched_at") or ""] + [row["allocation_modified"] for row in incoming])}
	return lines, snapshot


def query(values, *, details=False, with_comparisons=False, report_id=None):
	f = validate_filters(values)
	snapshot, lines, context, rules = load_lines()
	if f["company"] and f["company"] not in context["companies"]:
		frappe.throw(_("You do not have permission to view this subsidiary."), frappe.PermissionError)
	if with_comparisons:
		snapshot = {**snapshot, "comparisons": comparisons(lines, f, snapshot)}
	selected = selected_lines(fx.annotate(lines, f["reporting_currency"]), f, details=details)
	# Resolve budget versions and permissions on the full projection first, but
	# never request exchange rates for lines outside this report's scope.
	if report_id == "purchase":
		selected = [row for row in selected if row["kind"] == "purchase"]
	elif report_id == "hr":
		selected = [row for row in selected if row["management_category"] in rules_service.HR_CATEGORIES]
	return snapshot, convert_lines(selected, f["reporting_currency"]), context, rules, f


def convert_lines(rows, target, *, loader=None):
	"""Keep the existing per-approval FX total when distributing its detail cents.

	Remainder cents go to the largest fractional remainders, with a stable
	source-order tie break. It is calculated from the complete allocation
	before company/category filtering, so subreports always reconcile.
	"""
	return reconcile_converted_lines(fx.convert(rows, target, loader=loader))


def reconcile_converted_lines(converted):
	"""Allocate rounding cents using the full source, before visibility filters."""
	adjustments = {}
	for row in converted:
		parts = row.get("allocation_amounts") or []
		if not parts or row.get("exchange_rate") is None:
			continue
		key = row["source_key"]
		if key not in adjustments:
			rate = Decimal(row["exchange_rate"])
			amounts = [Decimal(value) for value in parts]
			header = (Decimal(row["header_amount"]) * rate).quantize(CENT, rounding=ROUND_HALF_UP)
			raw = [value * rate for value in amounts]
			allocated = [value.quantize(CENT, rounding=ROUND_DOWN) for value in raw]
			cents = int((header - sum(allocated, Decimal(0))) / CENT)
			order = sorted(range(len(amounts)), key=lambda index: raw[index] - allocated[index], reverse=True)
			for index in order[:cents]:
				allocated[index] += CENT
			adjustments[key] = allocated
		row["converted_amount"] = str(adjustments[key][row["allocation_index"]])
	return converted


def total(rows, *, empty="0.00"):
	rows = list(rows)
	if not rows:
		return empty
	if any(row.get("converted_amount") is None or "structure" in row["issues"] for row in rows):
		return None
	return str(sum((Decimal(row["converted_amount"]) for row in rows), Decimal(0)).quantize(CENT))


def subtract(first, *others):
	return str((Decimal(first) - sum((Decimal(value) for value in others), Decimal(0))).quantize(CENT)) if all(value is not None for value in (first, *others)) else None


def metrics(rows, threshold):
	budgets = [row for row in rows if row["kind"] == "budget" and row["status"] == "approved" and row["budget_state"] in ("effective", "unresolved")]
	approved = [row for row in rows if row["kind"] != "budget" and row["status"] == "approved"]
	pending = [row for row in rows if row["kind"] != "budget" and row["status"] == "pending"]
	unresolved = any(row["budget_state"] == "unresolved" for row in budgets)
	category_absent = not budgets and bool(rows) and all(row.get("budget_categories_complete") for row in rows)
	budget = None if unresolved else total(budgets, empty="0.00" if category_absent else None)
	spent, waiting = total(approved), total(pending)
	# Unknown allocations and missing company budgets cannot be netted against
	# another company's surplus. Group-level comparability is computed below.
	balance = subtract(budget, spent)
	forecast = subtract(budget, spent, waiting)
	rate = str((Decimal(spent) / Decimal(budget) * 100).quantize(CENT)) if budget is not None and Decimal(budget) > 0 and spent is not None else None
	flags = []
	if unresolved or (budgets and budget is None) or spent is None or waiting is None or any("history" in row["issues"] for row in rows):
		flags.append("incomplete")
	if not budgets and not category_absent and (approved or pending):
		flags.append("no_budget")
	if balance is not None and Decimal(balance) < 0:
		flags.append("over_budget")
	elif forecast is not None and Decimal(forecast) < 0:
		flags.append("forecast_over")
	elif rate is not None and Decimal(rate) >= Decimal(str(threshold)):
		flags.append("near_limit")
	if any("history" in row["issues"] for row in rows):
		balance = forecast = rate = None
	return {"budget": budget, "approved": spent, "pending": waiting,
		"operating": total(row for row in approved if row["kind"] == "operating"),
		"purchase": total(row for row in approved if row["kind"] == "purchase"),
		"balance": balance, "forecast_balance": forecast, "execution_rate": rate, "flags": flags,
		"budget_count": len({row["source_key"] for row in budgets}),
		"approval_count": len({row["source_key"] for row in approved}),
		"pending_count": len({row["source_key"] for row in pending}),
		"count": len({row["source_key"] for row in rows}),
		"review_count": len({row["source_key"] for row in rows if row["issues"]}),
		"superseded_count": len({row["source_key"] for row in rows if row["budget_state"] == "superseded"})}


def comparable_metrics(rows, threshold):
	result = metrics(rows, threshold)
	periods = defaultdict(list)
	for row in rows:
		periods[(row["company"], row["month"])].append(row)
	parts = [metrics(part, threshold) for part in periods.values()]
	if any(not company and any(row["kind"] != "budget" for row in part) for (company, _month), part in periods.items()) or any(part["budget"] is None and part["approval_count"] + part["pending_count"] for part in parts):
		result.update(balance=None, forecast_balance=None, execution_rate=None)
	result["flags"] = sorted(set(result["flags"]) | {flag for part in parts for flag in part["flags"]})
	return result


def unclassified_comparison(result):
	"""Unknown budget and expense categories cannot establish a shared limit."""
	result.update(balance=None, forecast_balance=None, execution_rate=None)
	result["flags"] = sorted(set(result["flags"]) - {"no_budget", "over_budget", "forecast_over", "near_limit"}
		| ({"incomplete"} if result["count"] else set()))
	return result


def grouped(rows, fields, threshold, *, category_scope=""):
	groups = defaultdict(list)
	for row in rows:
		groups[tuple(row.get(key, "") for key in fields)].append(row)
	result = [{**dict(zip(fields, key)), **comparable_metrics(group, threshold)} for key, group in sorted(groups.items())]
	for row in result:
		if category_scope == "unclassified" or "management_category" in fields and not row["management_category"]:
			unclassified_comparison(row)
	return result


def aggregate(rows, context, rules, f):
	threshold = rules["warning_percent"]
	category_scope = f.get("management_category", "")
	companies = grouped(rows, ("company",), threshold, category_scope=category_scope)
	# Show accessible companies without activity as an explicit optional group.
	seen = {row["company"] for row in companies}
	for company in sorted(context["companies"] - seen):
		if not f.get("unassigned") and (not f["company"] or company == f["company"]):
			companies.append({"company": company, **metrics([], threshold)})
	company_months = grouped(rows, ("company", "month"), threshold, category_scope=category_scope)
	# Do not let excess budget in another month mask expenditure with no budget.
	for company in companies:
		months = [row for row in company_months if row["company"] == company["company"]]
		if any(row["budget"] is None and row["approval_count"] + row["pending_count"] > 0 for row in months):
			company.update(balance=None, forecast_balance=None, execution_rate=None)
		company["flags"] = sorted(set(company["flags"]) | {flag for row in months for flag in row["flags"]})
	grand = comparable_metrics(rows, threshold)
	if category_scope == "unclassified":
		unclassified_comparison(grand)
	if any(not row["company"] and row["count"] or row["budget"] is None and row["approval_count"] + row["pending_count"] for row in company_months):
		grand.update(balance=None, forecast_balance=None, execution_rate=None)
	grand["over_budget_companies"] = sum("over_budget" in row["flags"] for row in companies if row["company"])
	grand["forecast_over_companies"] = sum("forecast_over" in row["flags"] for row in companies if row["company"])
	quality = {"count": len({row["source_key"] for row in rows}), "lines": len(rows),
		"unassigned": len({row["source_key"] for row in rows if not row["company"]}),
		"unclassified": len({row["source_key"] for row in rows if not row["management_category"]}),
		"issues": {key: len({row["source_key"] for row in rows if key in row["issues"]}) for key in sorted({i for row in rows for i in row["issues"]})}}
	return {"totals": grand, "companies": sorted(companies, key=lambda row: (not bool(row["count"]), row["company"])),
		"company_months": company_months,
		"categories": grouped(rows, ("management_category",), threshold),
		"category_months": grouped(rows, ("company", "month", "management_category"), threshold),
		"months": grouped(rows, ("month",), threshold, category_scope=category_scope), "quality": quality,
		"top_spending": [public_line(row) for row in sorted((row for row in rows if row["kind"] != "budget" and row["status"] == "approved"),
			key=lambda row: Decimal(row["converted_amount"] or 0), reverse=True)[:10]],
		"warning_percent": threshold, "budget_policy": "latest_approved", "purchase_policy": "all"}


def shift_month(month, delta):
	year, month_number = map(int, month.split("-"))
	year, index = divmod(year * 12 + month_number - 1 + delta, 12)
	return f"{year:04d}-{index + 1:02d}"


def comparisons(lines, f, snapshot):
	"""Compare closed periods using current approval status, never partial months.

	The source has no historical status-at-date ledger or stated coverage start.
	An absent baseline is therefore unavailable, not a fabricated zero baseline.
	"""
	end = f["to_month"]
	if end >= str(frappe.utils.today())[:7]:
		return {"reason": "open_period", "items": [], "drivers": []}
	if not snapshot.get("has_snapshot", True) or snapshot.get("missing_history_templates"):
		return {"reason": "incomplete", "items": [], "drivers": []}
	months = (int(end[:4]) - int(f["from_month"][:4])) * 12 + int(end[5:]) - int(f["from_month"][5:]) + 1
	current_spending = convert_lines([row for row in selected_lines(lines, f) if matches_measure(row, "approved")], f["reporting_currency"])
	current_total = total(current_spending)
	items, drivers = [], []
	for code, offset in (("previous", -months), ("year", -12)):
		start_month, end_month = shift_month(f["from_month"], offset), shift_month(end, offset)
		if start_month < "0001-01":
			continue
		baseline = selected_lines(lines, {**f, "from_month": start_month, "to_month": end_month})
		# No recorded baseline does not establish that the source contains all
		# history for this company/category. Keep comparison explicitly absent.
		flags = getattr(frappe.local, "flags", None)
		muted = flags.get("mute_messages", False) if flags is not None else False
		try:
			if flags is not None:
				flags["mute_messages"] = True
			previous_spending = convert_lines([row for row in baseline if matches_measure(row, "approved")], f["reporting_currency"])
		except frappe.ValidationError:
			items.append({"code": code, "from_month": start_month, "to_month": end_month, "current": current_total,
				"previous": None, "change": None, "percent": None, "reason": "exchange_rate"})
			continue
		finally:
			if flags is not None:
				flags["mute_messages"] = muted
		previous_total = total(previous_spending) if baseline else None
		change = subtract(current_total, previous_total)
		percent = str((Decimal(change) / Decimal(previous_total) * 100).quantize(CENT)) if change is not None and Decimal(previous_total) > 0 else None
		items.append({"code": code, "from_month": start_month, "to_month": end_month, "current": current_total,
			"previous": previous_total, "change": change, "percent": percent})
		if code == "previous" and change is not None:
			for field in ("company", "management_category"):
				for value in sorted({row[field] for row in current_spending + previous_spending}):
					amount = total(row for row in current_spending if row[field] == value)
					old = total(row for row in previous_spending if row[field] == value)
					difference = subtract(amount, old)
					if difference is not None and Decimal(difference) != 0:
						drivers.append({"dimension": field, "value": value, "current": amount, "previous": old, "change": difference})
	drivers.sort(key=lambda row: abs(Decimal(row["change"])), reverse=True)
	return {"reason": "", "items": items, "drivers": drivers[:10]}


def public_line(row):
	return {**approvals.public_row(row), **{key: row.get(key) for key in ("source_key", "line_label", "management_category", "source_path", "budget_state", "approved_at", "project", "allocation_origin", "regional_allocation")}}


def overview(values=None):
	snapshot, rows, context, rules, f = query(values, with_comparisons=True)
	return {"comparisons": snapshot["comparisons"], "filters": f, "fetched_at": snapshot["fetched_at"], "snapshot_id": snapshot.get("snapshot_id"),
		"has_snapshot": snapshot.get("has_snapshot", True), "sync": snapshot.get("sync"),
		"management": aggregate(rows, context, rules, f), "rows": [], "total": len(rows),
		"options": {"companies": sorted(context["companies"]), "reporting_currencies": sorted(fx.CURRENCIES),
			"management_categories": rules_service.CATEGORIES, "can_manage_settings": settings.can_manage(context["companies"])},
		"issue_labels": {**approvals.ISSUES, **ISSUES}, "can_export": snapshot.get("has_snapshot", True), "reports": REPORTS,
		"retained_history": snapshot.get("retained_history", []), "missing_history_templates": snapshot.get("missing_history_templates", 0)}


def details(values=None, start=0):
	snapshot, rows, context, rules, f = query(values, details=True)
	try:
		start = int(start)
	except (TypeError, ValueError):
		frappe.throw(_("Invalid CN approval filters."))
	if start < 0 or start > 50000:
		frappe.throw(_("Invalid CN approval filters."))
	rows.sort(key=lambda row: (row.get("month") or "", row.get("application_date") or "", row["key"]), reverse=True)
	return {"filters": f, "fetched_at": snapshot["fetched_at"], "snapshot_id": snapshot.get("snapshot_id"), "sync": snapshot.get("sync"),
		"has_snapshot": snapshot.get("has_snapshot", True), "rows": [public_line(row) for row in rows[start:start + 25]],
		"total": len(rows), "start": start, "page_length": 25, "summary": approvals.aggregate(rows),
		"options": {"companies": sorted(context["companies"]), "reporting_currencies": sorted(fx.CURRENCIES),
			"management_categories": rules_service.CATEGORIES, "can_manage_settings": settings.can_manage(context["companies"])},
		"issue_labels": {**approvals.ISSUES, **ISSUES}, "can_export": snapshot.get("has_snapshot", True), "analytical_lines": True,
		"retained_history": snapshot.get("retained_history", []), "missing_history_templates": snapshot.get("missing_history_templates", 0)}


def detail(key, reporting_currency="CNY"):
	from ..integrations.cn_approval_history import read_snapshot
	from deeplinkerp_branding.services.mexico_expenses import _dingtalk_url
	approvals.require_reader()
	if not isinstance(key, str) or len(key) != 64 or any(c not in "0123456789abcdef" for c in key):
		frappe.throw(_("Invalid approval identity."))
	context, snapshot = approvals.access_context(), read_snapshot()
	rules = settings.management_rules()
	lines, snapshot = with_regional_allocations(snapshot, context, rules)
	line = next((row for row in lines if row["key"] == key), None)
	if line is None:
		frappe.throw(_("This approval is unavailable or you do not have permission to view it."), frappe.PermissionError)
	target = validate_filters({"reporting_currency": reporting_currency})["reporting_currency"]
	line = convert_lines([line], target)[0]
	if line.get("regional_allocation"):
		from deeplinkerp_branding.services.regional_expenses import cn_detail
		return {**public_line(line), **cn_detail(line, context)}
	source = next(row for row in snapshot["rows"] if row["key"] == line["source_key"])
	# A line-specific company permission never grants access to the other
	# companies' allocations or the entire OA document and its attachments.
	full_context = {**context, "manager": True, "companies": context["known_companies"]}
	all_lines = analytical_lines([source], full_context, rules)
	visible = {row["key"] for row in lines if row["source_key"] == source["key"]}
	full_access = all(row["key"] in visible for row in all_lines)
	tables = []
	if line["table_index"] >= 0:
		table = source["tables"][line["table_index"]]
		tables = [{**table, "rows": [table["rows"][line["detail_index"]]]}]
	elif full_access:
		tables = source.get("tables", [])
	return {**public_line(line), "facts": source.get("facts", []) if full_access else [], "tables": tables,
		"detail_total": None, "detail_currency": "CNY" if tables else None, "budget_snapshot": {},
		"attachments": source.get("attachments", []) if full_access else [],
		"approval_url": _dingtalk_url(source) if full_access else ""}


def report_data(report_id, values=None):
	if report_id not in REPORTS:
		frappe.throw(_("Invalid CN management report."))
	snapshot, rows, context, rules, f = query(values, report_id=report_id)
	threshold = rules["warning_percent"]
	if report_id == "company":
		data = aggregate(rows, context, rules, f)["companies"]
		dimensions = [("company", "Subsidiary")]
		metric_fields = ["budget", "operating", "purchase", "approved", "pending", "balance", "execution_rate", "forecast_balance"]
	elif report_id in ("budget", "hr"):
		data = grouped(rows, ("company", "month", "management_category"), threshold)
		dimensions = [("company", "Subsidiary"), ("month", "Reporting month"), ("management_category", "Management category")]
		metric_fields = ["budget", "operating", "purchase", "approved", "pending", "balance", "execution_rate"]
	elif report_id == "trend":
		data = grouped(rows, ("company", "month"), threshold, category_scope=f.get("management_category", ""))
		dimensions = [("company", "Subsidiary"), ("month", "Reporting month")]
		metric_fields = ["budget", "operating", "purchase", "approved", "pending", "balance", "execution_rate"]
	else:
		data = grouped(rows, ("company", "month", "management_category", "project"), threshold)
		dimensions = [("company", "Subsidiary"), ("month", "Reporting month"), ("management_category", "Management category"), ("project", "Project")]
		metric_fields = ["purchase", "pending"]
	columns = [{"fieldname": field, "label": label, "fieldtype": "Data"} for field, label in dimensions]
	columns += [{"fieldname": field, "label": METRICS[field], "fieldtype": "Percent" if field == "execution_rate" else "Currency"} for field in metric_fields]
	columns += [{"fieldname": "approval_count", "label": "Approved applications", "fieldtype": "Int"},
		{"fieldname": "review_count", "label": "Applications needing review", "fieldtype": "Int"}]
	return {"report_id": report_id, "title": REPORTS[report_id], "filters": f, "fetched_at": snapshot.get("fetched_at"),
		"snapshot_id": snapshot.get("snapshot_id"), "sync": snapshot.get("sync"), "has_snapshot": snapshot.get("has_snapshot", True),
		"columns": columns, "rows": data, "can_export": snapshot.get("has_snapshot", True),
		"retained_history": snapshot.get("retained_history", []), "missing_history_templates": snapshot.get("missing_history_templates", 0),
		"options": {"companies": sorted(context["companies"]), "reporting_currencies": sorted(fx.CURRENCIES),
			"management_categories": rules_service.CATEGORIES, "can_manage_settings": settings.can_manage(context["companies"])},
		"quality": aggregate(rows, context, rules, f)["quality"], "categories": rules_service.CATEGORIES}
