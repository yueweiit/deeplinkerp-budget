from decimal import Decimal
from types import SimpleNamespace

import frappe
import pytest

from budget.api import cn_workbench as api
from budget.integrations.budget_quota_local_source import BudgetQuotaLocalSource
from budget.services import report_service, ledger_sync_settings
from budget.services.oa_budget_parser import BUDGET_PROCESS_CODE
from budget.services.oa_budget_sync import _entry_values


@pytest.fixture(autouse=True)
def translations(monkeypatch):
    monkeypatch.setattr(ledger_sync_settings, "current_source", lambda: {"corp_id": "corp-A"})
    monkeypatch.setattr(frappe, "_", lambda text, *a, **kw: text)
    monkeypatch.setattr(report_service, "_", lambda text, *a, **kw: text)
    monkeypatch.setattr(report_service, "current_report_source", lambda: None)
    monkeypatch.setattr(api, "schema_state", lambda: {"level_column": True, "ready": True})


def test_budget_scope_uses_complete_months_and_exact_parameterized_companies():
    f = api.normalize_filters(dict(from_date="2024-02-19",to_date="2024-03-03",company="A' OR 1=1 --"))
    assert (f["from_date"],f["to_date"]) == ("2024-02-01","2024-03-31")
    where, params = api.ledger_where(f)
    assert "company = %(company)s" in where
    assert f["company"] not in where
    assert params["company"] == f["company"]
    assert "application_date" not in where
    review, _ = api.ledger_where(f, review=True)
    assert "budget_month IS NULL AND application_date BETWEEN" in review
    assert api.ISSUES in review


def test_review_preserves_known_zero_and_decimal_tolerance():
    base = dict(company="A",department="D",budget_month="2026-11-01",total_amount=Decimal("0"),amount=99,detail_amount=0)
    assert api.issues_for(base) == []
    assert api.issues_for({**base,"detail_amount":Decimal("0.01")}) == []
    assert api.issues_for({**base,"detail_amount":Decimal("0.02")}) == ["Budget amount differs from detail total"]
    assert len(api.issues_for(dict(total_amount=12,detail_amount=10))) == 4


def test_company_level_approval_is_not_a_missing_department():
    row = _entry_values({"entry": {"dept_id": "company-id", "amount": 100,
                        "detail_amount": 100, "budget_month": "2026-11-01"}},
                        {"company-id": {"ids": ["root", "group", "company-id"],
                                        "names": ["Root", "Group", "Company A"]}}, "2026-11-19")
    assert row["department"] == ""
    assert api.is_company_level(row)
    assert api.issues_for(row) == []
    for changes in ({"department_id": "other-id"}, {"company": "Other"},
                    {"dept_path_ids": "invalid-json"}, {"dept_path_names": "[]"},
                    {"dept_path_ids": '["root","group","company-id","child"]',
                     "dept_path_names": '["Root","Group","Company A","Child"]'},
                    {"dept_path_ids": "{}"}, {"department_id": ""}):
        assert "Budget department is missing" in api.issues_for({**row, **changes})


def test_quota_list_totals_and_export_keep_the_department_scope(monkeypatch):
    import base64
    from io import BytesIO
    from openpyxl import load_workbook
    from frappe.utils import xlsxutils

    monkeypatch.setattr(api, "_check", lambda *a: None)
    monkeypatch.setattr(api, "_", lambda text: text)
    monkeypatch.setattr(xlsxutils, "_", lambda text: text)
    monkeypatch.setattr(frappe, "get_system_settings", lambda key: {"date_format": "yyyy-mm-dd", "time_format": "HH:mm:ss"}.get(key))
    queries = []
    def sql(query, params, **kwargs):
        queries.append((query, params))
        assert "q.corp_id = %(corp_id)s" in query and params["corp_id"] == "corp-A"
        assert "q.oa_process_code = %(oa_process_code)s" in query and params["oa_process_code"] == BUDGET_PROCESS_CODE
        assert "q.department_id = %(filter_department_id)s" in query
        assert params["filter_department_id"] == "dept-A"
        assert params["company_name"] == "Company A"
        if "COUNT(*) AS total " in query:
            return [{"total": 0}]
        if "AS quota_count" in query and "GROUP BY" not in query:
            return [{"quota_count": 0, "total_amount": 0}]
        return []
    monkeypatch.setattr(frappe, "db", SimpleNamespace(sql=sql))
    filters = dict(from_date="2026-11-01", to_date="2026-11-30", company="Company A", department_id="dept-A")
    result = api.get_data(filters, tab="quota")
    assert result["total_count"] == 0
    assert result["summary"]["totals"]["total_amount"] == 0
    exported = api.export_data(filters, tab="quota")
    book = load_workbook(BytesIO(base64.b64decode(exported["content"])))
    assert book.active.max_row == 1
    assert len(queries) >= 7


def test_quota_department_name_fallback_stays_parameterized():
    filters = api.normalize_filters(dict(from_date="2026-11-01", to_date="2026-11-30", keyword="D' OR 1=1 --"))
    where, params = BudgetQuotaLocalSource()._filters(**api.quota_filters(filters))
    assert "q.department_name LIKE %(keyword_like)s" in where
    assert filters["keyword"] not in where
    assert params["keyword_like"] == "%D' OR 1=1 --%"


def test_quota_range_keeps_effective_only_and_does_not_use_single_month():
    where, params = BudgetQuotaLocalSource()._filters(start_month="2026-09",end_month="2026-11",company_name="A")
    assert params == {"corp_id": "corp-A", "oa_process_code": BUDGET_PROCESS_CODE, "status":"effective","range_start":"2026-09-01","range_end":"2026-11-01","company_name":"A"}
    assert "q.status = %(status)s" in where
    assert "q.company_name = %(company_name)s" in where
    assert "q.budget_month >= %(range_start)s" in where
    assert "q.budget_month <= %(range_end)s" in where


def test_every_quota_option_and_cn_company_option_uses_current_corporation_and_template(monkeypatch):
    queries = []
    def sql(query, params, **kwargs):
        assert "q.corp_id = %(corp_id)s" in query and params["corp_id"] == "corp-A"
        assert "q.oa_process_code = %(oa_process_code)s" in query and params["oa_process_code"] == BUDGET_PROCESS_CODE
        assert "q.status = %(status)s" in query and params["status"] == "effective"
        queries.append(query)
        return []
    monkeypatch.setattr(frappe, "db", SimpleNamespace(sql=sql))
    BudgetQuotaLocalSource().get_options()
    assert len(queries) == 7
    monkeypatch.setattr(api, "_check", lambda *a: None)
    monkeypatch.setattr(frappe, "get_roles", lambda: ["System Manager"])
    monkeypatch.setattr(frappe, "session", SimpleNamespace(user="Administrator"))
    def get_list(doctype, **kwargs):
        if doctype == "Budget Quota":
            assert kwargs["filters"] == {"corp_id": "corp-A", "oa_process_code": BUDGET_PROCESS_CODE, "status": "effective"}
        return []
    monkeypatch.setattr(frappe, "get_list", get_list)
    api.get_options()


@pytest.mark.parametrize("source", [{"corp_id": ""}, {"corp_id": "corp-A", "bound_fingerprint": "old", "fingerprint": "changed"}])
def test_unbound_or_changed_quota_scope_cannot_read_any_historical_rows(monkeypatch, source):
    from budget.integrations.budget_quota_local_source import scope_filters
    monkeypatch.setattr(ledger_sync_settings, "current_source", lambda: source)
    where, _ = BudgetQuotaLocalSource()._filters()
    assert "1 = 0" in where
    assert scope_filters()["name"] == ["is", "not set"]


@pytest.mark.parametrize("endpoint,kwargs,roles", [(api.get_options,{},api.READ_ROLES),(api.get_data,{},api.READ_ROLES),(api.get_data,{"tab":"quota"},("System Manager",)),(api.export_data,{"tab":"quota"},("System Manager",))])
def test_endpoints_authorize_before_reading_data(monkeypatch,endpoint,kwargs,roles):
    seen=[]
    def denied(value):
        seen.append(value)
        raise PermissionError("denied")
    monkeypatch.setattr(frappe,"only_for",denied)
    with pytest.raises(PermissionError): endpoint(**kwargs)
    assert seen == [roles]


def test_company_expense_assignment_rejects_unknown_and_ambiguous_ownership():
    rows=[dict(department_id="1",amount=10),dict(department_id="2",amount=20)]
    assert report_service.company_expenses(rows,{"1":{"A"},"2":{"B"}},"A") == rows[:1]
    for mapping in ({"1":{"A"}}, {"1":{"A"},"2":{"A","B"}}, {"1":{"A"},"2":{""}}):
        with pytest.raises(ValueError): report_service.company_expenses(rows,mapping,"A")


def test_report_company_filter_scopes_budget_counts_and_expenses(monkeypatch):
    entries=[dict(company_display=company,dept_id=dept,department_display=dept,budget_kind="non_production",budget_month="2026-11",amount=amount) for company,dept,amount in (("A","1",100),("B","2",900))]
    count_calls=[]
    def details(*args,**kwargs): count_calls.append(kwargs); return {"non_production":2}
    monkeypatch.setattr(report_service,"get_ledger_source",lambda:SimpleNamespace(list_budget_entries=lambda **kw:{"rows":entries,"total":2},count_detail_rows=details))
    monkeypatch.setattr(report_service,"get_expense_source",lambda:SimpleNamespace(list_approved_expenses=lambda *a:[dict(department_id="1",amount=25,accounting_date="2026-11-02"),dict(department_id="2",amount=500,accounting_date="2026-11-02")]))
    result=report_service.build_report("2026-11-01","2026-11-30",company="A")
    assert count_calls == [{"company":"A"}]
    assert result["stats"]["nonProductionTotal"] == 100
    assert result["stats"]["approvedTotal"] == 25
    assert result["availability"]["pending_expenses"] is False
    assert result["availability"]["approval"] is False
    assert result["execution_status"] == []


def test_local_report_adapter_keeps_zero_in_sync_with_ledger_aggregation():
    from budget.integrations.budget_ledger_local_source import _row_to_api
    assert _row_to_api({"total_amount":0,"amount":99})["amount"] == 0
    assert _row_to_api({"total_amount":None,"amount":99})["amount"] == 99
