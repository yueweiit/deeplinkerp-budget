"""Unified expense queries and source-owned synchronization commands."""
import base64

import frappe
from frappe import _

from ..services import expense_management as service


@frappe.whitelist()
def get_sync_status():
	from ..services import expense_sync
	result = expense_sync.get_status()
	from ..services import expense_report_store as store
	if store.installed():
		state = store.state() or {}
		cn = result["sources"]["cn"]
		cn["revision"] = [*(cn.get("revision") or []), state.get("publication")]
	return result


@frappe.whitelist(methods=["POST"])
def enqueue_sync(source, retry_failed=False):
	from ..services import expense_sync
	return expense_sync.enqueue(source, retry_failed=retry_failed)


@frappe.whitelist()
def get_sync_settings():
	from ..services import expense_sync_settings
	return expense_sync_settings.get_settings()


@frappe.whitelist()
def get_sync_mapping_candidates(kind, start=0):
	from ..services import expense_sync_settings
	return expense_sync_settings.mapping_candidates(kind, start)


@frappe.whitelist(methods=["POST"])
def save_sync_settings(values):
	from ..services import expense_sync_settings
	return expense_sync_settings.save_settings(values)


@frappe.whitelist(methods=["POST"])
def preview_sync_template(template, business_timezone, template_defaults=None, processing_mode=None):
	from ..services import expense_sync_settings
	return expense_sync_settings.preview_template(template, business_timezone, template_defaults, processing_mode)


@frappe.whitelist(methods=["POST"])
def inspect_sync_template(template, business_timezone, template_defaults=None, processing_mode="approval"):
	from ..services import expense_sync_settings
	return expense_sync_settings.inspect_template(template, business_timezone, template_defaults, processing_mode)


@frappe.whitelist()
def get_options():
	service.approvals.require_reader()
	return {"companies": sorted(service.approvals.allowed_companies()),
		"mx_companies": frappe.get_list("Company", filters={"is_group": 0, "country": "Mexico"}, pluck="name", limit_page_length=0),
		"categories": service.management.rules_service.CATEGORIES, "issues": service.ISSUES}


@frappe.whitelist()
def get_data(filters=None, start=0):
	return service.query(filters, start)


@frappe.whitelist()
def get_detail(key, filters=None):
	return service.detail(key, filters)


def source_labels(row):
	path = list(row.get("source_path") or [])
	if path and row.get("kind") == "budget":
		groups = {"hr": "Personnel budget", "office": "Office premises budget", "operation": "Operating budget"}
		if path[0] in groups:
			path[0] = _(groups[path[0]])
	return path


def source_filter_label(filters, metadata):
	if not filters["source_category"]:
		return _("All categories")
	option = next((row for row in metadata["source_categories"] if row["key"] == filters["source_category"]), {})
	return " / ".join(source_labels(option)) or (_("Category not provided") if filters["source_category"] == "missing" else _("Selected OA category"))


@frappe.whitelist()
def export_data(filters=None):
	from .cn_approvals import excel_amount
	if service.filters(filters)["view"] == "budget":
		return export_budget(filters)
	rows, f, metadata = service.export_rows(filters)
	headers = ("Approval number", "Subsidiary", "Application date", "Reporting month", "Approval status", "OA expense category", "OA expense subcategory",
		"Detail item", "Original amount", "Currency", "Amount in CNY", "Amount in MXN", "Exchange rate date",
		"CNY exchange rate", "MXN exchange rate", "Date source", "Included in approved expenditure", "Review issues", "Management category (optional)")
	issues = service.ISSUES
	statuses = dict(zip(service.approvals.STATUSES, ("Approved", "In progress", "Rejected", "Terminated", "Withdrawn", "Unknown")))
	data = [[_("Expense Management")], [_("Reporting month"), f["month"]],
		[_("Subsidiary"), f["company"] or _("All Subsidiaries")],
		[_("OA expense category"), source_filter_label(f, metadata)],
		[_("Management category"), _(service.management.rules_service.CATEGORIES.get(f["category"], "Unclassified" if f["category"] else "All"))],
		[_("Approved expenses by application month; approval does not confirm payment.")],
		[_("CNY and MXN are converted independently from original amounts using the application month-start rate.")],
		[_("Snapshot complete"), _("Yes") if metadata["complete"] else _("No")], [], [_(label) for label in headers]]
	for row in rows:
		path = source_labels(row)
		data.append([row["number"], row["company"] or _("Company not matched"), row["application_date"], row["month"],
			_(statuses.get(row["status"], "Unknown")),
			path[0] if path else _("Category not provided"), " / ".join(path[1:]),
			row["line_label"] or row["description"], excel_amount(row["amount"]), row["currency"],
			excel_amount(row["amounts"]["CNY"]), excel_amount(row["amounts"]["MXN"]), row["rate_date"],
			row["rates"]["CNY"], row["rates"]["MXN"],
			_({"submission": "OA submission date", "budget": "Budget month"}.get(row["date_basis"], "Application date")),
			_("Yes") if row["eligible"] and row["status"] == "approved" and row["kind"] != "budget" else _("No"),
			"; ".join(_(issues.get(issue, "Needs review")) for issue in row["issues"]),
			_(service.management.rules_service.CATEGORIES[row["management_category"]]) if row["management_category"] else ""])
	return workbook(data, f"expenses-{f['month']}.xlsx")


def export_budget(filters):
	from .cn_approvals import excel_amount
	result = service.query(filters)
	if not result["metadata"]["has_data"]:
		frappe.throw(_("No local approval snapshot is available yet. Wait for synchronization before exporting."))
	data = [[_("Budget Execution"), result["filters"]["month"]],
		[_("Subsidiary"), result["filters"]["company"] or _("All Subsidiaries")],
		[_("OA expense category"), source_filter_label(result["filters"], result["metadata"])],
		[_("Management category"), _(service.management.rules_service.CATEGORIES.get(result["filters"]["category"], "Unclassified" if result["filters"]["category"] else "All"))],
		[_("Budgets keep their budget month. The latest approved budget per subsidiary is used; a missing budget is not zero.")],
		*([[_("Budget differences and execution rates are unavailable when filtering by OA category. Use unified categories for category-level budget comparisons.")]] if result["filters"]["source_category"] else []),
		[_("Snapshot complete"), _("Yes") if result["metadata"]["complete"] else _("No")], [],
		[_("Subsidiary")] + [_(label) + " · " + currency for label in ("Effective budget", "Approved budget occupation", "Budget difference")
			for currency in service.CURRENCIES] + [_("Budget execution rate")]]
	for row in result["budgets"]:
		data.append([row["company"]] + [excel_amount(row["metrics"][currency][key])
			for key in ("budget", "approved", "balance") for currency in service.CURRENCIES]
			+ [row["metrics"]["CNY"]["execution_rate"]])
	return workbook(data, f"expense-budget-{result['filters']['month']}.xlsx")


def workbook(data, filename):
	from frappe.utils.xlsxutils import make_xlsx
	data = [["'" + value if isinstance(value, str) and value.startswith(("=", "+", "-", "@")) else value for value in row] for row in data]
	return {"filename": filename,
		"content": base64.b64encode(make_xlsx(data, "Data Export").getvalue()).decode()}
