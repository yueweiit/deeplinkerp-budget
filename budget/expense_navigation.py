"""Consolidate visible entries only after native Page authorization succeeds."""
import frappe

PAGES = ("expense-management", "expense-analysis", "expense-budget", "expense-details", "expense-review")
LEGACY = {"Mexico Expenses", "China Operating Expenses"}


def install():
	if "deeplinkerp_branding" not in frappe.get_installed_apps():
		return
	from frappe.modules.import_file import import_file_by_path
	for route in PAGES:
		owner = frappe.db.get_value("Page", route, "module")
		if owner and owner != "Budget":
			frappe.throw(frappe._("Expense management navigation conflicts with an existing page."))
		slug = route.replace("-", "_")
		import_file_by_path(frappe.get_app_path("budget", "budget", "page", slug, slug + ".json"), force=True)
	for folder in ("workspace_sidebar", "desktop_icon"):
		doctype = "Workspace Sidebar" if folder == "workspace_sidebar" else "Desktop Icon"
		owner = frappe.db.get_value(doctype, "Expense Management", "app")
		if owner and owner != "budget":
			frappe.throw(frappe._("Expense management navigation conflicts with an existing page."))
		import_file_by_path(frappe.get_app_path("budget", folder, "expense_management.json"), force=True)
	frappe.clear_cache()


def boot(bootinfo):
	pages, sidebars = bootinfo.get("page_info") or {}, bootinfo.get("workspace_sidebar_item") or {}
	ready = ("deeplinkerp_branding" in frappe.get_installed_apps()
		and all(route in pages for route in PAGES) and "expense management" in sidebars)
	bootinfo["unified_expense_management"] = ready
	icons = bootinfo.get("desktop_icons") or []
	if ready:
		bootinfo["desktop_icons"] = [icon for icon in icons if icon.get("name") not in LEGACY and icon.get("label") not in LEGACY]
	else:
		bootinfo["desktop_icons"] = [icon for icon in icons if icon.get("name") != "Expense Management"]


def uninstall():
	# Explicit uninstall lifecycle only; leave source data with its owning app.
	old = frappe.conf.developer_mode
	try:
		frappe.conf.developer_mode = False
		for doctype in ("Desktop Icon", "Workspace Sidebar"):
			if frappe.db.exists(doctype, "Expense Management"):
				frappe.delete_doc(doctype, "Expense Management", ignore_permissions=True)
	finally:
		frappe.conf.developer_mode = old
