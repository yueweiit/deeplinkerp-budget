from decimal import Decimal
from types import SimpleNamespace

import frappe
import pytest

from budget.integrations.cn_expense_report_source import CNExpenseReportSource, ReportUnavailable
from budget.services import report_service


def approval(name="a", **changes):
    row = dict(name=name, source_system="dingtalk-oa", source_company="Finance", approval_no=name,
        request_date="2026-11-02", approval_state="approved", currency="CNY", amount="100",
        oa_identity={"corp_id": "corp", "process_instance_id": name},
        reporting=dict(version=1, process_code="PROC-ONE", create_time="2026-11-02T01:00:00Z",
            finish_time="2026-11-03T01:00:00Z", department_id="42", department_name="Finance",
            tasks_available=True, tasks=[dict(task_id="t", status="COMPLETED",
                start_time="2026-11-02T01:00:00Z", end_time="2026-11-03T01:00:00Z")]))
    row.update(changes)
    return row


@pytest.fixture
def adapter(monkeypatch):
    from deeplinkerp_branding.services.mexico_expense_fx import Conversion
    monkeypatch.setattr(frappe, "flags", frappe._dict())
    monkeypatch.setattr(report_service, "_", lambda text: text)
    calls, reads = [], []
    def make(rows):
        def rate(currency, day):
            calls.append((currency, day))
            return Decimal("7.1")
        return CNExpenseReportSource(dict(configured=True, fingerprint="one", bound_fingerprint="one", corp_id="corp"),
            reader=lambda end: reads.append(end) or rows, conversion=Conversion("CNY", loader=rate))
    return make, calls, reads


def test_approved_pending_and_timings_use_one_local_snapshot_and_monthly_rates(adapter):
    make, rates, reads = adapter
    approved = approval("usd", currency="USD", amount="20", request_date="2026-10-31")
    approved["reporting"]["create_time"] = "2026-10-31T01:00:00Z"
    pending = approval("pending", currency="USD", amount="10", approval_state="pending")
    pending["reporting"]["finish_time"] = None
    rows = [approval(), approved, pending, approval("denied", approval_state="rejected"),
            approval("withdrawn", source_withdrawn=True),
            approval("foreign", oa_identity={"corp_id":"another", "process_instance_id":"foreign"})]
    source = make(rows)
    result = source.list_approved_expenses("2026-01-01", "2026-12-31")
    assert sum(row["amount"] for row in result) == Decimal("242.00")
    assert source.pending_expenses("2026-11-01", "2026-11-30")[0]["amount"] == Decimal("71.00")
    instances, tasks = source.list_approval_efficiency_rows(start_date="2026-11-01", end_date="2026-11-30")
    assert {row["process_instance_id"] for row in instances} == {"a", "pending", "denied"}
    assert rates == [("USD", "2026-11-01")]
    assert reads == ["2026-12-31"]
    assert not frappe.flags.mute_messages


def test_missing_completion_snapshot_and_invalid_amount_are_not_reported_as_zero(adapter):
    make, _, _ = adapter
    for patch, message in (({"reporting":{}}, "Synchronize CN"), ({"amount":None}, "amounts or currencies"),
            ({"source_conflict":True}, "amounts or currencies")):
        with pytest.raises(ReportUnavailable, match=message):
            make([approval(**patch)]).list_approved_expenses("2026-11-01", "2026-11-30")
    row = approval()
    row["reporting"]["finish_time"] = None
    with pytest.raises(ReportUnavailable, match="completion dates"):
        make([row]).list_approved_expenses("2026-11-01", "2026-11-30")


def test_company_scope_uses_exact_budget_organization_not_similar_names(adapter):
    make, _, _ = adapter
    source = make([approval()])
    source.set_scope("Company A", {"42":{"Company A"}})
    assert len(source.list_approved_expenses("2026-11-01", "2026-11-30")) == 1
    source = make([approval()])
    source.set_scope("Company B", {"42":{"Company A"}})
    assert source.list_approved_expenses("2026-11-01", "2026-11-30") == []
    source = make([approval(source_company="Different form organization")])
    source.set_scope("Company A", {"42":{"Company A"}})
    with pytest.raises(ReportUnavailable, match="ownership is incomplete"):
        source.list_approved_expenses("2026-11-01", "2026-11-30")


def test_report_uses_shared_approvals_without_legacy_connections(adapter, monkeypatch):
    make, _, _ = adapter
    source = make([approval()])
    monkeypatch.setattr(report_service, "current_report_source", lambda: source)
    monkeypatch.setattr(report_service, "get_ledger_source", lambda: SimpleNamespace(
        list_budget_entries=lambda **kw: {"total":1,"rows":[dict(dept_id="42",dept_name="Finance",company_display="Company A",
            budget_month="2026-11",budget_kind="non_production",amount=500)]},
        count_detail_rows=lambda *a, **kw: {"non_production":1}))
    for name in ("get_expense_source", "get_budget_expense_source", "get_oa_source"):
        monkeypatch.setattr(report_service, name, lambda: pytest.fail("must not open legacy source"))
    result = report_service.build_report("2026-11-01", "2026-11-30", company="Company A")
    assert result["warnings"] == []
    assert all(result["availability"].values())
    assert result["stats"]["approvedTotal"] == 100
    assert result["stats"]["overallRate"] == "20.0%"
    assert result["approval"]["summary"]["averageCompletionHours"] == 24
    assert result["expense_basis"] == "approved"


def test_report_hides_raw_technical_errors(monkeypatch):
    monkeypatch.setattr(report_service, "_", lambda text: text)
    error = RuntimeError("postgres://private-host/sensitive-detail")
    assert report_service._report_warning(error, "Readable status") == "Readable status"


def test_shanghai_approval_date_can_fall_in_the_next_month(adapter):
    make, _, _ = adapter
    row = approval()
    row["reporting"].update(create_time="2026-10-30T12:00:00Z", finish_time="2026-10-31T18:00:00Z")
    result = make([row]).list_approved_expenses("2026-11-01", "2026-11-30")
    assert result[0]["accounting_date"] == "2026-11-01"


def test_unrelated_pending_organizations_do_not_block_company_approved_spending(adapter):
    make, _, _ = adapter
    source = make([approval(), approval("old", approval_state="pending", request_date="2026-09-01", reporting={})])
    source.set_scope("Company A", {"42": {"Company A"}})
    assert len(source.list_approved_expenses("2026-11-01", "2026-11-30")) == 1
    assert source.pending_expenses("2026-11-01", "2026-11-30") == []
    assert len(source.list_approval_efficiency_rows(start_date="2026-11-01", end_date="2026-11-30")[0]) == 1


def test_finished_approvals_cannot_keep_running_tasks_in_efficiency(adapter):
    make, _, _ = adapter
    row = approval()
    row["reporting"]["tasks"][0].update(status="RUNNING", end_time=None)
    with pytest.raises(ReportUnavailable, match="timing records"):
        make([row]).list_approval_efficiency_rows(start_date="2026-11-01", end_date="2026-11-30")


def test_absent_budget_has_no_execution_rate_but_keeps_approved_amount(adapter, monkeypatch):
    make, _, _ = adapter
    monkeypatch.setattr(report_service, "current_report_source", lambda: make([approval()]))
    monkeypatch.setattr(report_service, "get_ledger_source", lambda: SimpleNamespace(
        list_budget_entries=lambda **kw: {"total": 0, "rows": []}, count_detail_rows=lambda *a, **kw: {}))
    result = report_service.build_report("2026-11-01", "2026-11-30")
    assert result["stats"]["approvedTotal"] == 100
    assert result["stats"]["overallRate"] is None


def prepare_report(monkeypatch, source, budgets=()):
    monkeypatch.setattr(report_service, "current_report_source", lambda: source)
    monkeypatch.setattr(report_service, "get_ledger_source", lambda: SimpleNamespace(
        list_budget_entries=lambda **kw: {"total": len(budgets), "rows": list(budgets)},
        count_detail_rows=lambda *a, **kw: {"non_production": len(budgets)}))
    for name in ("get_expense_source", "get_budget_expense_source", "get_oa_source"):
        monkeypatch.setattr(report_service, name, lambda: pytest.fail("must not fall back to legacy sources"))


@pytest.mark.parametrize("has_pending", [False, True])
def test_unlinked_approved_spending_remains_in_execution_status(adapter, monkeypatch, has_pending):
    make, _, _ = adapter
    rows = [approval()]
    if has_pending:
        pending = approval("pending", approval_state="pending", amount="20")
        pending["reporting"]["finish_time"] = None
        rows.append(pending)
    prepare_report(monkeypatch, make(rows))
    result = report_service.build_report("2026-11-01", "2026-11-30")
    assert len(result["execution_status"]) == 1
    entry = result["execution_status"][0]
    assert entry["total"] == 0
    assert entry["executed"] == result["stats"]["approvedTotal"] == 100
    assert entry["inProgress"] == (20 if has_pending else 0)
    assert entry["unexecuted"] == 0


@pytest.mark.parametrize("failed_period", ["prior", "current"])
def test_annual_rate_failure_preserves_only_valid_current_period_statistics(adapter, monkeypatch, failed_period):
    from deeplinkerp_branding.services.mexico_expense_fx import Conversion
    make, _, reads = adapter
    current = approval()
    prior = approval("prior", request_date="2026-10-01")
    prior["reporting"].update(create_time="2026-10-01T01:00:00Z", finish_time="2026-10-02T01:00:00Z")
    (prior if failed_period == "prior" else current)["currency"] = "USD"
    source = make([prior, current])
    def missing_rate(*args):
        raise RuntimeError("private upstream detail")
    source.conversion = Conversion("CNY", loader=missing_rate)
    prepare_report(monkeypatch, source, [dict(dept_id="42", dept_name="Finance", company_display="Company A",
        budget_month="2026-11", budget_kind="non_production", amount=500)])
    result = report_service.build_report("2026-11-01", "2026-11-30", company="Company A")
    assert result["availability"]["trend_expenses"] is False
    assert all(row["actualExpense"] is None for row in result["trend"])
    assert result["stats"]["nonProductionTotal"] == 500
    assert result["availability"]["approval"] is True
    assert result["availability"]["pending_expenses"] is True
    if failed_period == "prior":
        assert result["availability"]["expenses"] is True
        assert result["stats"]["approvedTotal"] == result["stats"]["budgetSubmittedApprovedTotal"] == 100
        assert result["stats"]["overallRate"] == "20.0%"
        assert result["execution_status"][0]["executed"] == 100
        assert result["warnings"][0].startswith("Spending trend unavailable:")
    else:
        assert result["availability"]["expenses"] is False
        assert result["stats"]["approvedTotal"] is None
        assert result["stats"]["overallRate"] is None
        assert result["execution_status"] == []
    assert "private upstream detail" not in str(result)
    assert reads == ["2026-12-31"]


def test_source_initialization_failure_preserves_budgets_without_falling_back(adapter, monkeypatch):
    make, _, _ = adapter
    prepare_report(monkeypatch, make([]), [dict(dept_id="42", dept_name="Finance", company_display="Company A",
        budget_month="2026-11", budget_kind="non_production", amount=500)])
    def broken_config():
        raise RuntimeError("private configuration details")
    monkeypatch.setattr(report_service, "current_report_source", broken_config)
    result = report_service.build_report("2026-11-01", "2026-11-30", company="Company A")
    assert result["stats"]["nonProductionTotal"] == 500
    assert result["stats"]["operationCount"] == 1
    assert result["stats"]["approvedTotal"] is None
    assert not any(result["availability"].values())
    assert result["dept_distribution"][0]["total"] == 500
    assert len(result["warnings"]) == 1
    assert "private configuration details" not in str(result)


def test_source_configuration_errors_do_not_enqueue_modal_messages(adapter, monkeypatch):
    from budget.integrations import cn_expense_report_source, shared_operating_oa
    from budget.services import ledger_sync_settings
    monkeypatch.setattr(shared_operating_oa, "available", lambda: True)
    def invalid_source():
        assert frappe.flags.mute_messages is True
        raise frappe.ValidationError("bad configuration")
    monkeypatch.setattr(ledger_sync_settings, "current_source", invalid_source)
    frappe.flags.mute_messages = False
    with pytest.raises(frappe.ValidationError):
        cn_expense_report_source.current_report_source()
    assert frappe.flags.mute_messages is False
