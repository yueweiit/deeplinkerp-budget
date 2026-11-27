"""Limit dedicated API users to bank journal imports and their assigned companies."""

import frappe

from budget.services.cn_approval_workbench import allowed_companies

IMPORT_ROLE = "Bank Journal Import API"
METHOD_PREFIX = "/api/method/budget.api."
IMPORT_ROUTES = {
	("GET", METHOD_PREFIX + "bank_journal.get_options"),
	("GET", METHOD_PREFIX + "bank_journal.get_month"),
	("POST", METHOD_PREFIX + "bank_journal_files.preview_import"),
	("POST", METHOD_PREFIX + "bank_journal_files.apply_import"),
	("POST", METHOD_PREFIX + "bank_journal_files.preview_rows"),
	("POST", METHOD_PREFIX + "bank_journal_files.apply_rows"),
}


def is_import_user():
	user = frappe.session.user
	return bool(user and user not in ("Guest", "Administrator") and IMPORT_ROLE in frappe.get_roles(user))


def restrict_import_request():
	if not is_import_user():
		return
	if (
		"cmd" in frappe.form_dict
		or (frappe.request.method, frappe.request.path) not in IMPORT_ROUTES
		or frappe.get_request_header("Authorization", "").partition(" ")[0].lower() != "token"
	):
		frappe.throw("此接入账号只能调用银行流水导入接口。", frappe.PermissionError)


def journal_companies():
	visible = allowed_companies()
	if not is_import_user():
		return visible
	# Native User Permission treats an empty grant list as unrestricted.
	# This account requires an explicit grant for each legal entity.
	grants = frappe.get_all(
		"User Permission",
		filters={
			"user": frappe.session.user,
			"allow": "Company",
			"apply_to_all_doctypes": 1,
			"hide_descendants": 1,
		},
		pluck="for_value",
	)
	return visible.intersection(grants)
