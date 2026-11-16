"""站点级入口：预算模块的导航挂载与还原。

ERPNext 的 ``Workspace Sidebar`` / ``Desktop Icon`` 都是 standard 记录，
``bench migrate`` 会按 erpnext 目录里的 json 重新同步，所以这里用
``after_migrate`` 钩子重放；``before_uninstall`` 里再还原，保证卸载本 app
后导航回到 ERPNext 原生形态，不会留下指向已删 DocType/Page 的死链接。

所有写操作都走 ``db_insert`` / ``db_set_value``，不调用 ``doc.save()``：
后者在 developer_mode 下会把改动导回 erpnext 源码目录。
"""

from __future__ import annotations

import frappe

MODULE = "Budget"
APP = "budget"

# ---------------------------------------------------------------- 侧边栏
SIDEBAR = "Budget"
GROUP_LABEL = "自定义"

# ERPNext 原生（这 5 项收进分组里）
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
DESKTOP_ICON_ROOT_IDX = 103
DESKTOP_ICON_NATIVE_PARENT = "Accounting"
DESKTOP_ICON_NATIVE_IDX = 6


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


def _insert_item(
	label: str,
	*,
	item_type: str = "Link",
	link_type: str | None = None,
	link_to: str | None = None,
	icon: str | None = None,
	child: int = 0,
	indent: int = 0,
	collapsible: int = 0,
	keep_closed: int = 0,
	idx: int = 0,
) -> str:
	doc = frappe.get_doc(
		{
			"doctype": "Workspace Sidebar Item",
			"parent": SIDEBAR,
			"parentfield": "items",
			"parenttype": "Workspace Sidebar",
			"type": item_type,
			"label": label,
			"link_type": link_type,
			"link_to": link_to,
			"icon": icon,
			"child": child,
			"indent": indent,
			"collapsible": collapsible,
			"keep_closed": keep_closed,
			"idx": idx,
		}
	)
	doc.db_insert()
	return doc.name


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
		frappe.log_error(title="Budget sidebar 创建失败", message=frappe.get_traceback())
		return False
	return True


# ---------------------------------------------------------------- 安装态
def apply_budget_nav() -> None:
	"""把 ERPNext 原生的预算入口收进「自定义」分组，并追加本 app 的 3 个入口。"""
	ensure_module_def()
	if not _ensure_sidebar_record():
		return

	ensure_budget_sidebar_group()
	ensure_budget_sidebar_link()
	ensure_desktop_icon_position()

	frappe.db.commit()
	frappe.clear_cache()


def ensure_budget_sidebar_group() -> None:
	"""5 个 ERPNext 原生入口折叠进「自定义」分组（默认收起）。

	``collapsible`` 必须与 ``keep_closed`` 同时为 1：前端
	``enable_collapsible`` 会先判断 ``item.collapsible`` 才创建 ``$drop_icon``，
	随后才对 ``keep_closed`` 调 ``close()``。若只设 ``keep_closed``，
	``toggle()`` 会访问 undefined 的 ``$drop_icon`` 抛 TypeError，
	``create_sidebar`` 的 forEach 直接中断——表现为分组之后的侧边栏项全部消失。
	"""
	rows = _get_items()
	groups = _group_rows(rows)

	if groups:
		group_name = groups[0]["name"]
		if len(groups) > 1:
			_delete_items([row["name"] for row in groups[1:]])
		_set(
			group_name,
			{
				"type": "Section Break",
				"link_type": "DocType",
				"link_to": None,
				"icon": None,
				"child": 0,
				"indent": 1,
				"collapsible": 1,
				"keep_closed": 1,
			},
		)
	else:
		group_name = _insert_item(
			GROUP_LABEL,
			item_type="Section Break",
			link_type="DocType",
			indent=1,
			collapsible=1,
			keep_closed=1,
		)

	rows = _get_items()
	by_label = {row["label"]: row for row in rows if row["type"] == "Link"}
	grouped = []
	for label in GROUPED_LABELS:
		row = by_label.get(label)
		if not row:
			continue  # erpnext 这个入口不存在（版本差异），保持原样
		if not row["child"]:
			_set(row["name"], {"child": 1})
		grouped.append(row)

	others = [
		row
		for row in rows
		if row["name"] != group_name and row["name"] not in {item["name"] for item in grouped}
	]
	for row in others:
		if row["child"]:
			_set(row["name"], {"child": 0})

	group_row = next(row for row in rows if row["name"] == group_name)
	_reorder([group_row, *grouped, *others])
	frappe.db.set_value(
		"Workspace Sidebar", SIDEBAR, "modified", frappe.utils.now(), update_modified=False
	)


def ensure_budget_sidebar_link() -> None:
	"""幂等地补齐「预算台账 / 预算数据看板 / 预算配置」三个入口。"""
	if not frappe.db.exists("Workspace Sidebar", SIDEBAR):
		return

	rows = _get_items()
	existing = {row["label"]: row for row in rows}
	max_idx = max((int(row["idx"] or 0) for row in rows), default=0)

	for item in ITEMS:
		row = existing.get(item["label"])
		if row:
			frappe.db.set_value(
				"Workspace Sidebar Item",
				row["name"],
				{
					"type": "Link",
					"link_type": item["link_type"],
					"link_to": item["link_to"],
					"url": None,
					"icon": item["icon"],
					"child": 0,
				},
				update_modified=False,
			)
			continue
		max_idx += 1
		_insert_item(
			item["label"],
			link_type=item["link_type"],
			link_to=item["link_to"],
			icon=item["icon"],
			idx=max_idx,
		)


def ensure_desktop_icon_position() -> None:
	"""把 Desktop Icon「Budget」从「会计」里提到顶级，排在「海外成本核算」之后。"""
	if not frappe.db.exists("Desktop Icon", DESKTOP_ICON):
		return
	current = frappe.db.get_value(
		"Desktop Icon", DESKTOP_ICON, ["parent_icon", "idx"], as_dict=True
	)
	if current.parent_icon == "" and int(current.idx or 0) == DESKTOP_ICON_ROOT_IDX:
		return
	frappe.db.set_value(
		"Desktop Icon",
		DESKTOP_ICON,
		{"parent_icon": "", "idx": DESKTOP_ICON_ROOT_IDX},
		update_modified=False,
	)
	frappe.cache.delete_key("desktop_icons")


# ---------------------------------------------------------------- 卸载态
def restore_budget_nav() -> None:
	"""卸载前还原成 ERPNext 原生导航，避免留下死链接。"""
	try:
		restore_desktop_icon_position()
		restore_budget_sidebar()
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
