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
	record = frappe._dict(name=SOURCE, origin=origin, allocations_json=entries)
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
		"source_key": SOURCE, "kind": "operating", "company": entries[0]["company"]}
	return row, record, lines


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
	resolver = Mock(return_value=URL)
	monkeypatch.setattr(links, "original_url", resolver)
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
	resolver = Mock(return_value=URL)
	monkeypatch.setattr(links, "original_url", resolver)
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
