"""Add rebuildable query storage without changing original approval evidence."""
import frappe


def execute():
	for doctype in ("expense_report_state", "expense_report_line"):
		frappe.reload_doc("budget", "doctype", doctype)
	setup()


def setup():
	from budget.services import expense_report_store as store
	if not store.installed():
		return
	for fields, name in ((["reporting_month", "company", "approval_status"], "expense_month_company"),
		(["company", "reporting_month", "application_date", "name"], "expense_company_page"),
		(["reporting_month", "source_category_key"], "expense_month_category"),
		(["is_legacy", "source_key"], "expense_legacy_source")):
		frappe.db.add_index(store.LINE, fields, name)
	if not store.state():
		now = frappe.utils.now_datetime()
		frappe.db.bulk_insert(store.STATE, ["name", "creation", "modified", "owner", "modified_by"],
			[(store.CURRENT, now, now, "Administrator", "Administrator")], ignore_duplicates=True)
	if "deeplinkerp_branding" in frappe.get_installed_apps():
		from budget.services.expense_report_refresh import after_source_change
		after_source_change()
