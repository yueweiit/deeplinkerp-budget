from types import SimpleNamespace

import pytest

from budget.integrations import shared_operating_oa as source
from budget.services import ledger_sync_settings as settings
from budget.services import oa_budget_sync as sync
from budget.services.oa_budget_parser import BUDGET_PROCESS_CODE


@pytest.fixture
def shared(monkeypatch):
    monkeypatch.setattr(source, "available", lambda: True)
    monkeypatch.setattr(source, "_", lambda text: text)
    monkeypatch.setattr(source.frappe, "throw", lambda message: (_ for _ in ()).throw(ValueError(message)))
    http = SimpleNamespace(configuration=lambda: ("https://oa.test", ("corp-A",)), MAX_PAGES=3)
    monkeypatch.setattr(source, "client", lambda: http)
    return http


def row(identity="one", region="中国China", **changes):
    return {"process_instance_id": identity, "status": "COMPLETED", "result": "agree",
            "raw_payload": {"businessId": identity}, "process_code": BUDGET_PROCESS_CODE,
            "form_component_values": [{"name": "执行地区", "value": region}], **changes}


def binding():
    return {"corp_id": "corp-A", "profile": "operating_oa", "connection": {"url": "https://oa.test"}}


def test_shared_source_inherits_one_corporation_but_does_not_guess_among_multiple(shared):
    assert source.configuration() == ("https://oa.test", "corp-A", True)
    shared.configuration = lambda: ("https://oa.test", ("corp-A", "corp-B"))
    assert source.configuration() == ("https://oa.test", "", False)
    assert source.configuration("corp-B")[-1] is True
    assert source.configuration("corp-C")[-1] is False


def test_shared_scan_is_paginated_and_keeps_only_cn_approved_budgets(shared):
    calls, deadlines = [], []
    def read(url, corp, template, cursor=None):
        calls.append((url, corp, template, cursor))
        rows = ([row(), row("mx", "墨西哥Mexico"), row("running", status="RUNNING"),
                 row("deleted", deleted_at="2026-11-01"), row("rejected", result="refuse")]
                if cursor is None else [row("two", "CN")])
        return rows, {"hasMore": cursor is None, "nextCursor": "next", "syncUntil": "snapshot"}
    shared.read_template_page = read
    assert [r["process_instance_id"] for r in source.instances(binding(), lambda: deadlines.append(True))] == ["one", "two"]
    assert calls == [("https://oa.test", "corp-A", BUDGET_PROCESS_CODE, None),
                     ("https://oa.test", "corp-A", BUDGET_PROCESS_CODE, "next")]
    assert len(deadlines) == 2


@pytest.mark.parametrize("failure", ["snapshot", "duplicate", "cursor", "limit"])
def test_incomplete_scans_raise_before_any_snapshot_can_be_written(shared, failure):
    count = 0
    def read(*args, **kwargs):
        nonlocal count
        count += 1
        return [row("same" if failure == "duplicate" else str(count))], {
            "hasMore": True, "nextCursor": "same" if failure == "cursor" else str(count),
            "syncUntil": str(count) if failure == "snapshot" else "snapshot"}
    shared.read_template_page = read
    with pytest.raises(ValueError):
        source.instances(binding(), lambda: None)


def test_changed_shared_scope_is_rejected_before_reading(shared):
    shared.read_template_page = lambda *a, **kw: pytest.fail("must not request another source")
    with pytest.raises(ValueError, match="configuration changed"):
        source.instances({**binding(), "corp_id": "not-allowed"}, lambda: None)


def test_unconfigured_cn_defaults_to_shared_api_without_enabling_sync(shared, monkeypatch):
    monkeypatch.setattr(settings, "_runtime_config_value", lambda *a, **kw: "")
    monkeypatch.setattr(settings.frappe, "db", SimpleNamespace(count=lambda *a: 0, has_column=lambda *a: False))
    result = settings.resolve_source({"ledger_connection_profile": "budget"})
    assert result["profile"] == "operating_oa"
    assert result["configured"] and not result["enabled"]
    assert result["corp_id"] == "corp-A"
    assert settings.blocked_reason(result) == "disabled"
    public = settings.public_settings(result)
    assert "connection" not in public
    assert public["organization_review_ready"] is False


def test_existing_unbound_ledger_does_not_automatically_change_source(shared, monkeypatch):
    monkeypatch.setattr(settings, "_runtime_config_value", lambda *a, **kw: "")
    monkeypatch.setattr(settings, "shared_db_config", lambda: {"port": "5432"})
    monkeypatch.setattr(settings.frappe, "db", SimpleNamespace(count=lambda *a: 2))
    result = settings.resolve_source({"ledger_connection_profile": "budget"})
    assert result["profile"] == "budget"
    assert not result["configured"]


def test_old_enabled_setting_cannot_automatically_activate_a_new_source(shared, monkeypatch):
    monkeypatch.setattr(settings, "_runtime_config_value", lambda *a, **kw: "")
    monkeypatch.setattr(settings, "shared_db_config", lambda: {"port": "5432"})
    monkeypatch.setattr(settings.frappe, "db", SimpleNamespace(count=lambda *a: 0))
    result = settings.resolve_source({"ledger_connection_profile": "budget", "ledger_sync_enabled": 1})
    assert result["profile"] == "budget"
    assert settings.blocked_reason(result) == "not_configured"


def test_shared_budget_sync_does_not_open_the_old_database(shared, monkeypatch):
    shared.read_template_page = lambda *a, **kw: ([row()], {"hasMore": False, "syncUntil": "snapshot"})
    monkeypatch.setattr(sync, "pg_fetch", lambda *a, **kw: pytest.fail("legacy database must not be opened"))
    monkeypatch.setattr(source.frappe, "get_all", lambda *a, **kw: [
        {"department_id": "org-A", "dept_path_ids": '["root","org-A"]', "dept_path_names": '["Root","A"]'},
        {"department_id": "org-B", "dept_path_ids": 'invalid', "dept_path_names": '[]'}])
    with settings.source_context(binding()):
        assert len(sync._fetch_instances()) == 1
        assert sync._department_paths() == {"org-A": {"ids": ["root", "org-A"], "names": ["Root", "A"]}}
