"""Retire regional expense pages after native unified Page authorization succeeds."""
import frappe

PAGES = ("expense-management", "expense-analysis", "expense-budget", "expense-details", "expense-review")
LEGACY = {"Mexico Expenses", "China Operating Expenses"}
LEGACY_ROUTES = {
	"china-expenses": "expense-management",
	"china-budget-execution": "expense-budget",
	"china-expense-reports": "expense-analysis",
	"china-management-reports": "expense-analysis",
	"china-personnel-report": "expense-analysis",
	"china-expense-details": "expense-details",
	"china-expense-review": "expense-review",
	"mexico-expenses": "expense-management",
	"mexico-expense-reports": "expense-analysis",
	"mexico-personnel-report": "expense-analysis",
	"mexico-personnel-expenses": "expense-analysis",
	"mexico-expense-trend": "expense-analysis",
	"mexico-expense-details": "expense-details",
	"mexico-expense-review": "expense-review",
	"budget-dashboard": "expense-budget",
	"budget-config": "expense-budget",
}


def retired_link(item):
	return (item.get("link_type") == "Page" and item.get("link_to") in LEGACY_ROUTES
		or item.get("link_type") == "Workspace Sidebar" and item.get("link_to") in LEGACY)


def visible_sidebar_items(items):
	"""Remove retired links and only the groups emptied by their removal."""
	result = []
	for index, item in enumerate(items):
		if retired_link(item):
			continue
		if item.get("type") == "Section Break":
			children = []
			for child in items[index + 1:]:
				if child.get("type") == "Section Break" or not child.get("child"):
					break
				children.append(child)
			if children and all(retired_link(child) for child in children):
				continue
		result.append(item)
	return result


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
	bootinfo["expense_legacy_routes"] = dict(LEGACY_ROUTES) if ready else {}
	icons = bootinfo.get("desktop_icons") or []
	if ready:
		# Project this user's authorized navigation; do not mutate cached metadata,
		# source documents, standalone installations or other users' permissions.
		bootinfo["page_info"] = {route: info for route, info in pages.items() if route not in LEGACY_ROUTES}
		retired = {name.lower() for name in LEGACY} | set(bootinfo.get("expense_retired_sidebars") or [])
		visible = {}
		for key, sidebar in sidebars.items():
			if key in retired:
				continue
			items = sidebar.get("items") or []
			remaining = visible_sidebar_items(items)
			# Only retire sidebars emptied by this projection. My Workspaces must
			# remain available even when empty for native personal-workspace setup.
			if key != "my workspaces" and remaining != items and not any(
				item.get("type") != "Section Break" for item in remaining
			):
				retired.add(key)
				continue
			visible[key] = {**sidebar, "items": remaining}
		bootinfo["workspace_sidebar_item"] = visible
		bootinfo["expense_retired_sidebars"] = sorted(retired)
		bootinfo["desktop_icons"] = [icon for icon in icons if icon.get("name") not in LEGACY
			and icon.get("label") not in LEGACY and not retired_link(icon)
			and not (icon.get("link_type") == "Workspace Sidebar" and icon.get("icon_type") not in ("App", "Folder")
				and any((icon.get(field) or "").lower() in retired for field in ("label", "link_to", "sidebar")))]
	else:
		bootinfo["expense_retired_sidebars"] = []
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
