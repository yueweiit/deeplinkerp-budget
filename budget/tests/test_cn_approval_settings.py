"""Settings isolation, authorization, concurrency and source compatibility."""
from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import Mock

import frappe
import pytest

from budget.integrations import cn_approval_source as source, shared_operating_oa
from budget.services import cn_approval_settings as settings, cn_approval_workbench as workbench


@pytest.fixture
def runtime(monkeypatch):
	state = SimpleNamespace(saved=None)
	http = SimpleNamespace(configuration=lambda: ("https://oa.invalid", ["corp-A", "corp-B"]),
		read_template_page=Mock(side_effect=AssertionError("save must not call OA")))
	monkeypatch.setattr(shared_operating_oa, "client", lambda: http)
	monkeypatch.setattr(source, "client", lambda: http)
	monkeypatch.setattr(settings, "_", lambda text: text)
	monkeypatch.setattr(source, "_", lambda text: text)
	monkeypatch.setattr(frappe, "throw", lambda message, *a: (_ for _ in ()).throw(ValueError(message)))
	monkeypatch.setattr(frappe, "conf", {"cn_approval_templates": [
		{"corp_id": "corp-A", "process_code": "PROC-test", "approval_type": "budget"}]})
	monkeypatch.setattr(frappe, "session", SimpleNamespace(user="Administrator"))
	monkeypatch.setattr(frappe, "get_roles", lambda: ["System Manager"])
	monkeypatch.setattr(frappe, "db", SimpleNamespace(get_value=lambda *a, **kw: settings.SETTINGS, rollback=Mock()))
	monkeypatch.setattr(frappe, "get_all", lambda *a, **kw: ["Allowed Co", "Hidden Co"])
	monkeypatch.setattr(workbench, "allowed_companies", lambda: {"Allowed Co", "Hidden Co"})
	monkeypatch.setattr(settings, "read_saved", lambda **kw: deepcopy(state.saved))
	monkeypatch.setattr(settings, "persist", lambda values: setattr(state, "saved", deepcopy(values)))
	state.http = http
	return state


def save(values, **changes):
	data = {**values, **changes}
	return settings.save_settings(data["enabled"], data["templates"], data["company_mappings"], values["config_version"])


def test_initial_settings_preserve_snapshot_identity_and_legacy_aliases(runtime):
	frappe.conf["cn_approval_company_map"] = [{"corp_id": "corp-A", "source_name": "Org", "company": "Allowed Co"}]
	url, before = source.configuration()
	values = settings.get_settings()
	save(values)
	assert source.configuration() == (url, before)
	assert settings.company_mappings() == frappe.conf["cn_approval_company_map"]
	assert runtime.saved["templates"][0]["enabled"] == 1
	assert settings.enabled() is True
	runtime.http.read_template_page.assert_not_called()


def test_disabled_templates_never_fall_back_to_defaults(runtime):
	values = settings.get_settings()
	values["templates"][0]["enabled"] = 0
	save(values)
	assert source.configuration()[1] == []
	assert settings.get_settings()["templates"][0]["enabled"] == 0
	assert settings.enabled() is True


def test_company_mapping_changes_revision_but_not_oa_snapshot(runtime):
	values = settings.get_settings();save(values)
	before = source.cache_key(*source.configuration())
	values = settings.get_settings()
	result = save(values, company_mappings=[{"corp_id": "corp-A", "source_name": " Org ", "company": "Allowed Co"}])
	assert result["config_version"] != values["config_version"]
	assert source.cache_key(*source.configuration()) == before
	assert settings.company_mappings()[0]["source_name"] == "Org"
	assert settings.version() == result["config_version"]


def test_conflicting_editor_cannot_overwrite_saved_settings(runtime):
	first = settings.get_settings();second = deepcopy(first)
	save(first, enabled=0)
	with pytest.raises(ValueError, match="Settings changed"):
		save(second)
	assert settings.enabled() is False


def test_existing_template_identity_and_family_are_preserved(runtime):
	values = settings.get_settings()
	with pytest.raises(ValueError, match="Disable historical"):
		save(values, templates=[])
	values["templates"][0]["approval_type"] = "operating"
	with pytest.raises(ValueError, match="cannot change approval form"):
		save(values)
	assert runtime.saved is None


@pytest.mark.parametrize("changes", [
	{"corp_id": "unauthorized"}, {"process_code": "bad"}, {"approval_type": "expense"},
	{"field_mapping": '{"unknown": ["id"]}'}, {"field_mapping": "{broken"},
	{"enabled": "true"}, {"classification_fields": '{"bad": "shape"}'},
])
def test_template_validation_is_shared_with_fetching(runtime, changes):
	values = settings.get_settings();values["templates"][0].update(changes)
	with pytest.raises(ValueError):
		save(values)
	assert runtime.saved is None


def test_template_uniqueness_and_corporation_scoping(runtime):
	values = settings.get_settings();row = values["templates"][0]
	with pytest.raises(ValueError):
		save(values, templates=[row, deepcopy(row)])
	save(values, templates=[row, {**row, "corp_id": "corp-B"}])
	assert len(source.configuration()[1]) == 2


@pytest.mark.parametrize("mapping", [
	{"corp_id": "wrong", "source_name": "Org", "company": "Allowed Co"},
	{"corp_id": "corp-A", "source_name": " ", "company": "Allowed Co"},
	{"corp_id": "corp-A", "source_name": "Org", "company": "Missing Co"},
	{"corp_id": "corp-A", "source_name": "Org", "company": ""},
])
def test_invalid_company_aliases_are_rejected(runtime, mapping):
	with pytest.raises(ValueError, match="Invalid subsidiary"):
		save(settings.get_settings(), company_mappings=[mapping])


def test_aliases_allow_same_name_in_different_corps_but_not_duplicates(runtime):
	a = {"corp_id": "corp-A", "source_name": "Org", "company": "Allowed Co"}
	values = settings.get_settings()
	with pytest.raises(ValueError, match="Duplicate source"):
		save(values, company_mappings=[a, {**a, "source_name": " Org "}])
	save(values, company_mappings=[a, {**a, "corp_id": "corp-B", "company": "Hidden Co"}])
	assert len(settings.company_mappings()) == 2


def test_finance_and_company_restricted_managers_cannot_read_or_write_settings(runtime, monkeypatch):
	frappe.session.user = "finance@example.invalid"
	monkeypatch.setattr(frappe, "get_roles", lambda: ["Accounts Manager"])
	assert not settings.can_manage()
	with pytest.raises(ValueError, match="access to all subsidiaries"):
		settings.get_settings()
	monkeypatch.setattr(frappe, "get_roles", lambda: ["System Manager"])
	monkeypatch.setattr(workbench, "allowed_companies", lambda: {"Allowed Co"})
	for fn in (settings.get_settings, lambda: settings.save_settings(1, [], [], ""),
		lambda: settings.preview_template({}), settings.get_unmapped_sources):
		with pytest.raises(ValueError, match="access to all subsidiaries"):
			fn()


def test_generic_document_writes_cannot_bypass_managed_configuration(runtime):
	with pytest.raises(ValueError, match="through the workbench"):
		settings.validate_managed_document(SimpleNamespace(flags={"ignore_permissions": True}))
	with settings.managed_write():
		settings.validate_managed_document(None)
	with pytest.raises(ValueError):
		settings.validate_managed_document(None)


def test_deadlock_retry_restarts_entire_save_transaction(runtime, monkeypatch):
	fn = Mock(side_effect=[frappe.QueryDeadlockError(), {"ok": True}])
	monkeypatch.setattr(settings, "save_once", fn)
	assert settings.save_settings(1, [], [], "v") == {"ok": True}
	assert fn.call_count == 2
	frappe.db.rollback.assert_called_once()


def test_preview_is_bounded_and_does_not_expose_raw_samples(runtime, monkeypatch):
	from budget.services import cn_approval_contract
	runtime.http.read_template_page.side_effect = None
	runtime.http.read_template_page.return_value = ([{"private": "secret"}, {"private": "secret"}], {})
	monkeypatch.setattr(cn_approval_contract, "project", Mock(side_effect=[{"issues": []}, None]))
	result = settings.preview_template(settings.get_settings()["templates"][0])
	assert result == {"sample_count": 2, "cn_count": 1, "issues": []}
	assert runtime.http.read_template_page.call_args.kwargs == {"limit": 3}
	assert runtime.saved is None


def test_unmapped_sources_only_use_local_authorized_projection_and_paginate(runtime, monkeypatch):
	rows = [{"key": str(i), "kind": "budget", "corp_id": "corp-A", "entity": f"Org {i:03}", "company": ""} for i in range(55)]
	rows += [{**rows[0], "key": "another"}, {"key": "mapped", "kind": "budget", "corp_id": "corp-A", "entity": "Mapped", "company": "Allowed Co"}]
	context = {"aliases": {("corp-A", "Mapped"): "Allowed Co"}, "known_companies": {"Allowed Co"}}
	from budget.integrations import cn_approval_history
	monkeypatch.setattr(cn_approval_history, "read_snapshot", lambda: {"rows": rows})
	monkeypatch.setattr(workbench, "access_context", lambda: context)
	first, last = settings.get_unmapped_sources(), settings.get_unmapped_sources(50)
	assert first["total"] == 55 and len(first["rows"]) == 50
	assert first["rows"][0]["count"] == 2 and len(last["rows"]) == 5
	runtime.http.read_template_page.assert_not_called()


def test_template_history_lifecycle_is_server_owned_and_drafts_do_not_change_snapshot(runtime):
	save(settings.get_settings())
	before = source.cache_key(*source.configuration())
	values = settings.get_settings()
	values['templates'].append({**values['templates'][0], 'process_code': 'PROC-new', 'enabled': 0, 'history_state': 'expected'})
	save(values)
	assert runtime.saved['templates'][-1]['history_state'] == 'never_enabled'
	assert source.cache_key(*source.configuration()) == before
	save(settings.get_settings())
	assert runtime.saved['templates'][-1]['history_state'] == 'never_enabled'
	values = settings.get_settings(); values['templates'][-1]['enabled'] = 1
	save(values)
	assert runtime.saved['templates'][-1]['history_state'] == 'expected'
	values = settings.get_settings(); values['templates'][-1].update(enabled=0, history_state='never_enabled')
	save(values)
	assert runtime.saved['templates'][-1]['history_state'] == 'expected'
	assert source.cache_key(*source.configuration()) == before
	runtime.http.read_template_page.assert_not_called()


@pytest.mark.parametrize('legacy_state', [None, ''])
def test_legacy_history_cannot_be_downgraded_by_client(runtime, legacy_state):
	save(settings.get_settings())
	runtime.saved['templates'][0].update(enabled=0, history_state=legacy_state)
	values = settings.get_settings(); values['templates'][0]['history_state'] = 'never_enabled'
	save(values)
	assert runtime.saved['templates'][0]['history_state'] == 'expected'


def test_history_state_requires_schema_before_settings_can_be_saved(monkeypatch):
	monkeypatch.setattr(settings, '_', lambda text: text)
	monkeypatch.setattr(frappe, 'throw', lambda message, *a: (_ for _ in ()).throw(ValueError(message)))
	monkeypatch.setattr(frappe, 'get_meta', lambda *a: SimpleNamespace(has_field=lambda name: False))
	monkeypatch.setattr(frappe, 'get_doc', lambda *a, **kw: pytest.fail('must not silently drop lifecycle state'))
	with pytest.raises(ValueError, match='site migration'):
		settings.persist({})
