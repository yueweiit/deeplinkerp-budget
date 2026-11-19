"""预算台账的本地读取源（MariaDB）。

与 ``BudgetLedgerSource``（PostgreSQL 直连）同名同签名，数据来自站点自己的
``Budget Ledger Entry`` / ``Budget Ledger Detail``，由 ``oa_budget_sync`` 从
钉钉 OA 归档同步而来。

页面因此不再依赖任何外部数据库连接。
"""

from __future__ import annotations

import json

import frappe

ENTRY_DOCTYPE = "Budget Ledger Entry"
DETAIL_DOCTYPE = "Budget Ledger Detail"

PRODUCTION = "production"
NON_PRODUCTION = "non_production"

EXPENSE_GROUPS = ("hr", "office", "operation")
PRODUCTION_GROUPS = ("material", "production", "labor")

_DETAIL_COLUMNS = (
	"detail_group",
	"detail_type",
	"detail_code",
	"detail_item",
	"detail_category",
	"budget_purpose_detail",
	"operation_expense",
	"budget_detail",
	"headcount",
	"original_amount",
	"currency",
	"exchange_rate",
	"rmb_amount",
	"amount",
	"calculation_basis",
	"remark",
	"production_line",
	"item_name",
	"specification",
	"product_name",
	"post_name",
	"work_type",
	"unit",
	"quantity",
	"unit_price",
	"overtime_hours",
	"overtime_unit_price",
	"estimated_overtime_amount",
	"row_index",
)


def _month_start(value: str | None) -> str | None:
	"""``2026-11`` → ``2026-11-01``，保持与 Date 字段可直接比较。"""
	text = str(value or "").strip()
	if not text:
		return None
	return f"{text}-01" if len(text) == 7 and text[4] == "-" else text


def _json_list(value) -> list:
	if isinstance(value, list):
		return value
	if isinstance(value, str) and value.strip():
		try:
			parsed = json.loads(value)
		except ValueError:
			return []
		return parsed if isinstance(parsed, list) else []
	return []


def _row_to_api(row: dict) -> dict:
	"""把本地表行转成与 PG 版一致的字段名，业务层无需感知来源。"""
	amount = float((row.get("total_amount") if row.get("total_amount") is not None else row.get("amount")) or 0)
	return {
		"id": row.get("name"),
		"form_no": row.get("form_no"),
		"process_instance_id": row.get("process_instance_id"),
		"dept_name": row.get("dept_name"),
		"dept_id": row.get("department_id"),
		"dept_path_ids": _json_list(row.get("dept_path_ids")),
		"dept_path_names": _json_list(row.get("dept_path_names")),
		"budget_type": row.get("budget_kind"),
		"declaration_month": row.get("declaration_month"),
		"budget_month": str(row.get("budget_month") or "")[:7],
		"application_date": str(row.get("application_date") or "") or None,
		"execution_region": row.get("execution_region"),
		"creator_name": row.get("creator_name"),
		"create_time": str(row.get("source_created_at") or "") or None,
		"status": row.get("status"),
		"budget_kind": row.get("budget_kind"),
		"budget_amount": amount,
		"total_amount": amount,
		"budget_month_key": str(row.get("budget_month") or "")[:7],
		"dept_key": row.get("dept_key"),
		"company_display": row.get("company") or "待确认",
		"department_display": row.get("department") or "",
		"budget_kind_label": "生产预算" if row.get("budget_kind") == PRODUCTION else "非生产预算",
		"amount": amount,
	}


class BudgetLedgerLocalSource:
	"""``BudgetLedgerSource`` 的 MariaDB 版本。"""

	def _filters(self, *, budget_type, start_month, end_month, status, keyword) -> tuple[str, dict]:
		conditions = ["1=1"]
		values: dict = {}

		if budget_type in (PRODUCTION, NON_PRODUCTION):
			conditions.append("budget_kind = %(budget_type)s")
			values["budget_type"] = budget_type
		if start_month:
			conditions.append("budget_month >= %(start_month)s")
			values["start_month"] = _month_start(start_month)
		if end_month:
			conditions.append("budget_month <= %(end_month)s")
			values["end_month"] = _month_start(end_month)
		if status:
			conditions.append("status = %(status)s")
			values["status"] = status
		if keyword:
			conditions.append(
				"(form_no LIKE %(kw)s OR department LIKE %(kw)s OR creator_name LIKE %(kw)s"
				" OR execution_region LIKE %(kw)s)"
			)
			values["kw"] = f"%{keyword}%"
		return " AND ".join(conditions), values

	def list_budget_entries(
		self,
		*,
		budget_type: str | None = None,
		start_month: str | None = None,
		end_month: str | None = None,
		status: str | None = None,
		keyword: str | None = None,
		page: int = 1,
		page_size: int = 10,
	) -> dict:
		page = max(int(page or 1), 1)
		page_size = min(max(int(page_size or 10), 1), 100)
		where, values = self._filters(
			budget_type=budget_type,
			start_month=start_month,
			end_month=end_month,
			status=status,
			keyword=keyword,
		)

		counts: dict[str, int] = {PRODUCTION: 0, NON_PRODUCTION: 0}
		amounts: dict[str, float] = {PRODUCTION: 0.0, NON_PRODUCTION: 0.0}
		for row in frappe.db.sql(
			f"""SELECT budget_kind, COUNT(*) AS total,
			           COALESCE(SUM(COALESCE(total_amount, amount, 0)), 0) AS amount
			      FROM `tab{ENTRY_DOCTYPE}`
			     WHERE {where}
			     GROUP BY budget_kind""",
			values,
			as_dict=True,
		):
			kind = row.get("budget_kind")
			if kind in counts:
				counts[kind] = int(row.get("total") or 0)
				amounts[kind] = float(row.get("amount") or 0)
		counts["all"] = counts[PRODUCTION] + counts[NON_PRODUCTION]
		amounts["all"] = amounts[PRODUCTION] + amounts[NON_PRODUCTION]

		rows = frappe.db.sql(
			f"""SELECT name, form_no, process_instance_id, company, department,
			           department_id, creator_name, dept_name, dept_key, budget_kind,
			           application_date, budget_month, declaration_month, execution_region,
			           status, amount, detail_amount, total_amount, remark,
			           dept_path_ids, dept_path_names, oa_process_code, source_created_at
			      FROM `tab{ENTRY_DOCTYPE}`
			     WHERE {where}
			     ORDER BY source_created_at DESC, name DESC
			     LIMIT %(limit)s OFFSET %(offset)s""",
			{**values, "limit": page_size, "offset": (page - 1) * page_size},
			as_dict=True,
		)

		active = budget_type if budget_type in (PRODUCTION, NON_PRODUCTION) else "all"
		return {
			"rows": [_row_to_api(row) for row in rows],
			"total": counts.get(active, 0),
			"counts": counts,
			"total_amount": round(amounts.get(active, 0), 2),
			"page": page,
			"page_size": page_size,
		}

	def get_stats(self) -> dict:
		"""四张卡：今日生产 / 今日非生产 / 生产总数 / 非生产总数。"""
		rows = frappe.db.sql(
			f"""SELECT budget_kind,
			           COUNT(*) AS total,
			           SUM(CASE WHEN DATE(source_created_at) = CURDATE() THEN 1 ELSE 0 END) AS today
			      FROM `tab{ENTRY_DOCTYPE}`
			     GROUP BY budget_kind""",
			as_dict=True,
		)
		stats = {
			"production_today": 0,
			"non_production_today": 0,
			"production_total": 0,
			"non_production_total": 0,
		}
		for row in rows:
			if row.get("budget_kind") == PRODUCTION:
				stats["production_today"] = int(row.get("today") or 0)
				stats["production_total"] = int(row.get("total") or 0)
			elif row.get("budget_kind") == NON_PRODUCTION:
				stats["non_production_today"] = int(row.get("today") or 0)
				stats["non_production_total"] = int(row.get("total") or 0)
		return stats

	def _detail_rows(self, form_nos: list[str], groups: tuple[str, ...]) -> list[dict]:
		names = [str(name).strip() for name in (form_nos or []) if str(name or "").strip()]
		if not names:
			return []
		rows = frappe.db.sql(
			f"""SELECT name AS id, form_no, {", ".join(_DETAIL_COLUMNS)},
			           production_process AS process
			      FROM `tab{DETAIL_DOCTYPE}`
			     WHERE form_no IN %(names)s AND detail_group IN %(groups)s
			     ORDER BY form_no, detail_group, row_index""",
			{"names": names, "groups": list(groups)},
			as_dict=True,
		)
		return rows

	def list_expense_details(self, form_nos: list[str]) -> list[dict]:
		return self._detail_rows(form_nos, EXPENSE_GROUPS)

	def list_production_details(self, form_nos: list[str]) -> list[dict]:
		return self._detail_rows(form_nos, PRODUCTION_GROUPS)

	def load_remarks(self, form_nos: list[str]) -> dict[str, str]:
		names = [str(name).strip() for name in (form_nos or []) if str(name or "").strip()]
		if not names:
			return {}
		rows = frappe.db.sql(
			f"""SELECT form_no, remark FROM `tab{ENTRY_DOCTYPE}`
			     WHERE form_no IN %(names)s AND COALESCE(remark, '') != ''""",
			{"names": names},
			as_dict=True,
		)
		return {str(row.get("form_no")): str(row.get("remark")) for row in rows}

	def count_detail_rows(
		self, start_month: str | None = None, end_month: str | None = None, company: str | None = None
	) -> dict[str, int]:
		conditions = ["1=1"]
		values: dict = {}
		if company:
			conditions.append("e.company = %(company)s")
			values["company"] = company
		if start_month:
			conditions.append("e.budget_month >= %(start_month)s")
			values["start_month"] = _month_start(start_month)
		if end_month:
			conditions.append("e.budget_month <= %(end_month)s")
			values["end_month"] = _month_start(end_month)

		rows = frappe.db.sql(
			f"""SELECT d.detail_group, COUNT(*) AS total
			      FROM `tab{DETAIL_DOCTYPE}` d
			      JOIN `tab{ENTRY_DOCTYPE}` e ON e.form_no = d.form_no
			     WHERE {" AND ".join(conditions)}
			     GROUP BY d.detail_group""",
			values,
			as_dict=True,
		)
		counts = {PRODUCTION: 0, NON_PRODUCTION: 0}
		for row in rows:
			group = row.get("detail_group")
			if group in PRODUCTION_GROUPS:
				counts[PRODUCTION] += int(row.get("total") or 0)
			elif group in EXPENSE_GROUPS:
				counts[NON_PRODUCTION] += int(row.get("total") or 0)
		return counts
