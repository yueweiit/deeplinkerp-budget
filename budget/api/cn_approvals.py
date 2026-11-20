"""CN approval queries and background synchronization commands."""
import base64
from decimal import Decimal

import frappe
from frappe import _

from ..services import cn_approval_sync as sync
from ..services import cn_approval_workbench as service
from ..services import cn_approval_settings as settings


@frappe.whitelist()
def get_settings():
	return settings.get_settings()


@frappe.whitelist(methods=["POST"])
def save_settings(enabled, templates, company_mappings, expected_config_version, management_rules=None):
	return settings.save_settings(enabled, templates, company_mappings, expected_config_version, management_rules)


@frappe.whitelist(methods=["POST"])
def preview_template(template):
	return settings.preview_template(template)


@frappe.whitelist()
def get_unmapped_sources(start=0):
	return settings.get_unmapped_sources(start)


@frappe.whitelist()
def get_unmapped_categories(start=0):
	return settings.get_unmapped_categories(start)


@frappe.whitelist()
def get_management(filters=None):
	from ..services import cn_approval_management as management
	service.require_reader()
	return management.overview(filters)


@frappe.whitelist()
def get_management_lines(filters=None, start=0):
	from ..services import cn_approval_management as management
	service.require_reader()
	return management.details(filters, start)


@frappe.whitelist()
def get_management_detail(key, reporting_currency="CNY"):
	from ..services import cn_approval_management as management
	service.require_reader()
	return management.detail(key, reporting_currency)


@frappe.whitelist()
def get_management_report(report_id="company", filters=None):
	from ..services import cn_approval_management as management
	service.require_reader()
	return management.report_data(report_id, filters)


@frappe.whitelist()
def export_management_report(report_id="company", filters=None):
	from ..services import cn_approval_management as management
	from frappe.utils.xlsxutils import make_xlsx
	service.require_reader()
	report = management.report_data(report_id, filters)
	if not report["has_snapshot"]:
		frappe.throw(_("No local approval snapshot is available yet. Wait for synchronization before exporting."))
	if len(report["rows"]) > 20000:
		frappe.throw(_("CN report export exceeds 20000 rows. Narrow the period or company."))
	f = report["filters"]
	data = [[_(report["title"])], [_("Month Range"), f["from_month"], f["to_month"]],
		[_("Subsidiary"), f["company"] or _("All Subsidiaries")], [_("Reporting Currency"), f["reporting_currency"]],
		[_("Data updated at"), report["fetched_at"]],
		[_("Management category"), _(report["categories"].get(f["management_category"], "Unclassified")) if f["management_category"] else _("All")],
		[_("Missing historical templates"), report.get("missing_history_templates", 0)], [_("Latest approved budget per company and month; all approved operating and purchase applications occupy the budget.")],
		[_("Approval does not confirm payment")],
		[_("Unassigned approvals"), report["quality"]["unassigned"]],
		[_("Unclassified approvals"), report["quality"]["unclassified"]], [],
		[_(column["label"]) for column in report["columns"]]]
	for row in report["rows"]:
		values = []
		for column in report["columns"]:
			key, fieldtype = column["fieldname"], column["fieldtype"]
			value = row.get(key)
			if fieldtype in ("Currency", "Percent"):
				value = excel_amount(value) if value is not None else _("Not available")
			elif key == "management_category":
				value = _(report["categories"].get(value, "Unclassified"))
			elif key == "company" and not value:
				value = _("Company not matched")
			values.append(value)
		data.append(values)
	data = [["'" + value if isinstance(value, str) and value.startswith(("=", "+", "-", "@")) else value for value in line] for line in data]
	return {"filename": f"cn-{report_id}-{f['from_month']}-{f['to_month']}.xlsx",
		"content": base64.b64encode(make_xlsx(data, "Data Export").getvalue()).decode()}


@frappe.whitelist()
def export_management_lines(filters=None):
	from ..services import cn_approval_management as management
	from frappe.utils.xlsxutils import make_xlsx
	service.require_reader()
	snapshot, rows, _context, _rules, f = management.query(filters, details=True)
	if snapshot.get("has_snapshot") is False:
		frappe.throw(_("No local approval snapshot is available yet. Wait for synchronization before exporting."))
	if len(rows) > 20000:
		frappe.throw(_("CN report export exceeds 20000 rows. Narrow the period or company."))
	headers = ("Approval number", "Approval form", "Company", "Source organization", "Reporting month", "Management category",
		"Detail item", "Approval status", "Original amount", "Currency", "Converted amount", "Reporting Currency", "Budget version", "Review issues")
	kinds = {"budget": _("Budget declaration"), "operating": _("Operating expenditure"), "purchase": _("Purchase expenditure")}
	statuses = {key: _(label) for key, label in zip(service.STATUSES, ("Approved", "In progress", "Rejected", "Terminated", "Withdrawn", "Unknown"))}
	versions = {"effective": _("Effective budget"), "superseded": _("Superseded budget"), "unresolved": _("Needs review")}
	data = [[_(key) for key in headers]]
	for row in rows:
		data.append([row["number"], kinds[row["kind"]], row["company"] or _("Company not matched"), row["entity"], row["month"],
			_(management.rules_service.CATEGORIES.get(row["management_category"], "Unclassified")), row["line_label"], statuses[row["status"]],
			excel_amount(row["amount"]), row["currency"], excel_amount(row["converted_amount"]), f["reporting_currency"],
			versions.get(row["budget_state"], ""), "; ".join(_(label) for issue in row["issues"] if (label := {**service.ISSUES, **management.ISSUES}.get(issue)))])
	data = [["'" + value if isinstance(value, str) and value.startswith(("=", "+", "-", "@")) else value for value in line] for line in data]
	return {"filename": "cn-management-lines.xlsx", "content": base64.b64encode(make_xlsx(data, "Data Export").getvalue()).decode()}


@frappe.whitelist()
def get_filter_options():
	return service.filter_options()


@frappe.whitelist()
def get_data(filters=None, start=0):
	service.require_reader()
	return service.overview(filters, start)


@frappe.whitelist(methods=["POST"])
def refresh_data(filters=None):
	service.require_reader()
	return service.overview(filters, refresh=True)


@frappe.whitelist(methods=["POST"])
def enqueue_sync(if_stale=False):
	service.require_reader()
	# Form-encoded requests carry booleans as "true"/"false" strings. Retain
	# native JSON booleans and 1/0 compatibility without turning true into 0.
	if isinstance(if_stale, str):
		if_stale = frappe.utils.sbool(if_stale.strip())
	return sync.request_sync(if_stale=bool(if_stale))


@frappe.whitelist()
def get_sync_status():
	service.require_reader()
	return sync.get_status()


@frappe.whitelist()
def get_detail(key, reporting_currency=None):
	service.require_reader()
	return service.detail(key, reporting_currency)


def excel_amount(value):
	if value is None:
		return None
	amount = Decimal(value)
	# Excel numeric cells retain only 15 significant digits. Preserve cents in
	# large converted amounts as literal decimal text instead of rounding them.
	return str(amount) if abs(amount) >= Decimal("10000000000000") else amount


@frappe.whitelist()
def export_data(filters=None):
	frappe.only_for("System Manager")
	from frappe.utils.xlsxutils import make_xlsx
	f = service.filters(filters)
	_snapshot, rows, context = service.load()
	if _snapshot.get("has_snapshot") is False:
		frappe.throw(_("No local approval snapshot is available yet. Wait for synchronization before exporting."))
	rows = service.prepared_rows(rows, f, context)
	if len(rows) > 10000:
		frappe.throw(_("Export exceeds 10000 approvals. Narrow the filters."))
	headers = ("Approval number", "Approval form", "Source organization", "Company", "Application date", "Reporting month",
		"Approval status", "Currency", "Application amount", "Business classification", "Review issues")
	if f["reporting_currency"]:
		headers += ("Reporting Currency", "Converted amount", "Exchange rate", "Exchange rate date", "Exchange rate basis")
	kinds = {"budget": _("Budget declaration"), "operating": _("Operating expenditure"), "purchase": _("Purchase expenditure")}
	statuses = {key: _(label) for key, label in zip(service.STATUSES, ("Approved", "In progress", "Rejected", "Terminated", "Withdrawn", "Unknown"))}
	data = [[_(key) for key in headers]]
	for row in rows:
		data.append([row["number"], kinds[row["kind"]], row["entity"], row["company"], row["application_date"], row["month"],
			statuses[row["status"]], row["currency"], excel_amount(row["amount"]), row["category"],
			"; ".join(_(service.ISSUES[issue]) for issue in row["issues"])])
		if f["reporting_currency"]:
			data[-1].extend([row["reporting_currency"], excel_amount(row["converted_amount"]), row["exchange_rate"],
				row["exchange_rate_date"], _(service.fx.DATE_BASES[row["fx_date_basis"]])])
	# Keep source strings literal; approval numbers and source labels are untrusted.
	data = [["'" + value if isinstance(value, str) and value.startswith(("=", "+", "-", "@")) else value for value in line] for line in data]
	# Frappe's Data Export mode preserves literal source strings instead of
	# stripping HTML after formula protection (which could expose a leading '=').
	return {"filename": "cn-approvals.xlsx", "content": base64.b64encode(make_xlsx(data, "Data Export").getvalue()).decode()}
