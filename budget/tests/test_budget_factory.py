from __future__ import annotations

import pytest

from budget.integrations import budget_factory


@pytest.fixture(autouse=True)
def clean_switch(monkeypatch):
	import frappe

	monkeypatch.delenv("BUDGET_DATA_SOURCE", raising=False)
	monkeypatch.delenv("BUDGET_OA_SOURCE", raising=False)
	# 脱离站点运行（pytest 不带 site 上下文）时 frappe.local 未绑定，读 frappe.conf 会抛
	# RuntimeError；这些用例只关心环境变量与默认值，给个空配置即可。
	monkeypatch.setattr(frappe, "conf", frappe._dict(), raising=False)


def test_local_switch_returns_local_sources(monkeypatch) -> None:
	from budget.integrations.budget_ledger_local_source import BudgetLedgerLocalSource
	from budget.integrations.budget_quota_local_source import BudgetQuotaLocalSource

	monkeypatch.setenv("BUDGET_DATA_SOURCE", "local")

	assert budget_factory.data_source_mode() == "local"
	assert isinstance(budget_factory.get_ledger_source(), BudgetLedgerLocalSource)
	assert isinstance(budget_factory.get_quota_source(), BudgetQuotaLocalSource)


def test_http_switch_returns_api_sources(monkeypatch) -> None:
	from budget.integrations.budget_expense_api_source import BudgetExpenseAPISource
	from budget.integrations.budget_ledger_api_source import BudgetLedgerAPISource
	from budget.integrations.expense_api_source import ExpenseAPISource
	from budget.integrations.quota_api_source import QuotaAPISource

	monkeypatch.setenv("BUDGET_DATA_SOURCE", "http")

	assert budget_factory.data_source_mode() == "http"
	assert isinstance(budget_factory.get_ledger_source(), BudgetLedgerAPISource)
	assert isinstance(budget_factory.get_quota_source(), QuotaAPISource)
	assert isinstance(budget_factory.get_expense_source(), ExpenseAPISource)
	assert isinstance(budget_factory.get_budget_expense_source(), BudgetExpenseAPISource)


def test_postgres_switch_returns_direct_sources(monkeypatch) -> None:
	from budget.integrations.budget_expense_source import BudgetExpenseSource
	from budget.integrations.budget_ledger_source import BudgetLedgerSource
	from budget.integrations.expense_source import ExpenseSource
	from budget.integrations.quota_source import QuotaSource

	monkeypatch.setenv("BUDGET_DATA_SOURCE", "postgres")

	assert budget_factory.use_http_sources() is False
	assert isinstance(budget_factory.get_ledger_source(), BudgetLedgerSource)
	assert isinstance(budget_factory.get_quota_source(), QuotaSource)
	assert isinstance(budget_factory.get_expense_source(), ExpenseSource)
	assert isinstance(budget_factory.get_budget_expense_source(), BudgetExpenseSource)


def test_new_switch_wins_over_legacy_key(monkeypatch) -> None:
	monkeypatch.setenv("BUDGET_DATA_SOURCE", "http")
	monkeypatch.setenv("BUDGET_OA_SOURCE", "postgres")

	assert budget_factory.data_source_mode() == "http"


def test_legacy_key_still_works(monkeypatch) -> None:
	monkeypatch.setenv("BUDGET_OA_SOURCE", "postgres")

	assert budget_factory.data_source_mode() == "postgres"
