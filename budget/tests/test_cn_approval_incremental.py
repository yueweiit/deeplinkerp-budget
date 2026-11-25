"""Full -> delta -> reconciliation, using actual approval projections."""
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import Mock

import frappe
import pytest

from budget.integrations import cn_approval_source as source
from budget.tests.test_cn_approvals import sample
from deeplinkerp_branding.services import oa_sync_policy as policy

START = datetime(2026, 11, 25, 1, tzinfo=timezone.utc)


@pytest.fixture
def scan(monkeypatch):
	monkeypatch.setattr(frappe.utils, "now", lambda: "2026-11-25 09:00:00")
	monkeypatch.setattr(frappe, "throw", lambda text: (_ for _ in ()).throw(ValueError(text)))
	def run(rows, *, previous=None, now=START, templates=None, pages=None):
		config = sample()[1]
		reader = Mock(side_effect=pages) if pages else Mock(return_value=(rows,
			{"syncUntil": now.isoformat(), "hasMore": False}))
		payload = source.load_snapshot("url", templates or [config], previous=previous,
			now=now, http=SimpleNamespace(read_template_page=reader))
		return payload, reader
	return run


@pytest.mark.parametrize("kind", ["budget", "operating", "purchase"])
def test_first_full_then_merge_new_and_changed_approvals_without_losing_history(scan, kind):
	row, template = sample(kind, status="RUNNING", result="NONE")
	untouched = {**row, "process_instance_id": "untouched"}
	baseline, first = scan([row, untouched], templates=[template])
	assert "updated_after" not in first.call_args.kwargs
	ended = {**row, "status": "COMPLETED", "result": "agree"}
	new = {**row, "process_instance_id": "new"}
	current, delta = scan([ended, new], previous=baseline, now=START + timedelta(minutes=5), templates=[template])
	assert delta.call_args.kwargs["updated_after"] == (START - timedelta(minutes=10)).isoformat()
	by_id = {item["process_instance_id"]: item for item in current["rows"]}
	assert len(by_id) == 3
	assert by_id[row["process_instance_id"]]["status"] == "approved"
	assert by_id["untouched"]["status"] == "pending"
	assert len(current["_refresh"]["changed_rows"]) == 2
	assert current["_refresh"]["full_templates"] == []


def test_unchanged_overlap_skips_projection_and_business_refresh(scan, monkeypatch):
	row, _ = sample(status="RUNNING", result="NONE")
	baseline, _ = scan([row])
	monkeypatch.setattr(source, "project", lambda *a: pytest.fail("unchanged approval reprojected"))
	current, _ = scan([{**row, "updated_at": "2026-11-25T01:05:00Z"}], previous=baseline,
		now=START + timedelta(minutes=5))
	assert current["rows"] == baseline["rows"]
	assert not current["_refresh"]["changed_rows"]
	assert current["sync_states"] != baseline["sync_states"]
	assert current["sync_fingerprints"] == baseline["sync_fingerprints"]


def test_empty_delta_retains_closed_approvals_and_advances_only_watermarks(scan):
	row, _ = sample()
	baseline, _ = scan([row])
	current, _ = scan([], previous=baseline, now=START + timedelta(minutes=5))
	assert current["rows"] == baseline["rows"]
	assert not current["_refresh"]["changed_rows"]
	state = next(iter(current["sync_states"].values()))
	assert state["watermark"] == (START + timedelta(minutes=5)).isoformat()
	assert state["reconciled_at"] == START.isoformat()


@pytest.mark.parametrize("change,expected", [
	({"status": "TERMINATED", "result": "NONE"}, "terminated"),
	({"status": "COMPLETED", "result": "refuse"}, "rejected"),
	({"deleted_at": "2026-11-25T01:04:00Z"}, "withdrawn"),
])
def test_status_and_tombstones_are_never_frozen_after_completion(scan, change, expected):
	row, _ = sample()
	baseline, _ = scan([row])
	current, _ = scan([{**row, **change}], previous=baseline, now=START + timedelta(minutes=5))
	assert current["rows"][0]["status"] == expected
	assert len(current["_refresh"]["changed_rows"]) == 1


def test_daily_full_reconciles_historical_corrections_and_missing_rows(scan):
	row, _ = sample()
	baseline, _ = scan([row, {**row, "process_instance_id": "missing"}])
	current, reader = scan([{**row, "status": "TERMINATED"}], previous=baseline, now=START + timedelta(days=1))
	assert "updated_after" not in reader.call_args.kwargs
	assert len(current["rows"]) == 1 and current["rows"][0]["status"] == "terminated"
	assert current["_refresh"]["full_templates"]
	assert next(iter(current["sync_states"].values()))["reconciled_at"] == (START + timedelta(days=1)).isoformat()


def test_delta_pages_keep_same_lower_bound_and_snapshot(scan):
	row, _ = sample()
	baseline, _ = scan([row])
	until = (START + timedelta(minutes=5)).isoformat()
	current, reader = scan([], previous=baseline, now=START + timedelta(minutes=5), pages=[
		([{**row, "process_instance_id": "new"}], {"syncUntil": until, "hasMore": True, "nextCursor": "next"}),
		([], {"syncUntil": until, "hasMore": False})])
	assert len(current["rows"]) == 2
	assert [c.kwargs["cursor"] for c in reader.call_args_list] == [None, "next"]
	assert len({c.kwargs["updated_after"] for c in reader.call_args_list}) == 1


def test_failed_delta_never_mutates_previous_snapshot_or_watermark(scan):
	row, _ = sample()
	baseline, _ = scan([row])
	before = deepcopy(baseline)
	with pytest.raises(ValueError, match="offline"):
		scan([], previous=baseline, now=START + timedelta(minutes=5), pages=[
			([{**row, "status": "TERMINATED"}], {"syncUntil": START.isoformat(), "hasMore": True, "nextCursor": "next"}),
			ValueError("offline")])
	assert baseline == before


def test_legacy_snapshot_and_changed_template_configuration_require_full_baseline(scan):
	row, template = sample()
	baseline, _ = scan([row])
	for previous, config in [({"rows": baseline["rows"]}, template),
		(baseline, {**template, "field_mapping": '{"amount":["new-amount"]}'})]:
		current, reader = scan([row], previous=previous, templates=[config], now=START + timedelta(minutes=5))
		assert "updated_after" not in reader.call_args.kwargs
		assert current["_refresh"]["full_templates"]


def test_accumulated_delta_cannot_exceed_local_snapshot_bound(scan, monkeypatch):
	row, _ = sample()
	baseline, _ = scan([row])
	monkeypatch.setattr(source, "MAX_ROWS", 1)
	with pytest.raises(source.ApprovalScanLimitExceeded):
		scan([{**row, "process_instance_id": "new"}], previous=baseline, now=START + timedelta(minutes=5))


def test_delta_cannot_move_an_instance_to_another_template(scan):
	row, template = sample()
	baseline, _ = scan([row])
	with pytest.raises(ValueError, match="duplicate"):
		scan([{**row, "process_code": "PROC-other"}], previous=baseline,
			templates=[{**template, "process_code": "PROC-other"}], now=START + timedelta(minutes=5))


@pytest.mark.parametrize("state", [{}, {"version": 1, "scope": "s", "watermark": "bad", "reconciled_at": START.isoformat()},
	{"version": 1, "scope": "old", "watermark": START.isoformat(), "reconciled_at": START.isoformat()}])
def test_invalid_or_different_scope_watermark_cannot_start_incremental(state):
	assert policy.plan(state, "s", now=START)["mode"] == "full"
