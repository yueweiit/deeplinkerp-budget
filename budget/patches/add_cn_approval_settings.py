"""Seed independent CN settings without contacting OA or changing its scope."""
import frappe


def execute():
	for name in ("cn_approval_template", "cn_approval_company_map", "cn_approval_settings"):
		frappe.reload_doc("budget", "doctype", name)
	from budget.services import cn_approval_settings as settings
	if settings.read_saved() is not None or "deeplinkerp_branding" not in frappe.get_installed_apps():
		return
	from budget.integrations.shared_operating_oa import client
	try:
		_url, corps = client().configuration()
	except frappe.ValidationError:
		# An independent Budget install need not have an OA connection yet.
		return
	settings.persist(settings.defaults(corps))
