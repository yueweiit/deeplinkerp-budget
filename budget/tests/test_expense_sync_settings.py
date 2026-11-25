"""Unified edits retain exact parsing rules and never publish a partial save."""
from copy import deepcopy
from types import SimpleNamespace

import frappe
import pytest

from budget.services import expense_sync_settings as s, expense_sync


def template(**values):
	return frappe._dict(corp_id="corp", process_code="PROC-test", approval_type="operating",
		template_name="Application", enabled=1, field_mapping='{"company":["CNField"]}', classification_fields="", **values)


def configuration():
	cn = dict(enabled=1, config_version="cn-v1", templates=[template()], company_mappings=[], management_rules={"warning_percent": 90, "category_mappings": []})
	mx_row = template()
	mx_row.update(template_name="Expense", field_mapping='{"company":["MXField"]}', finance_node_names="Finance", finance_activity_ids="node", enabled=0)
	mx = frappe._dict(enabled=0, config_version="mx-v1", templates=[mx_row], company_mappings=[], business_timezone="America/Mexico_City")
	return cn, mx


def values(cn, mx):
	return dict(sync_mode="mixed", versions={"cn": "cn-v1", "mx": "mx-v1"},
		business_timezone=mx.business_timezone, templates=s.merge_templates(cn["templates"], mx.templates),
		company_mappings=[], management_rules=cn["management_rules"])


def test_shared_template_is_one_row_and_round_trips_both_parsers():
	cn, mx = configuration()
	rows = s.merge_templates(cn["templates"], mx.templates)
	assert len(rows) == 1 and rows[0]["processing_rule"] == "both" and rows[0]["sync_mode"] == "mixed"
	result = s.split_templates(rows, cn, mx)
	for source, original in (("cn", cn["templates"][0]), ("mx", mx.templates[0])):
		assert result[source][0]["field_mapping"] == original.field_mapping
		assert result[source][0]["template_name"] == original.template_name
		assert result[source][0]["enabled"] == original.enabled
	assert result["mx"][0]["finance_activity_ids"] == "node"
	rows[0]["sync_mode"] = "enabled"
	assert all(row["enabled"] == 1 for rows in s.split_templates(rows, cn, mx).values() for row in rows)


@pytest.mark.parametrize("change", ["remove", "duplicate", "identity", "processing_rule"])
def test_existing_bindings_cannot_be_removed_moved_or_duplicated(monkeypatch, change):
	monkeypatch.setattr(s, "_", lambda text: text)
	monkeypatch.setattr(frappe, "throw", lambda *args: (_ for _ in ()).throw(frappe.ValidationError()))
	cn, mx = configuration()
	rows = values(cn, mx)["templates"]
	if change == "remove": rows.clear()
	elif change == "duplicate": rows.append(deepcopy(rows[0]))
	elif change == "identity": rows[0]["corp_id"] = "another"
	else: rows[0]["processing_rule"] = "expense"
	with pytest.raises(frappe.ValidationError): s.split_templates(rows, cn, mx)


def test_mappings_preserve_matching_domain_and_legacy_exclusion():
	result = s.split_mappings([
		{"match_kind": "organization", "corp_id": "corp", "source_name": "Department", "company": "A"},
		{"match_kind": "entity", "source_name": "Entity", "company": "B"},
		{"match_kind": "department", "source_name": "Excluded", "excluded": 1, "reason": "historical exclusion"},
	])
	assert result["cn"] == [{"corp_id": "corp", "source_name": "Department", "company": "A"}]
	assert result["mx"][0]["entity_kind"] == "entity" and result["mx"][0]["expense_scope"] == "mx"
	assert result["mx"][1]["expense_scope"] == "non_mx" and result["mx"][1]["company"] == ""


@pytest.fixture
def runtime(monkeypatch):
	cn, mx = configuration()
	env = SimpleNamespace(cn=cn, mx=mx, staged=[], published=[], rollbacks=0)
	monkeypatch.setattr(s, "read", lambda **kw: (cn, mx, ["corp"]))
	monkeypatch.setattr(s, "_", lambda text: text)
	monkeypatch.setattr(frappe, "throw", lambda *args: (_ for _ in ()).throw(frappe.ValidationError()))
	def rollback():
		env.rollbacks += 1
		env.staged.clear()
	monkeypatch.setattr(frappe, "db", SimpleNamespace(rollback=rollback))
	monkeypatch.setattr(s.mx, "save_settings", lambda **kwargs: env.staged.append(("mx", kwargs)))
	monkeypatch.setattr(s.cn, "save_once", lambda **kwargs: env.staged.append(("cn", kwargs)))
	return env


def test_versions_checked_before_either_source_writes(runtime):
	data = values(runtime.cn, runtime.mx)
	data["versions"]["cn"] = "obsolete"
	with pytest.raises(frappe.ValidationError): s.save_settings(data)
	assert runtime.staged == [] and runtime.rollbacks == 1


def test_second_source_failure_rolls_back_first_source(runtime, monkeypatch):
	monkeypatch.setattr(s.cn, "save_once", lambda **kw: (_ for _ in ()).throw(frappe.ValidationError()))
	with pytest.raises(frappe.ValidationError): s.save_settings(values(runtime.cn, runtime.mx))
	assert not runtime.staged and runtime.rollbacks == 1


def test_deadlock_retries_the_whole_pair_and_keeps_mixed_switches(runtime, monkeypatch):
	attempts = []
	def save(**kwargs):
		attempts.append(kwargs)
		if len(attempts) == 1: raise frappe.QueryDeadlockError()
		runtime.staged.append(("cn", kwargs))
	monkeypatch.setattr(s.cn, "save_once", save)
	assert s.save_settings(values(runtime.cn, runtime.mx)) == {"ok": True}
	assert runtime.rollbacks == 1 and [source for source, _ in runtime.staged] == ["mx", "cn"]
	assert runtime.staged[0][1]["enabled"] == 0 and runtime.staged[1][1]["enabled"] == 1


def test_common_choices_are_suggested_only_when_unambiguous():
	cn, mx = configuration()
	rows = s.merge_templates(cn["templates"], mx.templates)
	assert s.suggested_defaults(rows, ["corp"], {}) == {"corp_id": "corp", "processing_rule": "both"}
	rows.append({"corp_id": "other", "processing_rule": "application"})
	assert s.suggested_defaults(rows, ["corp", "other"], {}) == {"corp_id": "", "processing_rule": ""}
	assert s.suggested_defaults(rows, ["corp", "other"], {"corp_id": "corp", "processing_rule": "expense"}) == {"corp_id": "corp", "processing_rule": "expense"}


def test_common_settings_inherit_to_drafts_without_overwriting_history():
	cn, mx = configuration()
	rows = values(cn, mx)["templates"]
	rows.append({"approval_type": "operating", "template_name": "New", "process_code": "PROC-new",
		"corp_id": "stale", "processing_rule": "expense", "sync_mode": "disabled"})
	result = s.split_templates(rows, cn, mx, corporations=["corp"],
		template_defaults={"corp_id": "corp", "processing_rule": "application"})
	assert len(result["cn"]) == 2 and len(result["mx"]) == 1
	assert result["cn"][1]["corp_id"] == "corp"
	assert rows[1]["corp_id"] == "stale"
	assert result["mx"][0]["field_mapping"] == mx.templates[0].field_mapping


@pytest.mark.parametrize("kind,expected", [("budget", "application"), ("expense", "expense"), ("operating", "both"), ("purchase", "both")])
def test_shared_combined_rules_respect_supported_parsers(kind, expected):
	assert s.inherit_defaults({"approval_type": kind}, {"corp_id": "corp", "processing_rule": "both"})["processing_rule"] == expected


def test_common_settings_version_is_checked_before_writes(runtime, monkeypatch):
	monkeypatch.setattr(s, "read_template_defaults", lambda **kwargs: {"corp_id": "corp", "processing_rule": "application"})
	data = values(runtime.cn, runtime.mx)
	data.update(template_defaults={"corp_id": "corp", "processing_rule": "both"}, template_defaults_version=s.mx.digest({}))
	with pytest.raises(frappe.ValidationError): s.save_settings(data)
	assert runtime.staged == []


def test_common_settings_save_shares_the_source_transaction(runtime, monkeypatch):
	monkeypatch.setattr(s, "read_template_defaults", lambda **kwargs: {})
	monkeypatch.setattr(s, "save_template_defaults", lambda value: runtime.staged.append(("defaults", value)))
	data = values(runtime.cn, runtime.mx)
	data.update(template_defaults={"corp_id": "corp", "processing_rule": "both"}, template_defaults_version=s.mx.digest({}))
	s.save_settings(data)
	assert [source for source, _ in runtime.staged] == ["mx", "cn", "defaults"]
	runtime.staged.clear()
	monkeypatch.setattr(s, "save_template_defaults", lambda value: (_ for _ in ()).throw(frappe.ValidationError()))
	with pytest.raises(frappe.ValidationError): s.save_settings(data)
	assert runtime.staged == [] and runtime.rollbacks == 1


def test_invalid_corporation_in_common_settings_cannot_write(runtime, monkeypatch):
	monkeypatch.setattr(s, "read_template_defaults", lambda **kwargs: {})
	data = values(runtime.cn, runtime.mx)
	data.update(template_defaults={"corp_id": "unauthorized", "processing_rule": "both"}, template_defaults_version=s.mx.digest({}))
	with pytest.raises(frappe.ValidationError): s.save_settings(data)
	assert runtime.staged == []


def test_preview_uses_the_same_common_binding_as_save(runtime, monkeypatch):
	seen = []
	def cn_preview(row):
		seen.append(("cn", row))
		return {"sample_count": 1, "issues": []}
	def mx_preview(row, timezone):
		seen.append(("mx", row))
		return {"applications": 1, "issues": []}
	monkeypatch.setattr(s.cn, "preview_template", cn_preview)
	monkeypatch.setattr(s.mx, "preview_template", mx_preview)
	row = {"template_name": "New", "process_code": " \tPROC-new \n", "approval_type": "operating", "finance_node_names": "Finance"}
	result = s.preview_template(row, "America/Mexico_City", {"corp_id": "corp", "processing_rule": "both"})
	assert [item["rule"] for item in result] == ["application", "expense"]
	assert all(item["corp_id"] == "corp" for _, item in seen)
	assert all(item["process_code"] == "PROC-new" for _, item in seen)
	assert row["process_code"] == " \tPROC-new \n"
	seen.clear()
	with pytest.raises(frappe.ValidationError):
		s.preview_template(row, "America/Mexico_City", {"corp_id": "unauthorized", "processing_rule": "both"})
	assert seen == []


@pytest.mark.parametrize("defaults", [None, {"corp_id": "corp", "processing_rule": "both"}])
def test_pasted_codes_reach_both_native_parsers_with_the_same_identity(defaults):
	from budget.integrations.cn_approval_source import normalize_templates
	code = "PROC-14972EC1-2E3B-47DA-8346-9B1DBFE578C5"
	row = {"corp_id": "corp", "processing_rule": "both", "template_name": "New", "process_code": f" \t{code}\n ",
		"approval_type": "operating", "finance_node_names": "Finance", "sync_mode": "disabled"}
	result = s.split_templates([row], {"templates": []}, frappe._dict(templates=[]), corporations=["corp"], template_defaults=defaults)
	cn_row = normalize_templates(result["cn"], ["corp"])[0]
	mx_row = s.mx.templates.normalize(result["mx"][0])
	assert cn_row["process_code"] == mx_row["process_code"] == code
	assert row["process_code"] != code


def test_pasted_codes_cannot_bypass_duplicate_detection(monkeypatch):
	monkeypatch.setattr(s, "_", lambda text: text)
	monkeypatch.setattr(frappe, "throw", lambda text: (_ for _ in ()).throw(frappe.ValidationError(text)))
	cn, mx = configuration()
	rows = values(cn, mx)["templates"]
	copy = {**rows[0], "key": None, "process_code": " \tPROC-test \n"}
	with pytest.raises(frappe.ValidationError, match="row 2:.*already listed"):
		s.split_templates([*rows, copy], cn, mx)


def test_normalization_does_not_silently_change_historical_bindings():
	row = {"key": "saved", "corp_id": "corp", "process_code": "PROC-old ", "processing_rule": "application"}
	assert s.inherit_defaults(row, {"corp_id": "new", "processing_rule": "both"}) == row


@pytest.mark.parametrize("change,message", [
	("corp_id", "Corporation ID is required"), ("processing_rule", "Processing rules is required"),
	("unauthorized", "source corporation is not authorized"), ("duplicate", "already listed"),
])
def test_missing_duplicate_and_unauthorized_templates_have_distinct_row_errors(monkeypatch, change, message):
	monkeypatch.setattr(s, "_", lambda text: text)
	monkeypatch.setattr(frappe, "throw", lambda text: (_ for _ in ()).throw(frappe.ValidationError(text)))
	cn, mx = configuration()
	rows = values(cn, mx)["templates"]
	if change == "duplicate": rows.append(deepcopy(rows[0]))
	elif change == "unauthorized": rows[0]["corp_id"] = "other"
	else: rows[0].pop(change)
	with pytest.raises(frappe.ValidationError, match=message):
		s.split_templates(rows, cn, mx, corporations=["corp"])


def test_read_requires_both_existing_configuration_permissions(monkeypatch):
	cn, mx = configuration()
	monkeypatch.setattr(s.cn, "require_manager", lambda: None)
	monkeypatch.setattr(s.mx, "_settings", lambda **kwargs: mx)
	monkeypatch.setattr(s.mx, "_require_settings_access", lambda doc: (_ for _ in ()).throw(frappe.PermissionError()))
	monkeypatch.setattr(s.cn, "read_saved", lambda **kwargs: pytest.fail("unauthorized configuration read"))
	with pytest.raises(frappe.PermissionError): s.get_settings()


def test_overall_failure_never_counts_scanned_but_unpublished_templates():
	sources = {"cn": {"status": "failed", "completed_templates": 11}, "mx": {"status": "success"}}
	rows = [{"status": "failed", "last_update_at": None}, {"status": "success", "last_update_at": "2026-11-24"}]
	result = expense_sync.overall(sources, rows)
	assert result == {"status": "partial_failure", "completed": 1, "total": 2, "failed": 1, "last_update_at": None}


def test_progress_deduplicates_shared_templates_and_requires_all_bindings(monkeypatch):
	cn, mx = configuration()
	mx.templates[0].enabled = 1
	monkeypatch.setattr(s.cn, "read_saved", lambda: cn)
	monkeypatch.setattr(s.mx, "_settings", lambda: mx)
	key = s.mx._template_key(mx.templates[0])
	sources = {"cn": {"configured": True, "status": "failed", "completed_templates": 1},
		"mx": {"status": "success", "templates": [{"key": key, "status": "success", "last_update_at": "2026-11-24"}]}}
	progress = expense_sync.template_progress(sources)
	assert len(progress) == 1 and progress[0]["status"] == "failed"
	assert progress[0]["last_update_at"] is None
	assert expense_sync.overall(sources, progress)["completed"] == 0
	assert not {"corp_id", "process_code", "field_mapping"}.intersection(progress[0])


@pytest.mark.parametrize("status", ["pending", "queued", "running", "staged", "failed"])
def test_partial_page_timestamps_never_claim_a_complete_update(status):
	rows = [{"status": status, "last_update_at": "2026-11-24 10:05:00"},
		{"status": "success", "last_update_at": "2026-11-24 10:00:00"}]
	result = expense_sync.overall({"mx": {"status": status, "active": status in ("queued", "running")}}, rows)
	assert result["last_update_at"] is None


def test_complete_update_requires_all_templates_and_finished_publication():
	rows = [{"status": "success", "last_update_at": stamp} for stamp in ("2026-11-24 10:05:00", "2026-11-24 10:00:00")]
	sources = {"mx": {"status": "success", "allocation_rebuilding": True}}
	assert expense_sync.overall(sources, rows)["last_update_at"] is None
	sources["mx"]["allocation_rebuilding"] = False
	assert expense_sync.overall(sources, rows)["last_update_at"] == "2026-11-24 10:00:00"
	rows[0]["last_update_at"] = None
	assert expense_sync.overall(sources, rows)["last_update_at"] is None


def test_mapping_candidates_require_combined_permissions_before_local_reads(monkeypatch):
	monkeypatch.setattr(s, "read", lambda: (_ for _ in ()).throw(frappe.PermissionError()))
	monkeypatch.setattr(s.cn, "get_unmapped_sources", lambda *a: pytest.fail("unauthorized read"))
	with pytest.raises(frappe.PermissionError): s.mapping_candidates("organizations")


def test_candidate_pages_keep_exact_source_identity_and_existing_scope(runtime, monkeypatch):
	name = "Source " * 40
	monkeypatch.setattr(s.cn, "get_unmapped_sources", lambda start: {"rows": [{"corp_id": "corp", "source_name": name, "count": 2}], "total": 101})
	result = s.mapping_candidates("organizations", "50")
	assert result["rows"][0]["source_name"] == name and result["has_more"] and result["start"] == 50
	monkeypatch.setattr(s.mx, "get_unmapped_entities", lambda start: {"rows": [{"entity_kind": "department", "source_entity": name, "line_count": 3, "suggestions": ["MX"]}], "has_more": False})
	result = s.mapping_candidates("entities")
	assert result["rows"] == [{"match_kind": "department", "source_name": name, "count": 3, "suggestions": ["MX"]}]


def test_unified_category_candidates_include_all_report_sources_and_deduplicate(runtime, monkeypatch):
	from budget.services import expense_management
	rows = [{"kind": "operating", "source_path": ["A", "B"], "source_key": key, "management_category": ""} for key in ("CN", "MX", "CN")]
	rows += [{"kind": "operating", "source_path": ["already mapped"], "source_key": "other", "management_category": "salary"}]
	rows += [{"kind": "budget", "source_path": ["hr", str(i)], "source_key": str(i), "management_category": ""} for i in range(51)]
	monkeypatch.setattr(expense_management, "load", lambda: (rows, {}, {}, {}))
	first = s.mapping_candidates("categories")
	last = s.mapping_candidates("categories", 50)
	assert len(first["rows"]) == 50 and first["has_more"] and not last["has_more"]
	assert last["rows"][-1] == {"kind": "operating", "source_path": ["A", "B"], "count": 2}


@pytest.mark.parametrize("kind,start", [("unknown", 0), ([], 0), ("entities", -1), ("categories", 50001), ("organizations", "invalid")])
def test_candidate_pagination_rejects_invalid_inputs(runtime, kind, start):
	with pytest.raises(frappe.ValidationError): s.mapping_candidates(kind, start)


@pytest.mark.parametrize("kind,processors", [("budget", {"cn"}), ("expense", {"mx"}),
	("operating", {"cn", "mx"}), ("purchase", {"cn", "mx"})])
def test_approval_mode_selects_adapters_without_rule_or_finance_fields(runtime, kind, processors):
	cn, mx = runtime.cn, runtime.mx
	rows = values(cn, mx)["templates"]
	rows.append({"approval_type": kind, "template_name": "New", "process_code": " PROC-new ",
		"processing_rule": "forged", "finance_activity_ids": "forged", "sync_mode": "disabled"})
	result = s.split_templates(rows, cn, mx, corporations=["corp"],
		template_defaults={"corp_id": "corp"}, processing_mode="approval")
	assert {source for source, items in result.items() if len(items) == 2} == processors
	for source in processors:
		assert result[source][-1]["corp_id"] == "corp"
		assert result[source][-1]["process_code"] == "PROC-new"
		assert not result[source][-1].get("finance_activity_ids")


@pytest.mark.parametrize("binding", ["application", "expense", "both"])
def test_approval_mode_keeps_existing_adapters_names_mappings_and_finance_evidence(runtime, binding):
	cn, mx = runtime.cn, runtime.mx
	if binding == "application": mx.templates.clear()
	if binding == "expense": cn["templates"].clear()
	row = values(cn, mx)["templates"][0]
	row.update(processing_rule="forged", finance_node_names="forged", finance_activity_ids="forged")
	result = s.split_templates([row], cn, mx, processing_mode="approval", template_defaults={"corp_id": "other"})
	assert {key for key, entries in result.items() if entries} == set(s.RULES[binding])
	if mx.templates:
		assert result["mx"][0]["finance_node_names"] == "Finance"
		assert result["mx"][0]["finance_activity_ids"] == "node"
		assert result["mx"][0]["field_mapping"] == mx.templates[0].field_mapping
		assert result["mx"][0]["template_name"] == "Expense"
	assert all(item["corp_id"] == "corp" for items in result.values() for item in items)


@pytest.mark.parametrize("field,value", [("corp_id", "other"), ("process_code", "PROC-moved"),
	("approval_type", "purchase"), ("key", "forged")])
def test_approval_mode_cannot_retarget_history_in_save_or_preview(runtime, field, value):
	rows = values(runtime.cn, runtime.mx)["templates"]
	rows[0][field] = value
	with pytest.raises(frappe.ValidationError):
		s.split_templates(rows, runtime.cn, runtime.mx, corporations=["corp"], processing_mode="approval")
	with pytest.raises(frappe.ValidationError):
		s.preview_template(rows[0], "America/Mexico_City", processing_mode="approval")


def test_approval_mode_is_atomic_and_does_not_keep_obsolete_common_rule(runtime, monkeypatch):
	monkeypatch.setattr(s, "read_template_defaults", lambda **kw: {})
	monkeypatch.setattr(s, "save_template_defaults", lambda value: runtime.staged.append(("defaults", value)))
	data = values(runtime.cn, runtime.mx)
	data.update(processing_mode="approval", template_defaults={"corp_id": "corp", "processing_rule": "obsolete"},
		template_defaults_version=s.mx.digest({}))
	s.save_settings(data)
	assert runtime.staged[0][1]["management"] is True
	assert runtime.staged[-1] == ("defaults", {"corp_id": "corp"})
	monkeypatch.setattr(s.cn, "save_once", lambda **kw: (_ for _ in ()).throw(frappe.ValidationError()))
	with pytest.raises(frappe.ValidationError): s.save_settings(data)
	assert runtime.staged == []


@pytest.mark.parametrize("field", ["corp_id", "process_code", "approval_type"])
def test_approval_mode_rejects_non_scalar_identity_before_lookup(runtime, field):
	rows = values(runtime.cn, runtime.mx)["templates"]
	rows[0][field] = ["invalid"]
	with pytest.raises(frappe.ValidationError):
		s.split_templates(rows, runtime.cn, runtime.mx, processing_mode="approval")
