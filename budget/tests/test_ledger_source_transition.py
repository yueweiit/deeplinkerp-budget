from contextlib import contextmanager
from copy import deepcopy
from types import SimpleNamespace

import frappe
import pytest

from budget.integrations import shared_operating_oa as shared
from budget.services import ledger_source_transition as transition, ledger_sync_settings as settings
from budget.services.oa_budget_parser import parse_budget_instance, BUDGET_PROCESS_CODE


def remote(identity="1", amount="100"):
    return {"corp_id": "corp-A", "create_time": "2026-11-01T00:00:00", "process_instance_id": identity, "process_code": BUDGET_PROCESS_CODE,
            "status": "COMPLETED", "result": "agree", "raw_payload": {"businessId": "B" + identity},
            "originator_dept_id": "org-" + identity, "originator_dept_name": "Org",
            "form_component_values": [{"name": "预算月份", "value": "2026-11"},
                                      {"name": "预算总金额", "value": amount}, {"name": "执行地区", "value": "中国China"}]}


def local(row):
    entry = parse_budget_instance(row)["entry"]
    return {**entry, "department_id": entry["dept_id"], "total_amount": entry["amount"]}


@pytest.fixture(autouse=True)
def errors(monkeypatch):
    monkeypatch.setattr(frappe, "get_system_settings", lambda key: "Asia/Shanghai")
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
    state = SimpleNamespace(local=[local(remote())], remote=[remote()], quotas=[], subjects=[], stored=stored, events=[], cache={})
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
    def get_all(doctype, **kw):
        rows = {"Budget Ledger Entry": state.local, "Budget Quota": state.quotas, "Budget Quota Subject": state.subjects}[doctype]
        return deepcopy(rows)
    monkeypatch.setattr(frappe, "get_all", get_all)
    def sql(query, *args, **kw):
        assert "FOR UPDATE" in query
        state.events.append("row_lock")
        doctype = "Budget Quota Subject" if "`tabBudget Quota Subject`" in query else ("Budget Quota" if "`tabBudget Quota`" in query else "Budget Ledger Entry")
        return get_all(doctype)
    monkeypatch.setattr(frappe, "db", SimpleNamespace(count=lambda doctype, *a: len(get_all(doctype)), sql=sql,
        commit=lambda: state.events.append("commit"), rollback=lambda: state.events.append("rollback")))
    monkeypatch.setattr(shared, "instances", lambda source, deadline, **kw: deepcopy(state.remote))
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


def with_quotas(state):
    from budget.services.oa_budget_quota import project
    from budget.services.quota_sync import _quota_values
    state.remote[0]["form_component_values"].append({"name": "预算项目", "value": [
        {"rowValue": [{"label": "预算明细", "value": "Travel"}, {"label": "人民币金额", "value": "100"}]}]})
    state.local = [local(state.remote[0])]
    rows, subjects = project(state.remote, {}, {"corp_id": "corp-A"})
    state.quotas = [_quota_values(row, None) for row in rows]
    state.subjects = [{**row, "quota_form_no": row["quota_id"]} for row in subjects]


@pytest.mark.parametrize("quota_only", [False, True])
def test_verified_transition_accepts_quota_only_and_combined_history_without_rewriting_it(environment, quota_only):
    state = environment
    with_quotas(state)
    if quota_only: state.local = []
    original = deepcopy((state.local, state.quotas, state.subjects))
    doc = frappe.get_doc(settings.SETTINGS)
    doc.ledger_connection_profile = "operating_oa"
    with pytest.raises(ValueError, match="contains data"): settings.validate_settings(doc)
    proof = transition.preview_shared_source(state.version)
    assert proof["matched"] == (0 if quota_only else 1) and proof["matched_quotas"] == 1
    transition.confirm_shared_source(proof["token"], state.version)
    assert state.saved["ledger_connection_profile"] == "operating_oa"
    assert state.saved["ledger_sync_enabled"] == 0
    assert (state.local, state.quotas, state.subjects) == original


@pytest.mark.parametrize("field,value", [("process_instance_id", "other"), ("form_no", "other"),
    ("total_amount", 101), ("detail_amount", 99), ("corp_id", "corp-B"), ("oa_process_code", "other"),
    ("budget_month", "2026-12-01"), ("department_id", "other")])
def test_mismatched_historical_quota_blocks_transition(environment, field, value):
    state = environment
    with_quotas(state)
    state.local = []
    state.quotas[0][field] = value
    with pytest.raises(ValueError, match="budget quota"):
        transition.preview_shared_source(state.version)
    assert "save" not in state.events


@pytest.mark.parametrize("change", ["amount", "subject", "status", "added-quota", "remote"])
def test_quota_changes_between_preview_and_confirmation_block_transition(environment, change):
    state = environment
    with_quotas(state)
    state.local = []
    proof = transition.preview_shared_source(state.version)
    if change == "amount": state.quotas[0]["total_amount"] = 101
    if change == "subject": state.subjects[0]["subject_name"] = "Other"
    if change == "status": state.quotas[0]["status"] = "deactivated"
    if change == "added-quota": state.quotas.append({**state.quotas[0], "form_no": "new"})
    if change == "remote": state.remote[0]["result"] = "refuse"
    with pytest.raises(ValueError): transition.confirm_shared_source(proof["token"], state.version)
    assert "save" not in state.events and "commit" not in state.events
    assert state.events[-2:] == ["rollback", "unlock"]


def test_quota_history_reads_and_locks_only_the_configured_corporation_and_template(monkeypatch):
    calls = []
    def sql(query, params, **kwargs):
        calls.append(query)
        assert "FOR UPDATE" in query
        assert "q.corp_id = %(corp_id)s" in query and params["corp_id"] == "corp-A"
        assert "q.oa_process_code = %(oa_process_code)s" in query and params["oa_process_code"] == BUDGET_PROCESS_CODE
        return []
    monkeypatch.setattr(frappe, "db", SimpleNamespace(sql=sql))
    assert transition._local_quotas({"corp_id": "corp-A"}, lock_rows=True) == ([], [])
    assert len(calls) == 2


@pytest.mark.parametrize("endpoint,kwargs", [(transition.preview_shared_source, {"expected_version": "v"}),
    (transition.confirm_shared_source, {"token": "x", "expected_version": "v"})])
def test_transition_endpoints_authorize_before_reading(monkeypatch, endpoint, kwargs):
    monkeypatch.setattr(frappe, "only_for", lambda *a: (_ for _ in ()).throw(PermissionError()))
    monkeypatch.setattr(frappe, "get_doc", lambda *a: pytest.fail("must authorize first"))
    with pytest.raises(PermissionError): endpoint(**kwargs)
