"""预算额度的本地读取源（MariaDB）。

与 ``QuotaSource``（PostgreSQL 直连）同名同签名，数据来自站点自己的
``Budget Quota`` / ``Budget Quota Subject``，由共用 OA 审批同步在同一事务内生成。

口径与 PG 版逐条对齐：

- 只统计 ``status = 'effective'``，失效额度保留在表里但不进配置页；
- 组织类筛选候选只取 ``organization_is_current = 1``；
- ``total_amount`` 是唯一参与余额计算的额度，科目金额只做科目统计。
"""

from __future__ import annotations

import frappe

QUOTA_DOCTYPE = "Budget Quota"
SUBJECT_DOCTYPE = "Budget Quota Subject"

EFFECTIVE = "effective"
PENDING_NAME = "待确认"


def scope_filters(source=None):
	"""Bind all local quota reads to the server's corporation and budget template."""
	from ..services.ledger_sync_settings import current_source
	from ..services.oa_budget_parser import BUDGET_PROCESS_CODE
	source = source if source is not None else current_source()
	filters = {"corp_id": source.get("corp_id") or "", "oa_process_code": BUDGET_PROCESS_CODE}
	if (not filters["corp_id"] or (source.get("bound_fingerprint")
		and source["bound_fingerprint"] != source.get("fingerprint"))):
		# A missing configuration must not expose unbound historical rows.
		filters["name"] = ["is", "not set"]
	return filters


def scope_sql(source=None):
	values = scope_filters(source)
	where = "q.corp_id <> '' AND q.corp_id = %(corp_id)s AND q.oa_process_code = %(oa_process_code)s"
	return where + (" AND 1 = 0" if "name" in values else ""), values


def _month_start(value: str | None) -> str | None:
	"""``2026-11`` → ``2026-11-01``，与 Date 字段可直接比较。"""
	text = str(value or "").strip()
	if not text:
		return None
	return f"{text}-01" if len(text) == 7 and text[4] == "-" else text


def _name_sql(column: str) -> str:
	return f"COALESCE(NULLIF(TRIM({column}), ''), '{PENDING_NAME}')"


def _amount(value) -> float:
	return float(value or 0)


class BudgetQuotaLocalSource:
	"""``QuotaSource`` 的 MariaDB 版本。"""

	# ------------------------------------------------------------ 筛选
	def _filters(
		self,
		*,
		budget_year=None,
		budget_month=None,
		group_dept_id=None,
		company_dept_id=None,
		department_id=None,
		budget_type=None,
		subject_name=None,
		start_month=None,
		end_month=None,
		company_name=None,
		keyword=None,
	) -> tuple[str, dict]:
		scope, values = scope_sql()
		conditions = [scope, "q.status = %(status)s"]
		values["status"] = EFFECTIVE

		if str(budget_year or "").strip():
			conditions.append("q.budget_year = %(budget_year)s")
			values["budget_year"] = int(budget_year)
		for column, value in (
			("group_dept_id", group_dept_id),
			("company_dept_id", company_dept_id),
			("department_id", department_id),
			("budget_type", budget_type),
		):
			text = str(value or "").strip()
			if text:
				conditions.append(f"q.{column} = %(filter_{column})s")
				values[f"filter_{column}"] = text
		for column, operator, value in (("budget_month", ">=", start_month), ("budget_month", "<=", end_month)):
			if value:
				key = "range_start" if operator == ">=" else "range_end"
				conditions.append(f"q.{column} {operator} %({key})s")
				values[key] = _month_start(value)
		if company_name:
			conditions.append("q.company_name = %(company_name)s")
			values["company_name"] = company_name
		if keyword:
			conditions.append("(q.form_no LIKE %(keyword_like)s OR q.department_name LIKE %(keyword_like)s)")
			values["keyword_like"] = "%" + str(keyword) + "%"
		month = _month_start(budget_month)
		if month:
			conditions.append("q.budget_month = %(budget_month)s")
			values["budget_month"] = month
		if str(subject_name or "").strip():
			conditions.append(
				f"EXISTS (SELECT 1 FROM `tab{SUBJECT_DOCTYPE}` s "
				"WHERE s.quota_form_no = q.name AND s.subject_name = %(subject_name)s)"
			)
			values["subject_name"] = str(subject_name).strip()
		return " AND ".join(conditions), values

	# ------------------------------------------------------------ 选项
	def get_options(self) -> dict:
		"""筛选下拉候选值；组织类选项只取 ``organization_is_current = 1``。"""
		where, values = self._filters()

		def _values(sql: str) -> list:
			return [row[0] for row in frappe.db.sql(sql, values)]

		years = _values(
			f"""SELECT DISTINCT q.budget_year FROM `tab{QUOTA_DOCTYPE}` q
			     WHERE {where} AND q.budget_year IS NOT NULL
			     ORDER BY q.budget_year DESC"""
		)
		months = _values(
			f"""SELECT DISTINCT LEFT(q.budget_month, 7) AS value FROM `tab{QUOTA_DOCTYPE}` q
			     WHERE {where} AND q.budget_month IS NOT NULL
			     ORDER BY value"""
		)
		budget_types = _values(
			f"""SELECT DISTINCT q.budget_type FROM `tab{QUOTA_DOCTYPE}` q
			     WHERE {where} AND NULLIF(TRIM(q.budget_type), '') IS NOT NULL
			     ORDER BY q.budget_type"""
		)
		subjects = _values(
			f"""SELECT DISTINCT s.subject_name
			      FROM `tab{SUBJECT_DOCTYPE}` s
			      JOIN `tab{QUOTA_DOCTYPE}` q ON q.name = s.quota_form_no
			     WHERE {where} AND NULLIF(TRIM(s.subject_name), '') IS NOT NULL
			     ORDER BY s.subject_name"""
		)

		def _organizations(
			id_field: str, name_field: str, *, exclude_company: bool = False
		) -> list[dict]:
			extra = (
				"AND q.department_id IS NOT NULL AND q.department_id <> q.company_dept_id"
				if exclude_company
				else ""
			)
			rows = frappe.db.sql(
				f"""SELECT DISTINCT NULLIF(TRIM(q.{id_field}), '') AS id,
				           {_name_sql(f"q.{name_field}")} AS name
				      FROM `tab{QUOTA_DOCTYPE}` q
				     WHERE {where}
				       AND q.organization_is_current = 1
				       AND NULLIF(TRIM(q.{id_field}), '') IS NOT NULL
				       {extra}
				     ORDER BY name""",
				values,
				as_dict=True,
			)
			return [{"id": row.get("id"), "name": row.get("name")} for row in rows]

		return {
			"years": years,
			"months": months,
			"budget_types": budget_types,
			"groups": _organizations("group_dept_id", "group_name"),
			"companies": _organizations("company_dept_id", "company_name"),
			"departments": _organizations(
				"department_id", "department_name", exclude_company=True
			),
			"subjects": subjects,
		}

	# ------------------------------------------------------------ 汇总
	def get_summary(self, **filters) -> dict:
		"""4 张统计卡 + 组织额度汇总（集团 / 子公司 / 部门 / 科目）。"""
		where, values = self._filters(**filters)

		totals = frappe.db.sql(
			f"""SELECT COUNT(*) AS quota_count,
			           COALESCE(SUM(q.total_amount), 0) AS total_amount,
			           COALESCE(SUM(CASE WHEN q.detail_amount_mismatch THEN 1 ELSE 0 END), 0)
			               AS detail_amount_mismatch_count,
			           COUNT(DISTINCT NULLIF(TRIM(q.department_id), '')) AS department_count
			      FROM `tab{QUOTA_DOCTYPE}` q
			     WHERE {where}""",
			values,
			as_dict=True,
		)
		total_row = totals[0] if totals else {}

		def _rollup(id_expr: str, name_expr: str, extra: str = "") -> list[dict]:
			return frappe.db.sql(
				f"""SELECT {id_expr} AS rollup_id,
				           {name_expr} AS rollup_name,
				           COUNT(*) AS quota_count,
				           COALESCE(SUM(q.total_amount), 0) AS total_amount
				      FROM `tab{QUOTA_DOCTYPE}` q
				     WHERE {where} {extra}
				     GROUP BY rollup_id, rollup_name
				     ORDER BY total_amount DESC, rollup_name""",
				values,
				as_dict=True,
			)

		group_rows = _rollup(
			"NULLIF(TRIM(q.group_dept_id), '')", _name_sql("q.group_name")
		)
		company_rows = _rollup(
			"NULLIF(TRIM(q.company_dept_id), '')", _name_sql("q.company_name")
		)
		# PG 版用 ``IS DISTINCT FROM``：两边都为 NULL 时不算部门，单边 NULL 仍要成组，
		# 因此这里用 NULL 安全的 ``<=>`` 取反，不能写成 ``IS NOT NULL AND <>``
		department_rows = _rollup(
			"NULLIF(TRIM(q.department_id), '')",
			_name_sql("q.department_name"),
			"AND NOT (q.department_id <=> q.company_dept_id)",
		)
		subject_rows = frappe.db.sql(
			f"""SELECT s.subject_type AS subject_type,
			           s.subject_code AS subject_code,
			           {_name_sql('s.subject_name')} AS rollup_name,
			           COUNT(DISTINCT q.name) AS quota_count,
			           COALESCE(SUM(s.amount), 0) AS total_amount
			      FROM `tab{QUOTA_DOCTYPE}` q
			      JOIN `tab{SUBJECT_DOCTYPE}` s ON s.quota_form_no = q.name
			     WHERE {where}
			     GROUP BY s.subject_type, s.subject_code, rollup_name
			     ORDER BY total_amount DESC, rollup_name""",
			values,
			as_dict=True,
		)

		def _rows(rows: list[dict]) -> list[dict]:
			return [
				{
					"id": row.get("rollup_id"),
					"name": row.get("rollup_name"),
					"quota_count": int(row.get("quota_count") or 0),
					"total_amount": _amount(row.get("total_amount")),
				}
				for row in rows
			]

		return {
			"totals": {
				"quota_count": int(total_row.get("quota_count") or 0),
				"total_amount": _amount(total_row.get("total_amount")),
				"detail_amount_mismatch_count": int(
					total_row.get("detail_amount_mismatch_count") or 0
				),
				"department_count": int(total_row.get("department_count") or 0),
			},
			"rollups": {
				"group": _rows(group_rows),
				"company": _rows(company_rows),
				"department": _rows(department_rows),
				"subject": [
					{
						"subject_type": row.get("subject_type"),
						"subject_code": row.get("subject_code"),
						"name": row.get("rollup_name"),
						"quota_count": int(row.get("quota_count") or 0),
						"total_amount": _amount(row.get("total_amount")),
					}
					for row in subject_rows
				],
			},
		}

	# ------------------------------------------------------------ 明细
	def _subjects(self, form_nos: list[str]) -> dict[str, list[dict]]:
		names = [str(name).strip() for name in (form_nos or []) if str(name or "").strip()]
		if not names:
			return {}
		rows = frappe.db.sql(
			f"""SELECT quota_form_no, subject_name, amount
			      FROM `tab{SUBJECT_DOCTYPE}`
			     WHERE quota_form_no IN %(names)s
			     ORDER BY quota_form_no, line_no""",
			{"names": names},
			as_dict=True,
		)
		grouped: dict[str, list[dict]] = {}
		for row in rows:
			grouped.setdefault(str(row.get("quota_form_no")), []).append(
				{"name": row.get("subject_name"), "amount": _amount(row.get("amount"))}
			)
		return grouped

	def list_quotas(self, *, page: int = 1, page_size: int = 20, **filters) -> dict:
		where, values = self._filters(**filters)
		page = max(int(page or 1), 1)
		page_size = min(max(int(page_size or 20), 1), 200)

		count_rows = frappe.db.sql(
			f"SELECT COUNT(*) AS total FROM `tab{QUOTA_DOCTYPE}` q WHERE {where}",
			values,
			as_dict=True,
		)
		total = int(count_rows[0].get("total") or 0) if count_rows else 0

		rows = frappe.db.sql(
			f"""SELECT q.name, q.budget_year, LEFT(q.budget_month, 7) AS budget_month,
			           q.group_name, q.company_name, q.department_name, q.department_id,
			           q.company_dept_id,
			           (q.department_id IS NOT NULL AND q.department_id = q.company_dept_id)
			               AS department_is_company,
			           q.total_amount, q.detail_amount, q.detail_amount_mismatch,
			           q.owner_name, q.budget_type, q.form_no, q.approval_completed_at
			      FROM `tab{QUOTA_DOCTYPE}` q
			     WHERE {where}
			     ORDER BY q.budget_month DESC, q.approval_completed_at DESC, q.name DESC
			     LIMIT %(limit)s OFFSET %(offset)s""",
			{**values, "limit": page_size, "offset": (page - 1) * page_size},
			as_dict=True,
		)

		subjects = self._subjects([row.get("form_no") for row in rows])
		payload = []
		for row in rows:
			lines = subjects.get(str(row.get("form_no")), [])
			payload.append(
				{
					"id": row.get("name"),
					"budget_year": row.get("budget_year"),
					"budget_month": row.get("budget_month"),
					"group_name": row.get("group_name"),
					"company_name": row.get("company_name"),
					"department_name": row.get("department_name"),
					"department_id": row.get("department_id"),
					"company_dept_id": row.get("company_dept_id"),
					"department_is_company": bool(row.get("department_is_company")),
					"total_amount": _amount(row.get("total_amount")),
					"detail_amount": _amount(row.get("detail_amount")),
					"detail_amount_mismatch": bool(row.get("detail_amount_mismatch")),
					"owner_name": row.get("owner_name"),
					"budget_type": row.get("budget_type"),
					"form_no": row.get("form_no"),
					"subject_amount": round(sum(line["amount"] for line in lines), 2),
					"subjects": lines,
				}
			)

		return {
			"rows": payload,
			"total": total,
			"page": page,
			"page_size": page_size,
		}
