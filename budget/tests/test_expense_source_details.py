"""Original line provenance must not substitute an approval total or stale row."""
import frappe
import pytest

from budget.services import expense_source_details as service


def test_cn_table_preview_uses_selected_original_row_and_description():
	source = {"amount": "100.00", "currency": "CNY", "tables": [{"label": "Expenses", "rows": [
		{"amount": "40.00", "label": "Other line"},
		{"amount": "60.00", "label": "Department A", "entity": "Department A", "cells": [{"label": "备注", "value": "Office rent"}]}]}]}
	selected = {"table_index": 0, "detail_index": 1, "amount": "60.00", "currency": "CNY"}
	result = service.original_expense("native", source, selected, {"amount": "60", "currency": "CNY"})
	assert result == {"origin": "detail", "table_label": "Expenses", "row_number": "2", "amount": "60.00",
		"currency": "CNY", "description": "Office rent", "source_entity": "Department A", "split_count": 1}
	assert "100.00" not in str(result) and "Other line" not in str(result)
	assert service.original_expense("native", source, selected, {"amount": "40", "currency": "CNY"}) is None


def test_cn_header_remains_explicit_when_no_detail_was_parsed():
	source = {"amount": "80.00", "currency": "CNY", "description": "Header expense", "entity": "Department A"}
	result = service.original_expense("native", source, {**source, "table_index": -1, "detail_index": -1}, source)
	assert result["origin"] == "header" and result["description"] == "Header expense"
	assert result["amount"] == "80.00" and not result["row_number"]


@pytest.mark.parametrize("tables", [[], [{"is_amount": True, "label": "Budget", "rows": [
	{"label": "Office", "amount": "60.00", "cells": []}]}]])
def test_budget_header_preview_compares_the_same_normalized_path_as_the_report(tables):
	from budget.services import cn_approval_management as management, cn_approval_management_rules as rules
	from budget.services import expense_management
	from .test_cn_approval_management import approval, context
	source = approval("header-budget", kind="budget", value="80.00", tables=tables, description="Budget request")
	selected = management.analytical_lines([source], context(), rules.defaults())[0]
	row = expense_management.normalize(selected)
	assert selected["source_path"] == ["unitemized"] and row["source_path"] == []
	result = service.original_expense("native", source, selected, row)
	assert result["origin"] == "header" and result["amount"] == "80.00"
	assert result["description"] == "Budget request"
	assert service.original_expense("native", source, selected, {**row, "amount": "81.00"}) is None
	assert service.original_expense("native", source, selected, {**row, "source_path": ["Office"]}) is None


def test_legacy_preview_reparses_only_local_source_and_requires_exact_identity_and_version(monkeypatch):
	source = frappe._dict(raw_json='{"corp_id":"corp","process_code":"code"}', corp_id="corp", process_code="code", source_version="v1")
	settings = frappe._dict(business_timezone="Asia/Shanghai", templates=[{"corp_id": "corp", "process_code": "code"}])
	monkeypatch.setattr(service.mx, "_settings", lambda: settings)
	line = {"line_key": "selected", "table_id": "table", "source_row_id": "4", "amount": "12.34", "currency": "MXN", "description": "Local row"}
	calls = []
	def project(raw, template, timezone, **kwargs):
		calls.append((raw, timezone, kwargs))
		return {"approval_status": "approved"}, [line]
	monkeypatch.setattr(service.mx.projection, "project", project)
	row = {"key": "selected", "amount": "12.34", "currency": "MXN", "status": "approved"}
	assert service.original_expense("legacy", source, {"source_version": "old"}, row) is None
	assert calls == []
	result = service.original_expense("legacy", source, {"source_version": "v1"}, row)
	assert result["row_number"] == "4" and result["description"] == "Local row"
	assert calls[0][1:] == ("Asia/Shanghai", {"ignore_region": True, "include_finance": False})
	assert service.original_expense("legacy", source, {"source_version": "v1"}, {**row, "key": "different"}) is None
	assert service.original_expense("legacy", source, {"source_version": "v1"}, {**row, "status": "pending"}) is None
	settings.templates = []
	assert service.original_expense("legacy", source, {"source_version": "v1"}, row) is None


@pytest.mark.parametrize("amount,currency", [(None, "CNY"), ("NaN", "CNY"), ("Infinity", "CNY"), ("10", "MXN"), ("11", "CNY")])
def test_original_preview_cannot_be_attached_to_unknown_or_changed_amount(amount, currency):
	row = {"amount": amount, "currency": currency}
	assert not service.current_line(row, "10.00", "CNY")


def test_same_amount_does_not_prove_a_replaced_row_is_the_original():
	row = {"amount": "10.00", "currency": "CNY", "description": "Rent", "source_path": ["Office"]}
	assert not service.current_line(row, "10", "CNY", {"description": "Salary"})
	assert not service.current_line(row, "10", "CNY", {"source_path": ["Payroll"]})
