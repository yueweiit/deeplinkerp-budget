"""站点级入口：把预算台账挂到 ERPNext 会计的「预算」侧边栏下。

ERPNext 的 ``Workspace Sidebar`` 是 standard 记录，``bench migrate`` 会按
``erpnext/workspace_sidebar/*.json`` 重新同步，所以这里用 ``after_migrate``
钩子重放，而不是手工改一次数据库了事。

直接 ``db_insert`` 子表行、不调用 ``doc.save()``，是为了绕开
``WorkspaceSidebar.export_sidebar``——它在 developer_mode 下会把改动写回
erpnext 源码目录。
"""

from __future__ import annotations

import frappe

MODULE = "Budget"
APP = "budget"
SIDEBAR = "Budget"

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

# 早期版本挂过、现在要下掉的入口。
REMOVED_LABELS = ()


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


def _upsert_item(item: dict, index: int) -> None:
	existing = frappe.db.get_value(
		"Workspace Sidebar Item", {"parent": SIDEBAR, "label": item["label"]}, "name"
	)
	values = {
		"type": "Link",
		"link_type": item["link_type"],
		"link_to": item["link_to"],
		"url": None,
		"icon": item["icon"],
	}
	if existing:
		frappe.db.set_value("Workspace Sidebar Item", existing, values, update_modified=False)
		return
	frappe.get_doc(
		{
			"doctype": "Workspace Sidebar Item",
			"parent": SIDEBAR,
			"parentfield": "items",
			"parenttype": "Workspace Sidebar",
			**values,
			"label": item["label"],
			"idx": index,
		}
	).db_insert()


def _ensure_sidebar_record() -> bool:
	"""侧边栏记录由 erpnext 的 standard json 同步；缺失时（新站点没 migrate 过）补一个 custom 的。

	``standard=0`` 是刻意的：``WorkspaceSidebar.before_save`` 只在 standard + developer_mode
	时把记录写回源码目录，custom 记录不会污染 erpnext 仓库。
	"""
	if frappe.db.exists("Workspace Sidebar", SIDEBAR):
		return True
	try:
		frappe.get_doc(
			{
				"doctype": "Workspace Sidebar",
				"title": SIDEBAR,
				"app": APP,
				"module": MODULE,
				"header_icon": "accounting",
				"standard": 0,
			}
		).insert(ignore_permissions=True)
	except Exception:  # 补入口失败不该中断 migrate
		frappe.log_error(
			title="Budget sidebar 创建失败", message=frappe.get_traceback()
		)
		return False
	return True


def ensure_budget_sidebar_link() -> None:
	"""幂等地补齐侧边栏入口；重复 migrate 也不会把下掉的入口加回来。"""
	ensure_module_def()
	if not _ensure_sidebar_record():
		return

	for label in REMOVED_LABELS:
		frappe.db.delete("Workspace Sidebar Item", {"parent": SIDEBAR, "label": label})

	last_idx = int(
		frappe.db.sql(
			"SELECT COALESCE(MAX(idx), 0) FROM `tabWorkspace Sidebar Item` WHERE parent = %s",
			SIDEBAR,
		)[0][0]
	)
	for offset, item in enumerate(ITEMS, start=1):
		_upsert_item(item, last_idx + offset)

	frappe.db.set_value(
		"Workspace Sidebar",
		SIDEBAR,
		"modified",
		frappe.utils.now(),
		update_modified=False,
	)
	frappe.clear_cache()
