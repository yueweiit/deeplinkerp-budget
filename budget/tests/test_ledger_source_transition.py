from contextlib import contextmanager
from copy import deepcopy
from types import SimpleNamespace

import frappe
import pytest

from budget.integrations import shared_operating_oa as shared
from budget.services import ledger_source_transition as transition, ledger_sync_settings as settings
from budget.services.oa_budget_parser import parse_budget_instance, BUDGET_PROCESS_CODE


def remote(identity="1", amount="100"):
    return {"process_instance_id": identity, "process_code": BUDGET_PROCESS_CODE,
            "status": "COMPLETED", "result": "agree", "raw_payload": {"businessId": "B" + identity},
            "originator_dept_id": "org-" + identity, "originator_dept_name": "Org",
            "form_component_values": [{"name": "预算月份", "value": "2026-11"},
                                      {"name": "预算总金额", "value": amount}, {"name": "执行地区", "value": "中国China"}]}


def local(row):
    entry = parse_budget_instance(row)["entry"]
    return {**entry, "department_id": entry["dept_id"], "total_amount": entry["amount"]}


@pytest.fixture(autouse=True)
def errors(monkeypatch):
    from budget.services import organization_mapping
    monkeypatch.setattr(organization_mapping, "schema_state", lambda: {"level_column": True, "ready": True})
    monkeypatch.setattr(transition, "_", lambda text: text)
    monkeypatch.setattr(settings, "_", lambda text: text)
    monkeypatch.setattr(frappe, "throw", lambda text: (_ for _ in ()).throw(ValueError(text)))


def test_all_existing_approvals_must_match_but_additional_remote_rows_are_not_imported():
    rows = [remote()]
    proof = transition.verify_ledger([local(rows[0])], rows + [remote("2")])
    assert proof["matched"] == proof["additional"] == 1


def test_superseded_remote_approvals_are_not_reported_as_additional_budgets():
    current = {**remote(), "create_time": "2026-11-02T00:00:00"}
    superseded = {**remote("2"), "originator_dept_id": "org-1", "create_time": "2026-11-01T00:00:00"}
    assert transition.verify_ledger([local(current)], [current, superseded])["additional"] == 0


@pytest.mark.parametrize("changes", [{"process_instance_id": "wrong"}, {"process_instance_id": ""},
    {"form_no": "wrong"}, {"department_id": "other"}, {"total_amount": 101},
    {"detail_amount": 100}, {"budget_month": "2026-12-01"}, {"oa_process_code": "other"}])
def test_mismatched_identity_financial_scope_or_template_blocks_switch(changes):
    with pytest.raises(ValueError, match="does not match"):
        transition.verify_ledger([{**local(remote()), **changes}], [remote()])


def test_empty_partial_or_duplicate_snapshots_cannot_authorize_switch():
    for saved, received in [([], []), ([local(remote())], []),
                            ([local(remote()), local(remote())], [remote()]),
                            ([local(remote())], [remote(), remote()])]:
        with pytest.raises(ValueError): transition.verify_ledger(saved, received)


@pytest.fixture
def environment(monkeypatch):
    stored = {"ledger_connection_profile": "budget", "ledger_corp_id": "corp-A", "ledger_sync_enabled": 1,
              "organization_mappings": [], "modified": "v1"}
    conf = {"host": "old-oa", "database": "oa", "port": "5432", "user": "reader", "password": "test-only"}
    monkeypatch.setattr(settings, "_runtime_config_value", lambda *a, **kw: "configured")
    monkeypatch.setattr(settings, "shared_db_config", lambda: conf)
    monkeypatch.setattr(shared, "configuration", lambda corp="": ("https://same-oa.test", corp, corp == "corp-A"))
    state = SimpleNamespace(local=[local(remote())], remote=[remote()], stored=stored, events=[], cache={})
    stored["ledger_source_fingerprint"] = settings.resolve_source(stored)["fingerprint"]

    class Doc(dict):
        def __getattr__(self, key): return self.get(key)
        def __setattr__(self, key, value): self[key] = value
        def get_doc_before_save(self): return self.old
        def save(self):
            assert settings._saving.get()
            settings.validate_settings(self)
            state.events.append("save")
            state.saved = dict(self)
        def add_comment(self, *args): state.events.append("audit")

    monkeypatch.setattr(frappe, "get_doc", lambda *a: Doc(deepcopy(stored), old=Doc(deepcopy(stored))))
    monkeypatch.setattr(frappe, "only_for", lambda role: state.events.append("authorize"))
    monkeypatch.setattr(frappe, "session", SimpleNamespace(user="Administrator"))
    monkeypatch.setattr(frappe, "get_all", lambda *a, **kw: deepcopy(state.local))
    def sql(query, **kw):
        assert "FOR UPDATE" in query
        state.events.append("row_lock")
        return deepcopy(state.local)
    monkeypatch.setattr(frappe, "db", SimpleNamespace(count=lambda *a: len(state.local), sql=sql,
        commit=lambda: state.events.append("commit"), rollback=lambda: state.events.append("rollback")))
    monkeypatch.setattr(shared, "instances", lambda source, deadline: deepcopy(state.remote))
    monkeypatch.setattr(frappe, "cache", SimpleNamespace(
        set_value=lambda key, value, **kw: state.cache.update({key: deepcopy(value)}),
        get_value=lambda key: state.cache.get(key), delete_value=lambda key: state.cache.pop(key, None)))
    @contextmanager
    def mutex(*a):
        state.events.append("lock")
        try: yield True
        finally: state.events.append("unlock")
    monkeypatch.setattr(settings, "sync_lock", mutex)
    state.version = settings.resolve_source(stored)["version"]
    return state


def test_verified_transition_disables_sync_preserves_ledger_and_is_single_use(environment):
    state = environment
    before = deepcopy(state.local)
    proof = transition.preview_shared_source(state.version)
    assert "save" not in state.events
    result = transition.confirm_shared_source(proof["token"], state.version)
    assert result == {"switched": True, "matched": 1, "enabled": False}
    assert state.local == before
    assert state.saved["ledger_connection_profile"] == "operating_oa"
    assert state.saved["ledger_sync_enabled"] == 0
    assert state.saved["ledger_source_fingerprint"] != state.stored["ledger_source_fingerprint"]
    assert state.events[-5:] == ["row_lock", "save", "audit", "commit", "unlock"]
    assert transition._verified.get() is None and not settings._saving.get()
    with pytest.raises(ValueError, match="expired"):
        transition.confirm_shared_source(proof["token"], state.version)
    assert state.events[-2:] == ["rollback", "unlock"]


@pytest.mark.parametrize("change", ["settings", "local", "remote", "user", "corp"])
def test_stale_foreign_or_changed_preview_never_saves(environment, change):
    state = environment
    proof = transition.preview_shared_source(state.version)
    if change == "settings": state.stored["modified"] = "v2"
    if change == "local": state.local[0]["total_amount"] = 150
    if change == "remote": state.remote.append(remote("2"))
    if change == "user": frappe.session.user = "Another Manager"
    if change == "corp": state.stored["ledger_corp_id"] = "corp-B"
    with pytest.raises(ValueError): transition.confirm_shared_source(proof["token"], state.version)
    assert "save" not in state.events and "commit" not in state.events
    assert state.events[-2:] == ["rollback", "unlock"]


def test_native_settings_cannot_bypass_source_proof(environment):
    doc = frappe.get_doc(settings.SETTINGS)
    doc.ledger_connection_profile = "operating_oa"
    with pytest.raises(ValueError, match="contains data"): settings.validate_settings(doc)
    doc.ledger_source_fingerprint = settings.resolve_source(doc)["fingerprint"]
    with pytest.raises(ValueError, match="managed by synchronization"): settings.validate_settings(doc)


@pytest.mark.parametrize("endpoint,kwargs", [(transition.preview_shared_source, {"expected_version": "v"}),
    (transition.confirm_shared_source, {"token": "x", "expected_version": "v"})])
def test_transition_endpoints_authorize_before_reading(monkeypatch, endpoint, kwargs):
    monkeypatch.setattr(frappe, "only_for", lambda *a: (_ for _ in ()).throw(PermissionError()))
    monkeypatch.setattr(frappe, "get_doc", lambda *a: pytest.fail("must authorize first"))
    with pytest.raises(PermissionError): endpoint(**kwargs)
