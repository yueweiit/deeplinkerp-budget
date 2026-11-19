"""预算数据源选择。

集成 CN/MX 的站点统一读取 OA 同步后的本地快照，不再受旧模式开关限制。
独立安装保留三种模式（``budget_data_source``，环境变量或 ``frappe.conf``）::

    local      默认。预算台账读站点 MariaDB（由 OA 归档同步而来）
    postgres   应急回退：直连外部 PostgreSQL
    http       调用 dingtalk-oa 的只读接口（上游接口已移除时不可用）

优先级：``budget_data_source`` > 旧键 ``budget_oa_source`` > ``local``。

注意：实际支出、部门树尚未全部本地化；共享 OA API 未提供的指标明确标记不可用。
独立安装的 ``local`` 模式仍按明确绑定的 PostgreSQL 来源读取这些指标；
预算台账与预算额度是纯本地读取；共用 OA 模式由 ``oa_budget_sync`` 同事务同步。
"""

from __future__ import annotations

from typing import Any

from .oa_approval_source import _runtime_config_value

_HTTP_VALUES = {"http", "api", "oa", "dingtalk-oa", "dingtalk_oa"}
_PG_VALUES = {"postgres", "postgresql", "pg", "db", "sql"}
_LOCAL_VALUES = {"local", "site", "mariadb", "mysql", "db_local"}
DEFAULT_MODE = "local"


def data_source_mode() -> str:
	# CN and MX share the OA archive. Once integrated, every budget consumer
	# (including report drill-down and detail) reads the synchronized ERP snapshot.
	# Legacy mode switches remain available only for standalone installations.
	from .shared_operating_oa import available
	if available():
		return "local"
	for key in ("budget_data_source", "budget_oa_source"):
		value = (_runtime_config_value(key) or "").strip().lower()
		if not value:
			continue
		if value in _HTTP_VALUES:
			return "http"
		if value in _PG_VALUES:
			return "postgres"
		if value in _LOCAL_VALUES:
			return "local"
		return value
	return DEFAULT_MODE


def use_http_sources() -> bool:
	return data_source_mode() == "http"


def get_ledger_source() -> Any:
	"""预算台账：默认读本地表。"""
	mode = data_source_mode()
	if mode == "http":
		from .budget_ledger_api_source import BudgetLedgerAPISource

		return BudgetLedgerAPISource()
	if mode == "postgres":
		from .budget_ledger_source import BudgetLedgerSource

		return BudgetLedgerSource()
	from .budget_ledger_local_source import BudgetLedgerLocalSource

	return BudgetLedgerLocalSource()


def get_quota_source() -> Any:
	"""预算额度：默认读本地审批快照；独立安装的显式旧读取模式保留兼容。"""
	mode = data_source_mode()
	if mode == "http":
		from .quota_api_source import QuotaAPISource

		return QuotaAPISource()
	if mode == "postgres":
		from .quota_source import QuotaSource

		return QuotaSource()
	from .budget_quota_local_source import BudgetQuotaLocalSource

	return BudgetQuotaLocalSource()


def get_expense_source() -> Any:
	"""已通过支出明细（尚未本地化）。"""
	if use_http_sources():
		from .expense_api_source import ExpenseAPISource

		return ExpenseAPISource()
	if data_source_mode() == "local":
		from ..services.ledger_sync_settings import validate_expense_source

		validate_expense_source()
	from .expense_source import ExpenseSource

	return ExpenseSource()


def get_budget_expense_source() -> Any:
	"""报表用支出数据（尚未本地化）。"""
	if use_http_sources():
		from .budget_expense_api_source import BudgetExpenseAPISource

		return BudgetExpenseAPISource()
	if data_source_mode() == "local":
		from ..services.ledger_sync_settings import validate_expense_source

		validate_expense_source()
	from .budget_expense_source import BudgetExpenseSource

	return BudgetExpenseSource()
