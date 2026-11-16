"""把外部 ``budget_system`` 库的预算单同步成 ERP 的 ``Budget Ledger Entry``。

为什么要落库：ERP 的列表、筛选、排序、导出、权限都建立在站点库的表上，
外部 PostgreSQL 对 Frappe 来说只是数据源，不落库就用不上这些原生能力。
做法对齐 china_finance 的银行流水：外部数据 → 导入/同步 → 本地 DocType。
"""

from __future__ import annotations

from typing import Any

import frappe

from ..integrations.budget_factory import get_ledger_source

DOCTYPE = "Budget Ledger Entry"
BATCH_SIZE = 100


def _fetch_all_rows(source: Any) -> list[dict]:
	"""分页拉全量；去重口径由数据源保证（部门 + 预算月份取最新一单）。"""
	rows: list[dict] = []
	page = 1
	while True:
		data = source.list_budget_entries(page=page, page_size=BATCH_SIZE)
		rows.extend(data.get("rows") or [])
		if len(rows) >= int(data.get("total") or 0):
			break
		if not data.get("rows"):
			break
		page += 1
	return rows


def _normalize_month(value) -> str | None:
	"""``budget_month`` 在 DocType 里是 Date（为了拿到原生日历筛选），而数据源只给到月份。

	所以统一落到当月 1 号；已经是完整日期的值原样保留。
	"""
	text = str(value or "").strip()
	if not text:
		return None
	if len(text) == 7 and text[4] == "-":
		return f"{text}-01"
	return text


def _values(row: dict, synced_at: str) -> dict:
	return {
		"form_no": row.get("form_no"),
		"process_instance_id": row.get("process_instance_id"),
		"company": row.get("company_display"),
		"department": row.get("department_display") or row.get("dept_name"),
		"department_id": row.get("dept_id"),
		"budget_kind": row.get("budget_kind"),
		"application_date": row.get("application_date"),
		"budget_month": _normalize_month(row.get("budget_month") or row.get("declaration_month")),
		"execution_region": row.get("execution_region"),
		"status": row.get("status"),
		"amount": float(row.get("total_amount") or row.get("budget_amount") or 0),
		"synced_at": synced_at,
	}


def sync_budget_entries(*, source: Any | None = None) -> dict:
	"""同步入口（保持函数名不变，兼容 hooks 与 whitelist）。

	默认改为「OA 归档 → 解析 → 增量写本地表」，直接复用外部调度器入口
	``oa_budget_sync.scheduled_sync``：``hooks.py`` 的 ``daily_long`` 与外部 cron
	走完全同一条链路（含每日一次整表对账）。传入 ``source`` 时仍走旧的
	「数据源 → 本地表」路径，仅供单测注入替身。
	"""
	if source is None:
		from .oa_budget_sync import scheduled_sync

		return scheduled_sync()

	# 以下分支仅供单测注入替身：直接 commit、不写运行记录、不做写后校验，
	# 生产请一律走上面的 scheduled_sync。
	rows = _fetch_all_rows(source)
	synced_at = frappe.utils.now()

	created = 0
	updated = 0
	names: list[str] = []

	for row in rows:
		form_no = (row.get("form_no") or "").strip()
		if not form_no:
			continue
		values = _values(row, synced_at)
		if frappe.db.exists(DOCTYPE, form_no):
			frappe.db.set_value(DOCTYPE, form_no, values, update_modified=False)
			updated += 1
		else:
			frappe.get_doc({"doctype": DOCTYPE, **values}).insert(
				ignore_permissions=True, ignore_mandatory=True
			)
			created += 1
		names.append(form_no)

	removed = 0
	if names:
		stale = frappe.get_all(DOCTYPE, filters={"form_no": ["not in", names]}, pluck="name")
		for name in stale:
			frappe.delete_doc(DOCTYPE, name, ignore_permissions=True, force=True)
			removed += 1

	frappe.db.commit()
	return {
		"remote_total": len(rows),
		"created": created,
		"updated": updated,
		"removed": removed,
		"local_total": frappe.db.count(DOCTYPE),
	}
