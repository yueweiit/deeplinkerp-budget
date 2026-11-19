"""站点级入口：预算模块的导航挂载与还原。

ERPNext 的 ``Workspace Sidebar`` / ``Desktop Icon`` 都是 standard 记录，
``bench migrate`` 会按 erpnext 目录里的 json 重新同步，所以这里用
``after_migrate`` 钩子重放；``before_uninstall`` 里再还原，保证卸载本 app
后导航回到 ERPNext 原生形态，不会留下指向已删 DocType/Page 的死链接。
定制入口在 China Operating Expenses 中维护，Budget 仅清理本 app 的旧挂载。

导航迁移使用原生 metadata import 和直接 db 写入，避免 developer_mode
把站点调整导回 ERPNext 源码目录。
"""

from __future__ import annotations

import frappe

MODULE = "Budget"
APP = "budget"

# ---------------------------------------------------------------- 侧边栏
SIDEBAR = "Budget"
GROUP_LABEL = "自定义"

# ERPNext 原生（恢复旧版本对这五个入口的分组调整）
GROUPED_LABELS = (
	"Budget",
	"Cost Center",
	"Accounting Dimension",
	"Cost Center Allocation",
	"Budget Variance",
)

# 本 app 追加的入口
ITEMS = [
	{
		"label": "预算台账",
		"link_type": "DocType",
		"link_to": "Budget Ledger Entry",
		"icon": "list",
	},
	{
		"label": "预算数据看板",
		"link_type": "Page",
		"link_to": "budget-dashboard",
		"icon": "dashboard",
	},
	{
		"label": "预算配置",
		"link_type": "Page",
		"link_to": "budget-config",
		"icon": "setting",
	},
]

# ------------------------------------------------------------ 桌面导航项
DESKTOP_ICON = "Budget"
DESKTOP_ICON_NATIVE_PARENT = "Accounting"
DESKTOP_ICON_NATIVE_IDX = 6

CN_PAGES = (
	("Expense Overview", "china-expenses", "layout-dashboard", 0),
	("Expense Details", "china-expense-details", "list", 0),
	("Expenditure Analysis", "china-expense-reports", "", 1),
	("Personnel Analysis", "china-personnel-report", "", 1),
	("Budget Execution Analysis", "budget-dashboard", "", 1),
	("Data Review", "china-expense-review", "clipboard-check", 0),
)


def cn_navigation_items(has_page):
	"""Add the regional layout only when its optional branding Pages are installed."""
	if not all(has_page(route) for _, route, _, _ in CN_PAGES):
		return []
	items = []
	for label, route, icon, child in CN_PAGES:
		if route == "china-expense-reports":
			items.append(dict(type="Section Break", label="Expense Reports", icon="chart-no-axes-combined",
				child=0, indent=1, collapsible=1, keep_closed=1))
		items.append(dict(type="Link", label=label, link_type="Page", link_to=route, icon=icon, child=child))
	return items


# ------------------------------------------------------------------ 工具
def _set(sidebar_item: str, values: dict) -> None:
	frappe.db.set_value("Workspace Sidebar Item", sidebar_item, values, update_modified=False)


def _get_items() -> list[dict]:
	return frappe.get_all(
		"Workspace Sidebar Item",
		filters={"parent": SIDEBAR, "parentfield": "items"},
		fields=["name", "label", "type", "link_type", "link_to", "icon", "child", "idx"],
		order_by="idx asc",
		limit_page_length=0,
	)


def _delete_items(names: list[str]) -> None:
	for name in names:
		frappe.db.delete("Workspace Sidebar Item", {"name": name})


def _reorder(rows: list[dict]) -> None:
	for position, row in enumerate(rows, start=1):
		if int(row["idx"] or 0) != position:
			_set(row["name"], {"idx": position})


def _group_rows(rows: list[dict]) -> list[dict]:
	return [row for row in rows if row["type"] == "Section Break" and row["label"] == GROUP_LABEL]


def ensure_module_def() -> None:
	"""Page 的 module 是 Link 字段，先保证 Module Def 存在（装的早的站点可能没有）。"""
	if frappe.db.exists("Module Def", MODULE):
		return
	# 走 db_insert 跳过 ModuleDef.on_update：它会往 app 的 modules.txt 追加内容，
	# 在容器里挂载目录权限不足时会直接失败。
	frappe.get_doc(
		{
			"doctype": "Module Def",
			"module_name": MODULE,
			"app_name": APP,
			"custom": 0,
		}
	).db_insert()


# ---------------------------------------------------------------- 安装态
def apply_budget_nav() -> None:
	"""Keep ERPNext Budget native; custom budget pages belong to CN operations."""
	ensure_module_def()
	restore_desktop_icon_position()
	restore_budget_sidebar()
	# Standard metadata is imported explicitly for install-app as well as migrate.
	from frappe.modules.import_file import import_file_by_path
	for folder in ("workspace_sidebar", "desktop_icon"):
		import_file_by_path(frappe.get_app_path(APP, folder, "china_operating_expenses.json"), force=True)
	# Existing CN pages are optional; standalone budget installations keep their existing links.
	items = cn_navigation_items(lambda route: frappe.db.exists("Page", route))
	regional_layout = bool(items)
	if not items and frappe.db.exists("Page", "operating-expenses"):
		items = [dict(type="Link", label="Operating Expenses", link_type="Page",
			link_to="operating-expenses", icon="receipt-text", child=0)]
	if items:
		sidebar = frappe.get_doc("Workspace Sidebar", "China Operating Expenses")
		for item in sidebar.items:
			if regional_layout:
				frappe.db.delete("Workspace Sidebar Item", {"name": item.name})
			else:
				_set(item.name, {"idx": item.idx + len(items), "child": 0})
		for idx, item in enumerate(items, 1):
			frappe.get_doc({"doctype": "Workspace Sidebar Item", "parent": sidebar.name,
				"parenttype": "Workspace Sidebar", "parentfield": "items", "idx": idx, **item}).db_insert()
	if regional_layout and "deeplinkerp_branding" in frappe.get_installed_apps():
		# Also reconcile after first installation: branding may migrate before
		# this optional sidebar exists. Standalone budget keeps its fallback.
		from deeplinkerp_branding.operating_navigation import ensure_operating_navigation
		ensure_operating_navigation()
	frappe.clear_cache()


# ---------------------------------------------------------------- 卸载态
def restore_budget_nav() -> None:
	"""卸载前还原成 ERPNext 原生导航，避免留下死链接。"""
	try:
		restore_desktop_icon_position()
		restore_budget_sidebar()
		for doctype in ("Desktop Icon", "Workspace Sidebar"):
			if frappe.db.exists(doctype, "China Operating Expenses"):
				developer_mode = frappe.conf.developer_mode
				try:
					frappe.conf.developer_mode = False
					frappe.delete_doc(doctype, "China Operating Expenses", ignore_permissions=True)
				finally:
					frappe.conf.developer_mode = developer_mode
		if "deeplinkerp_branding" in frappe.get_installed_apps():
			# Restore the surviving app's fallback without requiring migrate.
			from deeplinkerp_branding.operating_navigation import ensure_operating_navigation
			ensure_operating_navigation()
	except Exception:
		frappe.log_error(title="Budget 导航还原失败", message=frappe.get_traceback())
	finally:
		frappe.db.commit()
		frappe.clear_cache()


def restore_budget_sidebar() -> None:
	if not frappe.db.exists("Workspace Sidebar", SIDEBAR):
		return

	rows = _get_items()

	# 1) 删掉本 app 追加的入口和分组标题
	custom_labels = {item["label"] for item in ITEMS}
	to_delete = [row["name"] for row in rows if row["label"] in custom_labels]
	to_delete += [row["name"] for row in _group_rows(rows)]
	_delete_items(to_delete)

	# 2) 原生入口恢复成顶级
	rows = _get_items()
	by_label = {row["label"]: row for row in rows}
	native = []
	for label in GROUPED_LABELS:
		row = by_label.get(label)
		if not row:
			continue
		if row["child"]:
			_set(row["name"], {"child": 0})
		native.append(row)

	rest = [row for row in rows if row["name"] not in {item["name"] for item in native}]
	_reorder([*native, *rest])
	frappe.db.set_value(
		"Workspace Sidebar", SIDEBAR, "modified", frappe.utils.now(), update_modified=False
	)


def restore_desktop_icon_position() -> None:
	if not frappe.db.exists("Desktop Icon", DESKTOP_ICON):
		return
	frappe.db.set_value(
		"Desktop Icon",
		DESKTOP_ICON,
		{"parent_icon": DESKTOP_ICON_NATIVE_PARENT, "idx": DESKTOP_ICON_NATIVE_IDX},
		update_modified=False,
	)
	frappe.cache.delete_key("desktop_icons")


def boot_session(bootinfo):
	bootinfo.cn_budget_workbench = ("deeplinkerp_branding" in frappe.get_installed_apps()
		and bool(cn_navigation_items(lambda route: frappe.db.exists("Page", route))))
