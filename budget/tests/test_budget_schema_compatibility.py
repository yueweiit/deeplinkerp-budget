import base64
from io import BytesIO
from types import SimpleNamespace

import frappe
import pytest
from openpyxl import load_workbook

from budget.api import cn_workbench as api
from budget.services import organization_mapping as mapping, ledger_sync_settings as settings
from budget.services import oa_budget_sync as sync, ledger_source_transition as transition


@pytest.mark.parametrize("column,table,ledger_meta,settings_meta,ready", [
    (False, False, False, False, False), (True, False, True, True, False),
    (True, True, False, True, False), (True, True, True, False, False),
    (True, True, True, True, True),
])
def test_readiness_requires_physical_schema_and_metadata(monkeypatch, column, table, ledger_meta, settings_meta, ready):
    monkeypatch.setattr(frappe, "db", SimpleNamespace(has_column=lambda *a: column, table_exists=lambda *a: table))
    monkeypatch.setattr(frappe, "get_meta", lambda name: SimpleNamespace(
        has_field=lambda field: ledger_meta if name == "Budget Ledger Entry" else settings_meta))
    assert mapping.schema_state() == {"level_column": column, "ready": ready}


def test_old_schema_overview_review_details_and_export_preserve_existing_amounts(monkeypatch):
    from frappe.utils import xlsxutils
    monkeypatch.setattr(api, "schema_state", lambda: {"level_column": False, "ready": False})
    monkeypatch.setattr(api, "_check", lambda *a: None)
    monkeypatch.setattr(api, "_", lambda text: text)
    monkeypatch.setattr(xlsxutils, "_", lambda text: text)
    monkeypatch.setattr(frappe, "get_system_settings", lambda key: {"date_format": "yyyy-mm-dd", "time_format": "HH:mm:ss"}.get(key))
    row = dict(name="B1", form_no="B1", company="待确认", department="Original OA department", department_id="org1",
               budget_kind="production", budget_month="2026-11-01", total_amount=123, amount=123, detail_amount=123)
    queries = []
    def sql(query, params, **kwargs):
        queries.append(query)
        assert "reviewed_organization_level" not in query.replace("'' AS reviewed_organization_level", "")
        assert params["from_date"] == "2026-11-01"
        if "AS total_count" in query: return [dict(total_count=1, amount=123, company_count=0, review_count=1)]
        if "SELECT COUNT(*)" in query: return [(1,)]
        if "GROUP BY" in query: return []
        if "SELECT name" in query: return [dict(row, reviewed_organization_level="")]
        return [("B1", "2026-11-01", "待确认", "Original OA department", "production", 123, 123)]
    monkeypatch.setattr(frappe, "db", SimpleNamespace(sql=sql))
    filters = dict(from_date="2026-11-01", to_date="2026-11-30")
    for view in ("overview", "details", "review"):
        result = api.get_data(filters, view=view)
        assert result["amount"] == 123 and result["total_count"] == 1
        assert result["rows"][0]["issues"] == ["Budget company is missing"]
    result = api.export_data(filters, view="review")
    book = load_workbook(BytesIO(base64.b64decode(result["content"])))
    assert book.active.cell(2, 6).value == 123
    assert any("budget_month IS NULL AND application_date" in query for query in queries)


def test_partial_schema_still_uses_existing_review_column(monkeypatch):
    monkeypatch.setattr(api, "schema_state", lambda: {"level_column": True, "ready": False})
    level, issues = api.review_columns()
    assert level == "reviewed_organization_level" and issues == api.ISSUES


def test_sync_is_blocked_before_enqueue_when_schema_is_incomplete(monkeypatch):
    source = dict(configured=True, enabled=True, organization_review_ready=False,
                  fingerprint="same", bound_fingerprint="same")
    monkeypatch.setattr(settings, "current_source", lambda: source)
    monkeypatch.setattr(frappe, "only_for", lambda *a: None)
    monkeypatch.setattr(sync, "_enqueue_sync", lambda *a, **kw: pytest.fail("must not enqueue"))
    assert settings.blocked_reason(source) == "schema_update_required"
    assert sync.enqueue_oa_budget_sync()["blocked_by"] == "schema_update_required"
    assert "schema_update_required" in sync.SKIP_REASONS


def test_unmigrated_mapping_and_source_switch_fail_before_reading_or_writing(monkeypatch):
    monkeypatch.setattr(mapping, "schema_state", lambda: {"level_column": False, "ready": False})
    monkeypatch.setattr(mapping, "_", lambda text: text)
    monkeypatch.setattr(frappe, "throw", lambda text: (_ for _ in ()).throw(ValueError(text)))
    monkeypatch.setattr(frappe, "db", SimpleNamespace(get_value=lambda *a, **kw: pytest.fail("must not read or write")))
    for action in (lambda: mapping._context("B1", {}), lambda: transition._prepare({}, "v")):
        with pytest.raises(ValueError, match="requires a database update"):
            action()
