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
    monkeypatch.setattr(settings, "resolve_source", lambda doc: {"corp_id": "corp-A"})
    entries = [Row(name="B1", department_id="org-1", dept_name="OA A", dept_path_names=None),
               Row(name="B2", department_id="org-1", dept_name="OA B", dept_path_names='["Root","Group","Original Company","Original Dept"]')]
    writes = []
    def get_all(doctype, filters, **kwargs):
        if doctype == "Budget Quota": return []
        assert filters == {"department_id": ["in", ["org-1"]]}
        return entries
    monkeypatch.setattr(frappe, "get_all", get_all)
    monkeypatch.setattr(frappe, "db", SimpleNamespace(set_value=lambda *args: writes.append(args)))
    doc = Row(ledger_connection_profile="budget", ledger_corp_id="corp-A", organization_mappings=[assignment()], old=Row(organization_mappings=[]))
    mapping.apply_changes(doc)
    assert len(writes) == 2
    for _, _, update in writes:
        assert update == {"company": "Company A", "department": "Finance",
                          "reviewed_organization_level": "department", "source_digest": ""}
    writes.clear()
    mapping.apply_changes(Row(ledger_connection_profile="budget", ledger_corp_id="corp-A", organization_mappings=[], old=doc))
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


def test_mapping_changes_update_scoped_quotas_and_restore_original_paths_without_changing_financial_facts(monkeypatch):
    from budget.services.oa_budget_parser import BUDGET_PROCESS_CODE
    monkeypatch.setattr(settings, "resolve_source", lambda doc: {"corp_id": "corp-A"})
    entries = [Row(name="B1", department_id="org-1", dept_name="Original OA Label", dept_path_names=None)]
    base = dict(corp_id="corp-A", oa_process_code=BUDGET_PROCESS_CODE, department_id="org-1",
                total_amount=100, detail_amount=99, status="effective", process_instance_id="instance",
                company_name="Old Company", department_name="Old Department", dept_path_ids=None, dept_path_names=None)
    quotas = [Row(base, name="B1"), Row(base, name="B2",
                dept_path_ids='["root","group","company","org-1"]',
                dept_path_names='["Root","Group","Original Company","Original Department"]'),
              Row(base, name="B3"), Row(base, name="foreign", corp_id="corp-B"),
              Row(base, name="other-template", oa_process_code="other")]
    original = deepcopy(quotas)
    def get_all(doctype, filters, **kwargs):
        if doctype == "Budget Ledger Entry": return entries
        assert filters == {"corp_id": "corp-A", "oa_process_code": BUDGET_PROCESS_CODE, "department_id": ["in", ["org-1"]]}
        return [row for row in quotas if row.corp_id == filters["corp_id"] and row.oa_process_code == filters["oa_process_code"]]
    def set_value(doctype, name, changes):
        row = next(row for row in (entries if doctype == "Budget Ledger Entry" else quotas) if row.name == name)
        row.update(changes)
    monkeypatch.setattr(frappe, "get_all", get_all)
    monkeypatch.setattr(frappe, "db", SimpleNamespace(set_value=set_value))
    doc = Row(organization_mappings=[assignment()], old=Row(organization_mappings=[]))
    mapping.apply_changes(doc)
    assert all(row.company_name == "Company A" and row.department_name == "Finance" for row in quotas[:3])
    assert all(not row.company_dept_id and not row.department_is_company for row in quotas[:3])
    company = Row(organization_mappings=[assignment(department="", organization_level="company")], old=doc)
    mapping.apply_changes(company)
    assert all(row.company_dept_id == "org-1" and row.department_is_company for row in quotas[:3])
    mapping.apply_changes(Row(organization_mappings=[], old=company))
    assert (quotas[0].company_name, quotas[0].department_name) == ("待确认", "Original OA Label")
    assert (quotas[1].company_name, quotas[1].department_name, quotas[1].company_dept_id) == ("Original Company", "Original Department", "company")
    assert (quotas[2].company_name, quotas[2].department_name, quotas[2].organization_is_current) == ("待确认", "", False)
    assert quotas[3:] == original[3:]
    for row, old in zip(quotas, original):
        for field in ("total_amount", "detail_amount", "status", "process_instance_id", "dept_path_ids", "dept_path_names"):
            assert row[field] == old[field]


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
    monkeypatch.setattr(frappe, "get_all", lambda doctype, **kw: [entry] if doctype == "Budget Ledger Entry" else [])
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
