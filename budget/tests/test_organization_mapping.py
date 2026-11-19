from types import SimpleNamespace
from contextlib import contextmanager
from copy import deepcopy

import frappe
import pytest

from budget.api.cn_workbench import issues_for
from budget.services import organization_mapping as mapping, ledger_sync_settings as settings
from budget.services.oa_budget_sync import _entry_values


class Row(dict):
    def __getattr__(self, key): return self.get(key)
    def __setattr__(self, key, value): self[key] = value
    def set(self, key, value): self[key] = value
    def get_doc_before_save(self): return self.get("old")


def assignment(**changes):
    return Row(corp_id="corp-A", department_id="org-1", company="Company A", department="Finance",
               organization_level="department") | changes


@pytest.fixture(autouse=True)
def runtime(monkeypatch):
    monkeypatch.setattr(mapping, "_", lambda text: text)
    monkeypatch.setattr(mapping.frappe, "throw", lambda text: (_ for _ in ()).throw(ValueError(text)))
    monkeypatch.setattr(mapping, "schema_state", lambda: {"level_column": True, "ready": True})


def test_reviewed_assignment_survives_first_and_repeated_sync_without_fabricated_paths():
    raw = {"entry": {"dept_id": "org-1", "dept_name": "Legacy full OA name", "amount": 100,
                     "detail_amount": 100, "budget_month": "2026-11-01"}}
    source = {"corp_id": "corp-A", "organization_mappings": [assignment()]}
    assert _entry_values(raw, {}, "now")["company"] == "待确认"
    paths = mapping.overlay_paths({}, source)
    first, second = [_entry_values(raw, paths, stamp) for stamp in ("first", "second")]
    assert first["company"] == second["company"] == "Company A"
    assert first["department"] == second["department"] == "Finance"
    assert first["dept_name"] == "Legacy full OA name"
    assert first["dept_path_ids"] is first["dept_path_names"] is None
    assert issues_for(first) == []
    assert mapping.overlay_paths({}, {**source, "corp_id": "another-corp"}) == {}
    source["organization_mappings"] = [assignment(department="", organization_level="company")]
    company = _entry_values(raw, mapping.overlay_paths({}, source), "third")
    assert company["department"] == ""
    assert issues_for(company) == []
    assert "Budget department is missing" in issues_for({**company, "department_id": ""})


@pytest.mark.parametrize("changes,error", [
    ({"corp_id": "corp-B"}, "corporation"), ({"department_id": ""}, "valid organization"),
    ({"company": "待确认"}, "valid organization"), ({"company": ""}, "valid organization"),
    ({"department": ""}, "requires a department"), ({"organization_level": "other"}, "organization level"),
    ({"organization_level": "company"}, "must leave the department"),
])
def test_invalid_assignments_rejected(changes, error):
    with pytest.raises(ValueError, match=error):
        mapping.validate(Row(organization_mappings=[Row(assignment(**changes))]), "corp-A")


def test_mapping_uniqueness_and_explicit_corporation():
    row = Row(assignment(corp_id=""))
    doc = Row(organization_mappings=[row])
    mapping.validate(doc, "corp-A")
    assert row.corp_id == "corp-A"
    doc.organization_mappings.append(Row(assignment()))
    with pytest.raises(ValueError, match="only one"):
        mapping.validate(doc, "corp-A")


def test_mapping_updates_and_removal_preserve_oa_facts_and_invalidate_digest(monkeypatch):
    entries = [Row(name="B1", department_id="org-1", dept_name="OA A", dept_path_names=None),
               Row(name="B2", department_id="org-1", dept_name="OA B", dept_path_names='["Root","Group","Original Company","Original Dept"]')]
    writes = []
    def get_all(doctype, filters, **kwargs):
        assert filters == {"department_id": ["in", ["org-1"]]}
        return entries
    monkeypatch.setattr(frappe, "get_all", get_all)
    monkeypatch.setattr(frappe, "db", SimpleNamespace(set_value=lambda *args: writes.append(args)))
    doc = Row(organization_mappings=[assignment()], old=Row(organization_mappings=[]))
    mapping.apply_changes(doc)
    assert len(writes) == 2
    for _, _, update in writes:
        assert update == {"company": "Company A", "department": "Finance",
                          "reviewed_organization_level": "department", "source_digest": ""}
    writes.clear()
    mapping.apply_changes(Row(organization_mappings=[], old=doc))
    assert writes[0][2]["company"] == "待确认"
    assert writes[1][2]["company"] == "Original Company"
    assert writes[1][2]["department"] == "Original Dept"


def test_mapping_changes_invalidate_settings_version_without_needing_modified_timestamp(monkeypatch):
    from budget.integrations import shared_operating_oa
    monkeypatch.setattr(shared_operating_oa, "configuration", lambda *a: ("https://oa.test", "corp-A", True))
    doc = Row(ledger_connection_profile="operating_oa", ledger_corp_id="corp-A", organization_mappings=[])
    old = settings.resolve_source(doc)
    doc.organization_mappings = [assignment()]
    new = settings.resolve_source(doc)
    assert old["version"] != new["version"]
    assert old["fingerprint"] == new["fingerprint"]


@pytest.mark.parametrize("endpoint,args", [(mapping.get_mapping_context, {"name": "X"}),
    (mapping.save_mapping, dict(name="X", company="A", department="D", organization_level="department", expected_version="v"))])
def test_review_endpoints_require_manager_before_reading(monkeypatch, endpoint, args):
    def deny(roles):
        assert roles == "System Manager"
        raise PermissionError
    monkeypatch.setattr(frappe, "only_for", deny)
    monkeypatch.setattr(frappe, "get_doc", lambda *a: pytest.fail("no unauthorized read"))
    with pytest.raises(PermissionError): endpoint(**args)


def test_review_save_is_versioned_locked_and_applies_to_existing_entries(monkeypatch):
    from budget.integrations import shared_operating_oa
    events, writes = [], []
    before = Row(ledger_connection_profile="operating_oa", ledger_corp_id="corp-A", organization_mappings=[])
    class Settings(Row):
        def append(self, field, row): self[field].append(Row(row))
        def save(self):
            assert settings._saving.get()
            settings.validate_settings(self)
            mapping.apply_changes(self)
            events.append("save")
    doc = Settings(deepcopy(before), old=before)
    monkeypatch.setattr(shared_operating_oa, "configuration", lambda *a: ("https://oa.test", "corp-A", True))
    monkeypatch.setattr(settings, "_", lambda text: text)
    monkeypatch.setattr(frappe, "only_for", lambda *a: None)
    monkeypatch.setattr(frappe, "get_doc", lambda *a: doc)
    entry = Row(name="B1", department_id="org-1", dept_name="Old OA Name", dept_path_names=None)
    monkeypatch.setattr(frappe, "get_all", lambda *a, **kw: [entry])
    monkeypatch.setattr(frappe, "db", SimpleNamespace(get_value=lambda *a, **kw: entry,
        set_value=lambda *a: writes.append(a), commit=lambda: events.append("commit"), rollback=lambda: events.append("rollback")))
    @contextmanager
    def mutex(*a):
        events.append("lock")
        try: yield True
        finally: events.append("unlock")
    monkeypatch.setattr(settings, "sync_lock", mutex)
    arguments = dict(name="B1", company="Company A", department="Finance", organization_level="department",
                     expected_version=settings.resolve_source(doc)["version"])
    assert mapping.save_mapping(**arguments) == {"saved": True}
    assert events == ["lock", "save", "commit", "unlock"]
    assert doc.organization_mappings[0].department_id == "org-1"
    assert writes[0][2]["company"] == "Company A"
    with pytest.raises(ValueError, match="settings changed"):
        mapping.save_mapping(**arguments)
    assert len(writes) == 1
