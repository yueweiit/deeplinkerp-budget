"""实际支出（审批库 ``dingtalk_approval.approval_expense_*``）。

报表里的「实际支出明细 / 部门支出占比 / 部门执行率 / 执行状态分布」都依赖这些表：
运营支出（operation）、采购支出（purchase）、部门分摊（dept_split）、付款事件（payment_events）。
"""

from __future__ import annotations

from typing import Any

from .oa_approval_source import (
	OAApprovalSourceConfig,
	_default_connect,
	shared_database_name,
)

APPROVAL_DATABASE = "dingtalk_approval"
COMPLETED = "COMPLETED"


def approval_connection_kwargs() -> dict:
	"""审批库与 OA 库同实例时复用连接凭据，只换库名。

	连接信息来自站点唯一入口（``shared_db_config()``）；审批表与 OA 表合并到同一个库后
	库名也共用，合并之前才用 ``budget_approval_db_name`` / ``dingtalk_approval_db_name`` 指定。
	"""
	config = OAApprovalSourceConfig.from_site_config()
	database = shared_database_name(
		"budget_approval_db_name", "dingtalk_approval_db_name", default=APPROVAL_DATABASE
	)
	options = [
		f"-c statement_timeout={int(config.statement_timeout_ms)}",
		"-c default_transaction_read_only=on",
	]
	return {
		"host": config.host,
		"port": int(config.port),
		"dbname": database,
		"user": config.user,
		"password": config.password,
		"connect_timeout": int(config.connect_timeout),
		"application_name": "budget-reported-expense",
		"options": " ".join(options),
	}


class BudgetExpenseSource:
	"""只读查询实际支出，供报表导出使用。"""

	# (expense_kind, 表名)；表名来自内部常量，可安全拼进 SQL
	_EXPENSE_TABLES = (
		("operation", "approval_expense_operation"),
		("purchase", "approval_expense_purchase"),
	)

	def __init__(self, connect: Any | None = None):
		self._connect = connect or _default_connect

	def _fetch(self, sql: str, params: tuple | list | None = None) -> list[dict]:
		with self._connect(approval_connection_kwargs()) as connection:
			with connection.cursor() as cursor:
				cursor.execute(sql, tuple(params or ()))
				return list(cursor.fetchall())

	def load_expenses(self) -> list[dict]:
		"""已通过的运营 / 采购支出，附带标题与分类。"""
		rows: list[dict] = []

		for row in self._fetch(
			"""
			SELECT business_id, applicant_department, applicant_department_path_names,
			       expense_type, request_date, approval_completed_at, source_created_at,
			       base_currency_amount, approval_status, form_name, matter_description
			FROM public.approval_expense_operation
			WHERE approval_status = %s
			""",
			(COMPLETED,),
		):
			row["expense_kind"] = "operation"
			rows.append(row)

		for row in self._fetch(
			"""
			SELECT business_id, applicant_department, applicant_department_path_names,
			       NULL::text AS expense_type, request_date, approval_completed_at,
			       source_created_at, base_currency_amount, approval_status, form_name,
			       NULL::text AS matter_description
			FROM public.approval_expense_purchase
			WHERE approval_status = %s
			""",
			(COMPLETED,),
		):
			row["expense_kind"] = "purchase"
			rows.append(row)

		return rows

	def load_pending_expenses(self) -> list[dict]:
		"""审批中的支出金额（执行状态分布里的「审批中」）。

		与 ``load_expenses`` 保持同一行结构（含 ``expense_kind``），
		方便下游把已完成 / 审批中两类金额放在一起按运营、采购口径分组。
		"""
		rows: list[dict] = []
		for kind, table in self._EXPENSE_TABLES:
			for row in self._fetch(
				f"""
				SELECT business_id, applicant_department, applicant_department_path_names,
				       request_date, base_currency_amount
				FROM public.{table}
				WHERE approval_status = 'RUNNING'
				"""
			):
				row["expense_kind"] = kind
				rows.append(row)
		return rows

	def load_splits(self) -> dict[str, list[dict]]:
		"""部门分摊：business_id -> [{split_type, department, amount}]。"""
		grouped: dict[str, list[dict]] = {}
		for row in self._fetch(
			"""
			SELECT business_id, split_type, department, department_path_names,
			       amount, category_name
			FROM public.approval_expense_dept_split
			"""
		):
			grouped.setdefault(str(row.get("business_id") or ""), []).append(row)
		return grouped

	def load_payments(self) -> dict[str, list[dict]]:
		"""付款事件（已确认）：business_id -> [{paid_at, amount, evidence_text}]。"""
		grouped: dict[str, list[dict]] = {}
		for row in self._fetch(
			"""
			SELECT business_id, expense_kind, paid_at, base_currency_amount, evidence_text
			FROM public.approval_expense_payment_events
			WHERE status = 'confirmed'
			ORDER BY paid_at NULLS LAST, id
			"""
		):
			grouped.setdefault(str(row.get("business_id") or ""), []).append(row)
		return grouped
