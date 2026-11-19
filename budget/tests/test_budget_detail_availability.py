from types import SimpleNamespace

import pytest

from budget.api import budget_ledger
from budget.services import ledger_sync_settings


@pytest.fixture
def detail_context(monkeypatch):
	entry = {"budget_month": "2026-11-01", "department": "Finance", "amount": 1000}
	monkeypatch.setattr(budget_ledger.frappe, "only_for", lambda *a: None)
	monkeypatch.setattr(budget_ledger.frappe, "db", SimpleNamespace(exists=lambda *a: True))
	monkeypatch.setattr(budget_ledger.frappe, "get_doc", lambda *a: SimpleNamespace(as_dict=lambda: dict(entry)))
	monkeypatch.setattr(budget_ledger, "get_source", lambda: SimpleNamespace(
		load_remarks=lambda *a: {}, list_expense_details=lambda *a: [], list_production_details=lambda *a: []))
	monkeypatch.setattr(budget_ledger, "data_source_mode", lambda: "local")
	monkeypatch.setattr(ledger_sync_settings, "current_source", lambda: {"corp_id": "selected-corp"})
	return entry


def test_unavailable_expenses_do_not_become_zero_or_unused_budget(detail_context, monkeypatch):
	def unavailable():
		raise ledger_sync_settings.LedgerReadSourceError("source binding mismatch")
	monkeypatch.setattr(budget_ledger, "get_expense", unavailable)
	result = budget_ledger.get_budget_entry_detail("budget-1")
	assert result["budget"]["total"] == 1000
	assert not result["expense_available"]
	assert all(value is None for value in result["expense"].values())
	assert result["remaining"] is None
	assert result["expense_details"] == []
	assert "source binding mismatch" in result["warning"]
	assert result["corp_id"] == "selected-corp"


def test_readable_empty_expenses_are_real_zero(detail_context, monkeypatch):
	monkeypatch.setattr(budget_ledger, "get_expense", lambda: SimpleNamespace(
		list_approved_expenses=lambda *a: [], load_detail_meta=lambda *a: {}))
	result = budget_ledger.get_budget_entry_detail("budget-1")
	assert result["expense_available"]
	assert result["expense"]["total"] == 0
	assert result["remaining"] == 1000
	assert not result["warning"]


def test_missing_month_has_no_expense_query_or_fabricated_balance(detail_context, monkeypatch):
	detail_context["budget_month"] = None
	monkeypatch.setattr(budget_ledger, "get_expense", lambda: pytest.fail("no bounded period to query"))
	result = budget_ledger.get_budget_entry_detail("budget-1")
	assert not result["expense_available"]
	assert result["remaining"] is None
