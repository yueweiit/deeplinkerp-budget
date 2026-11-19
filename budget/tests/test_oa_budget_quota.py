from contextlib import contextmanager
from copy import deepcopy
from decimal import Decimal
from types import SimpleNamespace

import frappe
import pytest

from budget.integrations import shared_operating_oa
from budget.services import oa_budget_quota as projection
from budget.services import oa_budget_sync as ledger
from budget.services import quota_sync as quotas
from budget.services.oa_budget_parser import BUDGET_PROCESS_CODE


SOURCE = {"profile": "operating_oa", "corp_id": "corp-A", "enabled": True, "version": "version-A",
	"connection": {"url": "https://oa.test"}}
PATHS = {"dept-A": {"ids": ["root", "group", "company", "dept-A"],
	"names": ["Root", "Group", "Company A", "Department A"]}}


def approval(identity="first", *, amount="10.30", month="2026-11", dept="dept-A",
	created="2026-11-01T00:00:00Z", status="COMPLETED", result="agree", region="CN", **changes):
	return {"corp_id": "corp-A", "process_code": BUDGET_PROCESS_CODE,
		"process_instance_id": identity, "raw_payload": {"businessId": identity},
		"create_time": created, "updated_at": created, "originator_dept_id": dept,
		"originator_dept_name": "Original Department", "status": status, "result": result,
		"form_component_values": [
			{"name": "执行地区", "value": region},
			{"name": "预算类型", "value": "非生产预算"},
			{"name": "预算月份", "value": month},
			{"name": "预算总金额", "value": amount},
			{"name": "预算项目", "value": [{"rowValue": [
				{"label": "预算明细", "value": "Travel"}, {"label": "人民币金额", "value": "0.10"}]},
				{"rowValue": [{"label": "预算明细", "value": "Travel"},
					{"label": "人民币金额", "value": "0.20"}]},
				{"rowValue": [{"label": "预算明细", "value": "Office"},
					{"label": "人民币金额", "value": "10"}]}]},
		], **changes}


def test_approved_budget_projects_header_and_aggregated_subjects_with_decimal_amounts():
	rows, subjects = projection.project([approval()], PATHS, SOURCE)
	row = rows[0]
	assert row["total_amount"] == row["detail_amount"] == Decimal("10.30")
	assert not row["detail_amount_mismatch"]
	assert row["company_name"] == "Company A"
	assert row["company_dept_id"] == "company"
	assert row["department_name"] == "Department A"
	assert row["status"] == "effective"
	assert [(s["subject_name"], s["amount"]) for s in subjects] == [
		("Travel", Decimal("0.30")), ("Office", Decimal("10.00"))]


def test_latest_department_month_supersedes_old_quota_but_keeps_history_and_other_scopes():
	rows, _ = projection.project([approval(), approval("new", created="2026-11-02T00:00:00Z"),
		approval("other-dept", dept="dept-B"), approval("other-month", month="2026-12")], PATHS, SOURCE)
	assert {r["source_form_no"]: r["status"] for r in rows} == {
		"first": "deactivated", "new": "effective", "other-dept": "effective", "other-month": "effective"}


@pytest.mark.parametrize("changes", [{"status": "RUNNING"}, {"result": "refuse"},
	{"deleted_at": "2026-11-01"}, {"status": "CANCELED"}, {"region": "Mexico"}])
def test_noneligible_approvals_cannot_grant_quota(changes):
	assert projection.project([approval(**changes)], PATHS, SOURCE) == ([], [])


def test_terminated_agree_remains_eligible_and_zero_header_is_not_replaced_by_detail_amount():
	rows, _ = projection.project([approval(status="TERMINATED", amount="0")], {}, SOURCE)
	assert rows[0]["status"] == "effective"
	assert rows[0]["approval_status"] == "TERMINATED"
	assert rows[0]["total_amount"] == Decimal(0)
	assert rows[0]["detail_amount_mismatch"]
	assert rows[0]["company_name"] == "待确认"
	assert rows[0]["department_name"] == "Original Department"
	assert not rows[0]["organization_is_current"]


def test_company_review_reuses_exact_mapping_without_inventing_a_company_id():
	paths = {"dept-A": {"reviewed_company": "Reviewed Company", "reviewed_department": "Reviewed Dept",
		"reviewed_organization_level": "department"}}
	rows, _ = projection.project([approval()], paths, SOURCE)
	assert rows[0]["company_name"] == "Reviewed Company"
	assert rows[0]["department_name"] == "Reviewed Dept"
	assert rows[0]["company_dept_id"] == ""
	paths["dept-A"].update(reviewed_department="", reviewed_organization_level="company")
	rows, _ = projection.project([approval()], paths, SOURCE)
	assert rows[0]["company_dept_id"] == "dept-A"
	assert rows[0]["department_name"] == ""


@pytest.mark.parametrize("changes", [{"corp_id": "corp-B"}, {"process_code": "other"},
	{"month": "2026-13"}, {"month": ""}, {"created": "not-a-time"}])
def test_invalid_source_identity_or_period_fails_before_projection(changes):
	with pytest.raises(ValueError):
		projection.project([approval(**changes)], PATHS, SOURCE)


def test_duplicate_form_number_is_rejected_instead_of_overwriting_a_quota():
	with pytest.raises(ValueError, match="Duplicate"):
		projection.project([approval(), approval("second", raw_payload={"businessId": "first"})], PATHS, SOURCE)


@pytest.fixture
def memory_db(monkeypatch):
	data = {quotas.QUOTA_DOCTYPE: {}, quotas.SUBJECT_DOCTYPE: {}}
	def matches(doc, filters):
		for key, expected in (filters or {}).items():
			if isinstance(expected, list):
				if doc.get(key) not in expected[1]:
					return False
			elif doc.get(key) != expected:
				return False
		return True
	def get_all(doctype, filters=None, fields=None, **kwargs):
		if kwargs.get("pluck"):
			return [doc[kwargs["pluck"]] for doc in data[doctype].values() if matches(doc, filters)]
		return [frappe._dict({key: doc.get(key) for key in fields})
			for doc in data[doctype].values() if matches(doc, filters)]
	def get_doc(doc):
		def insert(**kwargs):
			name = doc.get("form_no") or f"subject-{len(data[doc['doctype']]) + 1}"
			data[doc["doctype"]][name] = {**doc, "name": name}
		return SimpleNamespace(insert=insert)
	def delete(doctype, filters):
		for name, doc in list(data[doctype].items()):
			if matches(doc, filters):
				del data[doctype][name]
	monkeypatch.setattr(quotas.frappe, "get_all", get_all)
	monkeypatch.setattr(quotas.frappe, "get_doc", get_doc)
	monkeypatch.setattr(quotas.frappe, "db", SimpleNamespace(
		set_value=lambda dt, name, values, **kw: data[dt][name].update(values), delete=delete))
	monkeypatch.setattr(quotas, "_datetime_value", lambda value: value)
	monkeypatch.setattr(quotas.frappe.utils, "now", lambda: "2026-11-19 10:00:00")
	monkeypatch.setattr(quotas, "_", lambda text: text)
	monkeypatch.setattr(quotas.frappe, "throw", lambda text: (_ for _ in ()).throw(ValueError(text)))
	monkeypatch.setattr(ledger, "_check_deadline", lambda: None)
	return data


def test_repeated_sync_is_idempotent_and_rejected_approval_deactivates_without_erasing_history(memory_db):
	first = quotas.apply_approval_snapshot([approval()], PATHS, SOURCE)
	second = quotas.apply_approval_snapshot([approval()], PATHS, SOURCE)
	assert first["created"] == second["updated"] == 1
	assert len(memory_db[quotas.QUOTA_DOCTYPE]) == 1
	assert len(memory_db[quotas.SUBJECT_DOCTYPE]) == 2
	before_subjects = deepcopy(memory_db[quotas.SUBJECT_DOCTYPE])
	result = quotas.apply_approval_snapshot([approval(result="refuse")], PATHS, SOURCE)
	assert result["deactivated"] == 1
	assert memory_db[quotas.QUOTA_DOCTYPE]["first"]["status"] == "deactivated"
	assert memory_db[quotas.SUBJECT_DOCTYPE] == before_subjects


def test_other_corporation_is_preserved_and_same_form_collision_is_rejected(memory_db):
	quotas.apply_approval_snapshot([approval()], PATHS, SOURCE)
	memory_db[quotas.QUOTA_DOCTYPE]["first"]["corp_id"] = "corp-B"
	before = deepcopy(memory_db)
	quotas.apply_approval_snapshot([approval(result="refuse")], PATHS, SOURCE)
	assert memory_db == before
	with pytest.raises(ValueError, match="identity conflicts"):
		quotas.apply_approval_snapshot([approval()], PATHS, SOURCE)
	assert memory_db == before


def test_partial_subject_write_is_detected(memory_db, monkeypatch):
	original = quotas.frappe.get_doc
	monkeypatch.setattr(quotas.frappe, "get_doc", lambda doc: (
		SimpleNamespace(insert=lambda **kw: None) if doc["doctype"] == quotas.SUBJECT_DOCTYPE else original(doc)))
	with pytest.raises(quotas.QuotaSyncVerificationError):
		quotas.apply_approval_snapshot([approval()], PATHS, SOURCE)


def test_one_complete_http_snapshot_supplies_both_projections_without_database_fetch(monkeypatch):
	rows = [approval(), approval("terminated", status="TERMINATED")]
	calls = []
	monkeypatch.setattr(ledger.sync_settings, "current_source", lambda: SOURCE)
	monkeypatch.setattr(shared_operating_oa, "instances", lambda *a, **kw: calls.append(kw) or rows)
	monkeypatch.setattr(ledger, "_department_paths", lambda: PATHS)
	monkeypatch.setattr(ledger, "_fetch_instances", lambda: pytest.fail("must not read the old source"))
	monkeypatch.setattr(ledger, "_sync_ledger_rows", lambda selected, paths, **kw:
		calls.append([r["process_instance_id"] for r in selected]) or {"verified": True})
	monkeypatch.setattr(quotas, "apply_approval_snapshot", lambda selected, paths, src:
		calls.append([r["process_instance_id"] for r in selected]) or {"verified": True})
	assert ledger._sync_from_oa()["quota"]["verified"]
	assert calls == [{"approved_only": False}, ["first"], ["first", "terminated"]]


def test_empty_snapshot_preserves_existing_data(monkeypatch):
	monkeypatch.setattr(ledger.sync_settings, "current_source", lambda: SOURCE)
	monkeypatch.setattr(shared_operating_oa, "instances", lambda *a, **kw: [])
	monkeypatch.setattr(quotas, "apply_approval_snapshot", lambda *a: pytest.fail("must preserve quotas"))
	assert ledger._sync_from_oa()["reason_code"] == "no_source_records"


@pytest.mark.parametrize("changes", [{"result": "refuse"}, {"deleted_at": "2026-11-19"}, {"status": "CANCELED"}])
def test_complete_ineligible_snapshot_removes_derived_ledger_and_deactivates_quota(memory_db, monkeypatch, changes):
	quotas.apply_approval_snapshot([approval()], PATHS, SOURCE)
	memory_db[ledger.ENTRY_DOCTYPE] = {"first": {"name": "first", "form_no": "first", "total_amount": 10.30}}
	memory_db[ledger.DETAIL_DOCTYPE] = {"detail": {"name": "detail", "form_no": "first"}}
	monkeypatch.setattr(frappe, "delete_doc", lambda dt, name, **kw: memory_db[dt].pop(name))
	monkeypatch.setattr(ledger, "_local_snapshot", lambda: {"entry_count": len(memory_db[ledger.ENTRY_DOCTYPE]),
		"total_amount": sum(row["total_amount"] for row in memory_db[ledger.ENTRY_DOCTYPE].values()),
		"detail_count": len(memory_db[ledger.DETAIL_DOCTYPE])})
	monkeypatch.setattr(ledger.sync_settings, "current_source", lambda: SOURCE)
	monkeypatch.setattr(ledger, "_department_paths", lambda: PATHS)
	monkeypatch.setattr(shared_operating_oa, "instances", lambda *a, **kw: [approval(**changes)])
	result = ledger._sync_from_oa()
	assert result["verified"] and result["quota"]["verified"]
	assert result["removed"] == 1 and result["local_total"] == result["local_amount"] == 0
	assert not memory_db[ledger.ENTRY_DOCTYPE] and not memory_db[ledger.DETAIL_DOCTYPE]
	assert memory_db[quotas.QUOTA_DOCTYPE]["first"]["status"] == "deactivated"
	assert len(memory_db[quotas.SUBJECT_DOCTYPE]) == 2


def test_empty_legacy_ledger_snapshot_is_still_skipped(monkeypatch):
	monkeypatch.setattr(ledger, "_check_deadline", lambda: None)
	monkeypatch.setattr(frappe, "get_all", lambda *a, **kw: pytest.fail("must not clear a legacy empty snapshot"))
	assert ledger._sync_ledger_rows([], {})["skipped"]


def test_quota_failure_rolls_back_ledger_and_does_not_commit_success(monkeypatch):
	events = []
	@contextmanager
	def time_budget():
		yield
	monkeypatch.setattr(ledger, "_time_budget", time_budget)
	monkeypatch.setattr(ledger, "_log_start", lambda *a, **kw: "log")
	monkeypatch.setattr(ledger, "_prune_sync_logs", lambda: None)
	monkeypatch.setattr(ledger, "_log_finish", lambda name, status, **kw: events.append(status))
	monkeypatch.setattr(ledger, "_remember", lambda *a: None)
	monkeypatch.setattr(ledger.frappe.utils, "now", lambda: "2026-11-19 10:00:00")
	monkeypatch.setattr(ledger.frappe, "db", SimpleNamespace(
		rollback=lambda: events.append("rollback"), commit=lambda: pytest.fail("must not commit success")))
	monkeypatch.setattr(ledger.frappe, "log_error", lambda **kw: None)
	monkeypatch.setattr(ledger.frappe, "get_traceback", lambda: "test failure")
	def fail(**kwargs):
		events.append("ledger-write")
		raise quotas.QuotaSyncVerificationError("quota mismatch")
	monkeypatch.setattr(ledger, "_sync_from_oa", fail)
	with pytest.raises(quotas.QuotaSyncVerificationError):
		ledger._run_sync(trigger="Manual", force=False)
	assert events == ["ledger-write", "rollback", "Failed"]


def test_legacy_quota_entry_uses_shared_sync_and_cannot_fall_back_to_pg(monkeypatch):
	monkeypatch.setattr(quotas.settings, "current_source", lambda: {"profile": "budget"})
	monkeypatch.setattr(ledger, "sync_from_oa", lambda **kw: pytest.fail("must not open a legacy connection"))
	assert quotas.sync_quotas()["reason_code"] == "shared_oa_required"
	monkeypatch.setattr(quotas.settings, "current_source", lambda: SOURCE)
	monkeypatch.setattr(ledger, "sync_from_oa", lambda **kw: kw)
	assert quotas.sync_quotas() == {"trigger": "Scheduled", "expected_config_version": "version-A"}
