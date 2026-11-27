"""Group is a versioned reporting rule, never a Company document."""
import json
from types import SimpleNamespace

import frappe
import pytest

from budget.services import expense_group_mappings as s


def rule(kind="organization", name="CEO office", corp="corp"):
	return {"match_kind": kind, "corp_id": corp, "source_name": name}


def test_group_rules_retain_source_domain_corporation_and_version():
	rows = [rule(), rule("department", "Sales  Team"), rule("entity", "Sales Team")]
	clean = s.clean(rows, ["corp"])
	lookup = s.lookup(clean, ["mx-corp"])
	assert s.matches(lookup, "corp", "department", "CEO office")
	assert not s.matches(lookup, "other", "department", "CEO office")
	assert not s.matches(lookup, "corp", None, "CEO  office")
	assert s.matches(lookup, "mx-corp", "department", "Sales   Team")
	assert not s.matches(lookup, "corp", "department", "Sales Team")
	assert s.version(clean) == s.version(list(reversed(clean)))
	assert s.version(clean) != s.version(clean[:-1])
	assert not s.matches(lookup, "corp", "entity", "")


@pytest.mark.parametrize("rows", [[rule(corp="forbidden")], [rule(name=" ")], [rule("unknown")], [rule(), rule()], [rule(name=[])], {}])
def test_invalid_or_unauthorized_group_rules_are_rejected(rows):
	with pytest.raises(ValueError):
		s.clean(rows, ["corp"])


def test_group_rule_size_is_bounded_before_writing_the_text_column():
	with pytest.raises(ValueError):
		s.clean([rule(name=f"source {index} " + "中" * 450) for index in range(45)], ["corp"])
	assert len(json.dumps(s.clean([rule()]), ensure_ascii=False).encode()) < s.MAX_BYTES


def test_storage_uses_site_defaults_and_does_not_query_or_create_company(monkeypatch):
	from frappe import defaults
	stored = {}
	def get_value(doctype, filters, field, **kwargs):
		assert doctype == "DefaultValue" and filters == {"parent": s.PARENT, "defkey": s.KEY}
		return stored.get("value")
	def set_default(key, value, parent):
		assert key == s.KEY and parent == s.PARENT
		stored["value"] = value
	monkeypatch.setattr(frappe, "db", SimpleNamespace(get_value=get_value))
	monkeypatch.setattr(defaults, "set_default", set_default)
	monkeypatch.setattr(s, "_", lambda text: text)
	monkeypatch.setattr(frappe, "throw", lambda text: (_ for _ in ()).throw(frappe.ValidationError(text)))
	assert s.read(lock=True) == []
	s.save([rule()])
	assert s.read() == [rule()] and s.TARGET not in stored["value"]
	stored["value"] = json.dumps({"unrecognized": True})
	with pytest.raises(frappe.ValidationError): s.read()


def test_native_conflicts_require_the_scoped_version_checked_command(monkeypatch):
	rows = [rule()]
	monkeypatch.setattr(s, "read", lambda **kwargs: rows)
	monkeypatch.setattr(s, "_", lambda text: text)
	monkeypatch.setattr(frappe, "throw", lambda text: (_ for _ in ()).throw(frappe.ValidationError(text)))
	with pytest.raises(frappe.ValidationError, match="assigned to Group"):
		s.require_no_conflicts(rows)
	s.require_no_conflicts([rule(corp="other")])
	s.require_no_conflicts([rule("department")])
	with pytest.raises(RuntimeError):
		with s.command([]):
			s.require_no_conflicts(rows)
			raise RuntimeError("rollback the native command")
	with pytest.raises(frappe.ValidationError, match="assigned to Group"):
		s.require_no_conflicts(rows)


def test_shadow_company_mapping_has_one_editable_group_row_without_mutating_storage():
	company = {**rule(), "company": "A"}
	other = {**rule(name="Other"), "company": "B"}
	rows = [company, other]
	result = s.merge(rows, [rule()])
	assert len(result) == 2 and result[-1]["company"] == s.TARGET
	assert rows == [company, other] and company["company"] == "A"
