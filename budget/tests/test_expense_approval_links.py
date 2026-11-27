"""Original links require live whole-source access, not just a visible row."""
from types import SimpleNamespace
from unittest.mock import Mock

import frappe
import pytest

from budget.services import expense_approval_links as links

SOURCE = "a" * 64
KEY = "b" * 64
URL = "dingtalk://test-instance"


@pytest.fixture
def runtime(monkeypatch):
	monkeypatch.setattr(frappe, "flags", frappe._dict())
	monkeypatch.setattr(links.mx, "_dingtalk_url", lambda source: URL)
	monkeypatch.setattr(links.regional, "require_connection", lambda record: None)
	return {"companies": {"CN", "MX"}, "manager": True}


def shared_source(monkeypatch, context, *, origin="mx", entries=None):
	entries = entries or [{"region": "mx", "company": "MX"}]
	record = frappe._dict(name=SOURCE, origin=origin, allocations_json=entries,
		state="confirmed", modified="2026-11-26 10:00:00.000001")
	lines = [frappe._dict(name=str(index), company=entry["company"] if entry["region"] == "mx" else "",
		regional_company=entry["company"] or entry.get("matched_company", ""), is_active=1,
		company_basis="mapping", mapping_version="v1") for index, entry in enumerate(entries)]
	def memberships(doctype, *, filters, fields):
		assert doctype == links.mx.LINE and filters == {"source": SOURCE}
		assert "regional_company" in fields
		return lines
	monkeypatch.setattr(frappe, "get_doc", lambda *args: record)
	monkeypatch.setattr(frappe, "get_all", memberships)
	monkeypatch.setattr(frappe, "db", SimpleNamespace(exists=lambda *args: True))
	monkeypatch.setattr(links.regional, "membership_fields", lambda: ["regional_company"])
	monkeypatch.setattr(links.regional, "allowed_companies", lambda: context["companies"])
	monkeypatch.setattr(links.mx, "_manager", lambda: context["manager"])
	monkeypatch.setattr(links.mx, "_finance", lambda: None)
	monkeypatch.setattr(links.mx, "_settings", lambda: frappe._dict())
	monkeypatch.setattr(links.mx, "_mapping_version", lambda settings: "v1")
	row = {"key": links.regional.digest([SOURCE, "regional-" + entries[0]["region"], 0]),
		"source_key": SOURCE, "kind": "operating", "company": entries[0]["company"],
		"allocation_modified": str(record.modified)}
	return row, record, lines


@pytest.mark.parametrize("origin", ["cn", "mx"])
def test_group_reporting_keeps_original_empty_company_identity_for_source_authorization(monkeypatch, runtime, origin):
	row, _, _ = shared_source(monkeypatch, runtime, origin=origin, entries=[{"region": "pending", "company": ""}])
	assert links.original_url({**row, "is_group_expense": 1}, runtime, {}) == URL
	assert links.original_url({**row, "company": "__expense_group__", "is_group_expense": 1}, runtime, {}) == ""


@pytest.mark.parametrize("origin", ["cn", "mx"])
@pytest.mark.parametrize("owner_field", ["company", "regional_company"])
def test_shared_source_blocks_forbidden_retired_memberships(monkeypatch, runtime, origin, owner_field):
	row, _record, lines = shared_source(monkeypatch, runtime, origin=origin)
	retired = frappe._dict(name="retired", company="MX", regional_company="MX", is_active=0,
		company_basis="mapping", mapping_version="old")
	retired[owner_field] = "Forbidden"
	lines.append(retired)
	assert links.original_url(row, runtime, {}) == ""
	runtime["companies"].add("Forbidden")
	assert links.original_url(row, runtime, {}) == URL


def test_mx_finance_reader_with_whole_source_access_gets_link(monkeypatch, runtime):
	runtime["manager"] = False
	row, _record, lines = shared_source(monkeypatch, runtime)
	monkeypatch.setattr(links.mx, "_companies", lambda: ["MX"])
	assert links.mx._whole_source_allowed(SOURCE)
	assert links.original_url(row, runtime, {}) == URL
	lines[0].mapping_version = "old"
	assert links.original_url(row, runtime, {}) == ""
	lines[0].mapping_version = "v1"
	def no_field_access():
		assert frappe.flags.mute_messages is True
		raise frappe.PermissionError()
	monkeypatch.setattr(links.mx, "_finance", no_field_access)
	assert links.original_url(row, runtime, {}) == ""
	assert frappe.flags.mute_messages is None


@pytest.mark.parametrize("origin", ["cn", "mx"])
def test_shared_current_owners_and_native_evidence_are_both_required(monkeypatch, runtime, origin):
	row, record, lines = shared_source(monkeypatch, runtime, origin=origin)
	assert links.original_url(row, runtime, {}) == URL
	# Even incomplete native memberships cannot grant access to another current owner.
	record.allocations_json.append({"region": "cn", "company": "Forbidden"})
	assert links.original_url(row, runtime, {}) == ""
	record.allocations_json[-1] = {"region": "pending", "company": "", "matched_company": "Forbidden"}
	assert links.original_url(row, runtime, {}) == ""
	record.allocations_json.pop()
	lines.clear()
	assert links.original_url(row, runtime, {}) == ""


@pytest.mark.parametrize("origin", ["cn", "mx"])
def test_shared_link_rechecks_company_permissions_on_each_read(monkeypatch, runtime, origin):
	row, _record, _lines = shared_source(monkeypatch, runtime, origin=origin)
	assert links.original_url(row, runtime, {}) == URL
	runtime["companies"].remove("MX")
	assert links.original_url(row, runtime, {}) == ""


def test_source_permission_probe_restores_message_state_and_does_not_hide_system_errors(monkeypatch, runtime):
	row, _record, _lines = shared_source(monkeypatch, runtime)
	frappe.flags.mute_messages = True
	assert links.original_url(row, runtime, {}) == URL
	assert frappe.flags.mute_messages is True
	frappe.flags.mute_messages = False
	def unexpected():
		raise RuntimeError("permission service failed")
	monkeypatch.setattr(links.mx, "_finance", unexpected)
	with pytest.raises(RuntimeError, match="permission service failed"):
		links.original_url(row, runtime, {})
	assert frappe.flags.mute_messages is False


@pytest.mark.parametrize("origin,region,company", [("mx", "cn", "CN"), ("cn", "mx", "MX")])
def test_shared_approval_from_either_feed_uses_current_whole_document_guard(monkeypatch, runtime, origin, region, company):
	entries = [{"region": region, "company": company}, {"region": "mx", "company": "MX"}]
	row, _record, _lines = shared_source(monkeypatch, runtime, origin=origin, entries=entries)
	assert links.original_url(row, runtime, {}) == URL
	runtime["companies"] = {"CN"}
	assert links.original_url(row, runtime, {}) == ""
	runtime.update(companies={"CN", "MX"}, manager=False)
	assert links.original_url(row, runtime, {}) == ""
	runtime["manager"] = True
	assert links.original_url({**row, "company": "Other"}, runtime, {}) == ""
	assert links.original_url({**row, "key": KEY}, runtime, {}) == ""
	def disconnected(record):
		raise frappe.ValidationError("connection changed")
	monkeypatch.setattr(links.regional, "require_connection", disconnected)
	with pytest.raises(frappe.ValidationError, match="connection changed"):
		links.original_url(row, runtime, {})


def test_legacy_requires_current_line_field_permissions_and_all_historic_companies(monkeypatch, runtime):
	row = {"key": KEY, "source_key": SOURCE, "kind": "operating", "company": "CN"}
	monkeypatch.setattr(frappe, "db", SimpleNamespace(exists=lambda doctype, *_: doctype == links.mx.LINE))
	monkeypatch.setattr(frappe, "get_doc", lambda *args: frappe._dict(name=SOURCE))
	monkeypatch.setattr(links.mx, "_finance", lambda: None)
	monkeypatch.setattr(links.mx, "_get_management_lines", lambda **kwargs: [frappe._dict(company="CN")])
	monkeypatch.setattr(links.mx, "_settings", lambda: frappe._dict(mapping_version="v1"))
	monkeypatch.setattr(links.mx, "_manager", lambda: True)
	monkeypatch.setattr(links.regional, "membership_fields", lambda: [])
	docs = [frappe._dict(company="CN", is_active=1, company_basis="manual"),
		frappe._dict(company="MX", is_active=0, company_basis="mapping", mapping_version="old")]
	monkeypatch.setattr(frappe, "get_all", lambda *args, **kwargs: docs)
	assert links.original_url(row, runtime, {}) == URL
	assert links.original_url(row, {**runtime, "companies": {"CN"}}, {}) == ""
	assert links.original_url(row, {**runtime, "companies": set()}, {}) == ""
	docs[0].update(company_basis="mapping", mapping_version="old")
	assert links.original_url(row, runtime, {}) == ""
	docs[0].company_basis = "manual"
	monkeypatch.setattr(links.mx, "_get_management_lines", lambda **kwargs: [])
	assert links.original_url(row, runtime, {}) == ""
	def no_field_access():
		assert frappe.flags.mute_messages is True
		raise frappe.PermissionError()
	monkeypatch.setattr(links.mx, "_finance", no_field_access)
	assert links.original_url(row, runtime, {}) == ""
	assert frappe.flags.mute_messages is None


def test_native_snapshot_link_requires_every_company_and_current_selected_line(monkeypatch, runtime):
	row = {"key": KEY, "source_key": SOURCE, "kind": "budget", "company": "CN"}
	monkeypatch.setattr(links, "read_snapshot", lambda: {"rows": [{"key": SOURCE}]})
	context = {**runtime, "known_companies": {"CN", "MX"}}
	monkeypatch.setattr(links.approvals, "access_context", lambda: context)
	def lines(sources, scope, rules):
		return [item for item in [{"key": KEY, "company": "CN"}, {"key": "other", "company": "MX"}]
			if item["company"] in scope["companies"]]
	monkeypatch.setattr(links.management, "analytical_lines", lines)
	assert links.original_url(row, context, {}) == URL
	context["companies"] = {"CN"}
	assert links.original_url(row, context, {}) == ""
	assert links.original_url({**row, "key": "c" * 64}, context, {}) == ""
	monkeypatch.setattr(links, "read_snapshot", lambda: {"rows": []})
	assert links.original_url(row, context, {}) == ""


def test_fallback_detail_adds_link_only_after_row_scope_check(monkeypatch):
	from budget.services import expense_management as service, expense_report_store as store
	row = {"key": KEY, "source_key": SOURCE, "company": "CN", "month": "2026-11"}
	monkeypatch.setattr(store, "installed", lambda: False)
	monkeypatch.setattr(service, "prepare", lambda values: ([row], {}, {}, {"company_regions": {}},
		{"month": "2026-11", "view": "overview"}))
	monkeypatch.setattr(service, "converter", lambda: lambda rows: rows)
	resolver = Mock(return_value={"approval_url": URL, "original_expense": None})
	monkeypatch.setattr(links, "original_context", resolver)
	result = service.detail(KEY)
	assert result["approval_url"] == URL
	assert "source_key" not in result["row"] and "approval_url" not in result["row"]
	resolver.reset_mock()
	monkeypatch.setattr(service, "_", lambda value: value)
	monkeypatch.setattr(frappe, "throw", lambda *args: (_ for _ in ()).throw(frappe.PermissionError()))
	with pytest.raises(frappe.PermissionError):
		service.detail("outside")
	resolver.assert_not_called()


def test_sql_detail_adds_link_only_after_authorized_selection(monkeypatch):
	from budget.services import expense_reporting as reporting
	context, rules = {"companies": {"CN"}}, {}
	monkeypatch.setattr(reporting, "prepare", lambda values: (
		{"view": "details", "measure": "", "month": "2026-11"}, context, rules,
		{"company_regions": {}}, "company in %(companies)s", {"companies": ["CN"]}))
	row = {"key": KEY, "source_key": SOURCE, "company": "CN"}
	selection = Mock(return_value=[row])
	monkeypatch.setattr(reporting.store, "select", selection)
	resolver = Mock(return_value={"approval_url": URL, "original_expense": None})
	monkeypatch.setattr(links, "original_context", resolver)
	result = reporting.detail(KEY)
	assert result["approval_url"] == URL and "source_key" not in result["row"]
	resolver.assert_called_once_with(row, context, rules)
	assert "company in" in selection.call_args.args[0] and "name=%(key)s" in selection.call_args.args[0]
	selection.return_value = []
	resolver.reset_mock()
	monkeypatch.setattr(reporting, "_", lambda value: value)
	monkeypatch.setattr(frappe, "throw", lambda *args: (_ for _ in ()).throw(frappe.PermissionError()))
	with pytest.raises(frappe.PermissionError):
		reporting.detail(KEY)
	resolver.assert_not_called()


def test_allocated_detail_returns_only_its_original_line_after_whole_source_permission(monkeypatch, runtime):
	entries = [{"region": "mx", "company": "MX", "line_key": "original", "amount": "30.00"},
		{"region": "cn", "company": "CN", "line_key": "original", "amount": "70.00"}]
	row, record, _lines = shared_source(monkeypatch, runtime, entries=entries)
	row.update(amount="30.00", currency="CNY")
	record.facts_json = [{"currency": "CNY"}, [
		{"line_key": "original", "table_id": "detail-table", "source_row_id": "2", "amount": "100.00",
			"currency": "CNY", "description": "Shared service", "source_entity": "Group service department"},
		{"line_key": "another", "amount": "999.00", "description": "Other expense must not be returned"}]]
	result = links.original_context(row, runtime, {})
	assert result["approval_url"] == URL
	assert result["original_expense"] == {"origin": "detail", "table_label": "", "row_number": "2",
		"amount": "100.00", "currency": "CNY", "description": "Shared service",
		"source_entity": "Group service department", "split_count": 2}
	assert "another" not in str(result) and "999.00" not in str(result)
	assert row["amount"] == "30.00"  # Preview never replaces the allocated report amount.
	runtime["companies"].remove("CN")
	assert links.original_context(row, runtime, {}) == {"approval_url": "", "original_expense": None}


def test_original_preview_can_be_read_without_a_dingtalk_link_and_rejects_stale_amount(monkeypatch, runtime):
	row, record, _lines = shared_source(monkeypatch, runtime,
		entries=[{"region": "mx", "company": "MX", "line_key": "header", "amount": "40.00"}])
	record.facts_json = [{"currency": "MXN"}, [{"line_key": "header", "table_id": "header", "amount": "40.00", "currency": "MXN"}]]
	row.update(amount="40", currency="MXN")
	monkeypatch.setattr(links.mx, "_dingtalk_url", lambda source: "")
	assert links.original_context(row, runtime, {})["original_expense"]["amount"] == "40.00"
	row["amount"] = "30.00"
	assert links.original_context(row, runtime, {})["original_expense"] is None
	row["amount"] = "40.00"
	record.facts_json = "{broken"
	assert links.original_context(row, runtime, {})["original_expense"] is None


def test_old_split_cannot_preview_new_facts_or_an_unreviewed_allocation(monkeypatch, runtime):
	entries = [{"region": "mx", "company": "MX", "line_key": "original", "amount": "30.00"},
		{"region": "cn", "company": "CN", "line_key": "original", "amount": "70.00"}]
	row, record, _lines = shared_source(monkeypatch, runtime, entries=entries)
	original = {"line_key": "original", "table_id": "detail-table", "source_row_id": "2", "amount": "100.00",
		"currency": "CNY", "description": "Shared service", "source_entity": "Group service department"}
	record.facts_json = [{"currency": "CNY", "approval_status": "approved"}, [original]]
	row.update(amount="30.00", currency="CNY", status="approved", description="Shared service",
		line_label="Shared service", source_path=links.regional.contract.management_path(original))
	assert links.original_context(row, runtime, {})["original_expense"]["amount"] == "100.00"
	# An OA edit keeps the manual split until it is reviewed again. The old
	# publication remains readable, but its original amount must not become 200.
	original["amount"] = "200.00"
	record.modified = "2026-11-26 10:05:00.000002"
	record.state = "stale"
	assert links.original_context(row, runtime, {}) == {"approval_url": URL, "original_expense": None}
	# Even a freshly published review row cannot treat a stale plan as valid.
	row["allocation_modified"] = str(record.modified)
	assert links.original_context(row, runtime, {})["original_expense"] is None
	# After review, the new 30+170 split is valid, but the old report's version
	# still cannot be used as evidence, even with identical display fields.
	entries[1]["amount"] = "170.00"
	record.state = "confirmed"
	record.modified = "2026-11-26 10:06:00.000003"
	assert links.original_context(row, runtime, {})["original_expense"] is None
	row["allocation_modified"] = str(record.modified)
	assert links.original_context(row, runtime, {})["original_expense"]["amount"] == "200.00"
	entries[0]["source_available"] = False
	assert links.original_context(row, runtime, {})["original_expense"] is None
	entries[0].pop("source_available")
	# Publications built before provenance was stored fail closed until rebuilt.
	row.pop("allocation_modified")
	assert links.original_context(row, runtime, {}) == {"approval_url": URL, "original_expense": None}


def test_source_preview_is_absent_from_public_rows_and_stored_reporting_payloads():
	from budget.services import expense_management, expense_report_store
	from .test_expense_reporting import line, rate
	row = expense_management.converter(rate)([line("selected")])[0]
	row["original_expense"] = {"amount": "999", "description": "Private original"}
	row["approval_url"] = URL
	assert "original_expense" not in expense_management.public(row)
	assert "original_expense" not in expense_report_store.pack(row)["detail_json"]
	assert "Private original" not in str(expense_report_store.pack(row))


def test_current_automatic_unverified_source_amount_is_visible_but_not_eligible(monkeypatch, runtime):
	entries = [{"region": "mx", "company": "MX", "line_key": "header", "amount": "6400.00", "assignment_mode": "automatic"}]
	row, record, _lines = shared_source(monkeypatch, runtime, entries=entries)
	record.state = "stale"
	record.facts_json = [{"currency": "MXN"}, [{"line_key": "header", "table_id": "header", "amount": "6400.00",
		"currency": "MXN", "base_issues": ["total"]}]]
	record.source_json = {"form_component_values": [{"id": "DDSelectField_1FEAISH1BZ4W0", "componentType": "DDSelectField", "value": "墨西哥Mexico"}]}
	record.template_json = {}
	row.update(amount=None, currency="MXN", issues=["regional_allocation", "total"])
	result = links.original_context(row, runtime, {})["original_expense"]
	assert result["amount"] == "6400.00" and result["unverified"]
	assert result["execution_region"] == "墨西哥Mexico"
	assert row["amount"] is None and "total" in row["issues"]
	row["allocation_modified"] = "old"
	assert links.original_context(row, runtime, {})["original_expense"] is None
	row["allocation_modified"] = str(record.modified)
	entries[0]["source_available"] = False
	assert links.original_context(row, runtime, {})["original_expense"] is None
	entries[0].pop("source_available")
	entries[0].pop("assignment_mode")
	assert links.original_context(row, runtime, {})["original_expense"] is None
