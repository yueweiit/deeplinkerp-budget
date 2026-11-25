"""Durable publication and background lifecycle, without site or upstream IO."""
import hashlib
import json
from copy import deepcopy
from datetime import datetime, timedelta
from types import SimpleNamespace

import frappe
import pytest

from budget.api import cn_approvals as api
from budget.integrations import cn_approval_source as source, cn_approval_store as store
from budget.services import cn_approval_sync as sync


@pytest.fixture
def runtime(monkeypatch):
	now = datetime(2026, 11, 20, 12)
	monkeypatch.setattr(sync.settings, "read_saved", lambda: None)
	state = SimpleNamespace(records={}, callbacks=[], jobs=[], logs=[], now=now)
	key = sync.identity('url', [])
	state.key = key
	def read(key, **kwargs):
		return deepcopy(state.records.get(key))
	def ensure(key):
		state.records.setdefault(key, {'name': key, 'sync_status': 'idle'})
		return read(key)
	def update(key, values):
		state.records[key].update(deepcopy(values))
	def commit():
		callbacks = state.callbacks[:]
		state.callbacks.clear()
		for callback in callbacks:
			callback()
	state.commit = commit
	state.old = {'rows': [{'amount': '12.30'}], 'fetched_at': '2026-11-20 10:00:00'}
	state.records[key] = {'name': key, 'sync_status': 'success', 'snapshot_id': 'old',
		'last_success_at': now - timedelta(hours=2), 'snapshot_json': json.dumps(state.old)}
	monkeypatch.setattr(store, 'read', read)
	monkeypatch.setattr(store, 'ensure', ensure)
	monkeypatch.setattr(store, 'update', update)
	monkeypatch.setattr(source, 'configuration', lambda: ('url', []))
	monkeypatch.setattr(source, 'load_snapshot', lambda *a, **kw: pytest.fail('unexpected upstream scan'))
	monkeypatch.setattr(sync, 'now_datetime', lambda: state.now)
	monkeypatch.setattr(sync, '_', lambda text: text)
	monkeypatch.setattr(frappe, 'conf', {'cn_approval_sync_enabled': 1})
	monkeypatch.setattr(frappe, 'flags', frappe._dict())
	monkeypatch.setattr(frappe, 'throw', lambda msg, *a: (_ for _ in ()).throw(ValueError(msg)))
	monkeypatch.setattr(frappe, 'db', SimpleNamespace(commit=commit, rollback=lambda: None,
		db_type='mariadb', sql=lambda *a: [(16 * 1024 * 1024,)], escape=lambda value: repr(value),
		after_commit=SimpleNamespace(add=state.callbacks.append)))
	monkeypatch.setattr(frappe, 'enqueue', lambda *a, **kw: state.jobs.append(kw))
	monkeypatch.setattr(frappe, 'logger', lambda *a: SimpleNamespace(error=lambda *a: state.logs.append(a)))
	return state


def test_local_reads_survive_cache_loss_without_contacting_oa(runtime, monkeypatch):
	monkeypatch.setattr(frappe, 'cache', SimpleNamespace(get_value=lambda *a, **kw: pytest.fail('Redis dependency')))
	first = source.get_snapshot()
	assert first['rows'] == runtime.old['rows'] and first['snapshot_id'] == 'old'
	assert source.get_snapshot()['rows'] == first['rows']
	assert runtime.jobs == []


def test_regional_projection_failure_cannot_publish_half_a_snapshot(runtime, monkeypatch):
	from deeplinkerp_branding.services import regional_expenses
	monkeypatch.setattr(source, 'load_snapshot', lambda *a, **kw: {'rows': [], 'fetched_at': str(runtime.now)})
	monkeypatch.setattr(regional_expenses, 'refresh_cn', lambda *a: (_ for _ in ()).throw(ValueError('allocation failed')))
	sync.request_sync()
	runtime.commit()
	job = runtime.jobs[-1]
	sync.run_sync(job['key'], job['token'])
	assert runtime.records[runtime.key]['snapshot_id'] == 'old'
	assert runtime.records[runtime.key]['sync_status'] == 'failed'
	assert runtime.records[runtime.key]['error_code'] == 'allocation_failed'


def test_first_sync_is_distinct_from_a_successful_empty_snapshot(runtime):
	runtime.records.clear()
	assert sync.read_snapshot()['has_snapshot'] is False
	assert sync.read_snapshot()['sync']['auto_sync_due'] is True
	runtime.records[runtime.key] = {'name': runtime.key, 'snapshot_id': 'empty', 'last_success_at': runtime.now,
		'snapshot_json': '{"rows": []}', 'sync_status': 'success'}
	assert sync.read_snapshot()['has_snapshot'] is True
	assert sync.get_status()['auto_sync_due'] is False


def test_empty_incremental_publication_only_advances_sync_metadata(runtime, monkeypatch):
	from deeplinkerp_branding.services import regional_expenses
	def scan(*args, previous, **kwargs):
		assert previous['rows'] == runtime.old['rows']
		return {'rows': previous['rows'], 'fetched_at': str(runtime.now),
			'sync_states': {'template': {'watermark': 'completed'}},
			'_refresh': {'full_templates': [], 'changed_rows': []}}
	monkeypatch.setattr(source, 'load_snapshot', scan)
	monkeypatch.setattr(regional_expenses, 'refresh_cn', lambda *a, **kw: pytest.fail('unchanged business writes'))
	sync.request_sync(); runtime.commit()
	sync.run_sync(runtime.key, runtime.jobs[0]['token'])
	record = runtime.records[runtime.key]
	assert record['sync_status'] == 'success'
	payload = json.loads(record['snapshot_json'])
	assert payload['rows'] == runtime.old['rows']
	assert payload['sync_states']['template']['watermark'] == 'completed'
	assert '_refresh' not in payload


def test_failed_incremental_allocation_retains_completed_watermark(runtime, monkeypatch):
	from deeplinkerp_branding.services import regional_expenses
	baseline = json.loads(runtime.records[runtime.key]['snapshot_json'])
	baseline['sync_states'] = {'template': {'watermark': 'old'}}
	runtime.records[runtime.key]['snapshot_json'] = json.dumps(baseline)
	def scan(*args, **kwargs):
		return {'rows': [{'amount': '20'}], 'fetched_at': str(runtime.now),
			'sync_states': {'template': {'watermark': 'new'}},
			'_refresh': {'full_templates': [], 'changed_rows': [{'amount': '20'}]}}
	def fail(rows, templates, *, incremental):
		assert incremental
		raise ValueError('allocation failed')
	monkeypatch.setattr(source, 'load_snapshot', scan)
	monkeypatch.setattr(regional_expenses, 'refresh_cn', fail)
	sync.request_sync(); runtime.commit()
	sync.run_sync(runtime.key, runtime.jobs[0]['token'])
	record = runtime.records[runtime.key]
	assert record['error_code'] == 'allocation_failed'
	assert json.loads(record['snapshot_json']) == baseline


@pytest.mark.parametrize('seconds, due', [(299, False), (300, True), (301, True)])
def test_auto_sync_becomes_due_after_five_minutes_without_shortening_worker_lease(runtime, seconds, due):
	record = runtime.records[runtime.key]
	record.update(last_success_at=runtime.now - timedelta(seconds=seconds),
		queued_at=runtime.now - timedelta(seconds=seconds))
	assert sync.get_status()['auto_sync_due'] is due
	record.update(sync_status='running', started_at=runtime.now - timedelta(seconds=seconds),
		heartbeat_at=runtime.now - timedelta(seconds=seconds))
	assert sync.get_status()['active']
	assert not sync.get_status()['auto_sync_due']


def scheduled_runtime(runtime, monkeypatch):
	monkeypatch.setattr(frappe, 'get_installed_apps', lambda: ['deeplinkerp_branding'])
	monkeypatch.setattr(frappe.db, 'table_exists', lambda doctype: True, raising=False)
	return runtime.records[runtime.key]


def test_cron_runs_next_five_minute_tick_even_when_previous_scan_finished_later(runtime, monkeypatch):
	record = scheduled_runtime(runtime, monkeypatch)
	started = runtime.now
	record.update(queued_at=started, last_success_at=started + timedelta(seconds=20))
	runtime.now = started + timedelta(minutes=5)
	assert not sync.get_status()['auto_sync_due'], 'page freshness backoff is unchanged'
	sync.scheduled_sync()
	assert record['sync_status'] == 'queued'
	assert len(runtime.callbacks) == 1
	# A concurrent scheduler/page attempt must reuse the claim even before enqueue.
	sync.scheduled_sync()
	assert len(runtime.callbacks) == 1


@pytest.mark.parametrize('state', ['queued', 'running'])
def test_cron_reuses_live_job_instead_of_starting_another_scan(runtime, monkeypatch, state):
	record = scheduled_runtime(runtime, monkeypatch)
	record.update(sync_status=state, queued_at=runtime.now - timedelta(minutes=5),
		started_at=runtime.now - timedelta(minutes=5), heartbeat_at=runtime.now)
	sync.scheduled_sync()
	assert record['sync_status'] == state
	assert not runtime.callbacks


def test_cron_respects_disabled_sync(runtime, monkeypatch):
	scheduled_runtime(runtime, monkeypatch)
	monkeypatch.setattr(sync, 'enabled', lambda: False)
	sync.scheduled_sync()
	assert not runtime.callbacks


def test_region_filtered_snapshot_requires_one_rebuild_then_reuses_local_data(runtime, monkeypatch):
	# Before region filtering was removed, the durable key excluded the version.
	legacy_key = hashlib.sha256(json.dumps(['url', []], sort_keys=True).encode()).hexdigest()
	legacy = runtime.records.pop(runtime.key)
	legacy.update(name=legacy_key, last_success_at=runtime.now)
	runtime.records[legacy_key] = legacy
	assert runtime.key != legacy_key
	initial = source.get_snapshot()
	assert not initial['has_snapshot'] and initial['sync']['auto_sync_due']
	assert not runtime.jobs

	payload = {'rows': runtime.old['rows'] + [{'amount': '23.40'}], 'fetched_at': str(runtime.now)}
	monkeypatch.setattr(source, 'load_snapshot', lambda *a, **kw: payload)
	sync.request_sync(if_stale=True)
	runtime.commit()
	sync.run_sync(runtime.key, runtime.jobs[0]['token'])
	monkeypatch.setattr(source, 'load_snapshot', lambda *a, **kw: pytest.fail('repeated upstream scan'))
	assert source.get_snapshot()['rows'] == payload['rows']
	assert sync.request_sync(if_stale=True)['status'] == 'success'
	assert source.get_snapshot()['rows'] == payload['rows']
	assert len(runtime.jobs) == 1 and runtime.records[legacy_key] == legacy


@pytest.fixture
def configured_runtime(runtime, monkeypatch):
	runtime.templates = source.normalize_templates([{"approval_type": "operating", "corp_id": "corp-A",
		"process_code": "PROC-test", "field_mapping": '{"region": ["old-region"], "amount": ["amount-1"]}'}], ["corp-A"])
	monkeypatch.setattr(source, 'configuration', lambda: ('url', deepcopy(runtime.templates)))
	record = runtime.records.pop(runtime.key)
	runtime.key = sync.identity(*source.configuration())
	runtime.records[runtime.key] = {**record, 'name': runtime.key, 'last_success_at': runtime.now}
	return runtime


def test_removing_region_mapping_keeps_fresh_local_snapshot(configured_runtime):
	runtime = configured_runtime
	runtime.templates[0]['field_mapping'] = '{"amount": ["amount-1"]}'
	result = source.get_snapshot()
	assert result['snapshot_id'] == 'old' and result['rows'] == runtime.old['rows']
	assert not result['sync']['auto_sync_due']
	assert sync.request_sync(if_stale=True)['status'] == 'success'
	assert not runtime.jobs and not runtime.callbacks


@pytest.mark.parametrize('stage', ['queued', 'running'])
@pytest.mark.parametrize('mapping', ['{"amount": ["amount-1"]}', '{"region": ["new-region"], "amount": ["amount-1"]}'])
def test_region_mapping_edits_do_not_abort_sync(configured_runtime, monkeypatch, stage, mapping):
	runtime = configured_runtime
	sync.request_sync(); runtime.commit()
	if stage == 'queued':
		runtime.templates[0]['field_mapping'] = mapping
	def scan(*args, progress, previous):
		progress(completed_templates=0, page_count=1)
		runtime.templates[0]['field_mapping'] = mapping
		assert sync.get_status()['status'] == 'running'
		assert sync.request_sync(if_stale=True)['status'] == 'running'
		return {'rows': [{'amount': '99.00'}], 'fetched_at': str(runtime.now)}
	monkeypatch.setattr(source, 'load_snapshot', scan)
	sync.run_sync(runtime.key, runtime.jobs[0]['token'])
	result = source.get_snapshot()
	assert result['sync']['status'] == 'success'
	assert result['snapshot_id'] != 'old' and result['rows'] == [{'amount': '99.00'}]
	assert len(runtime.jobs) == 1 and not runtime.callbacks


@pytest.mark.parametrize('changes', [
	{'field_mapping': '{"amount": ["amount-2"]}'},
	{'classification_fields': '[["category-2"]]'},
	{'corp_id': 'corp-B'}, {'process_code': 'PROC-other'}, {'approval_type': 'purchase'},
])
def test_effective_template_edits_still_reject_outdated_sync(configured_runtime, monkeypatch, changes):
	runtime = configured_runtime
	sync.request_sync(); runtime.commit()
	def scan(*args, **kwargs):
		runtime.templates[0].update(changes)
		return {'rows': ['outdated'], 'fetched_at': str(runtime.now)}
	monkeypatch.setattr(source, 'load_snapshot', scan)
	sync.run_sync(runtime.key, runtime.jobs[0]['token'])
	assert not source.get_snapshot()['has_snapshot']
	assert runtime.records[runtime.key]['snapshot_id'] == 'old'
	assert runtime.records[runtime.key]['error_code'] == 'configuration_changed'


def test_enqueue_is_after_commit_and_repeated_requests_share_claim(runtime):
	one = sync.request_sync()
	two = sync.request_sync()
	assert one == two and one['status'] == 'queued'
	assert len(runtime.callbacks) == 1 and not runtime.jobs
	assert source.get_snapshot()['snapshot_id'] == 'old'
	runtime.commit()
	assert len(runtime.jobs) == 1 and runtime.jobs[0]['timeout'] == sync.JOB_TIMEOUT


def test_worker_only_publishes_complete_snapshot(runtime, monkeypatch):
	sync.request_sync(); runtime.commit()
	token = runtime.jobs[0]['token']
	def scan(*args, progress, previous):
		assert source.get_snapshot()['snapshot_id'] == 'old'
		progress(completed_templates=1, page_count=3)
		assert sync.get_status()['page_count'] == 3
		assert sync.get_status()['status'] == 'running'
		assert source.get_snapshot()['rows'] == runtime.old['rows']
		return {'rows': [{'amount': '99.00'}], 'fetched_at': str(runtime.now)}
	monkeypatch.setattr(source, 'load_snapshot', scan)
	sync.run_sync(runtime.key, token)
	result = source.get_snapshot()
	assert result['snapshot_id'] != 'old' and result['rows'][0]['amount'] == '99.00'
	assert result['sync']['status'] == 'success'
	assert result['sync']['auto_sync_due'] is False


def test_partial_failure_preserves_last_complete_snapshot_and_backs_off(runtime, monkeypatch):
	sync.request_sync(); runtime.commit()
	def scan(*args, progress, previous):
		progress(completed_templates=1, page_count=2)
		raise RuntimeError('secret upstream payload')
	monkeypatch.setattr(source, 'load_snapshot', scan)
	sync.run_sync(runtime.key, runtime.jobs[0]['token'])
	result = source.get_snapshot()
	assert result['rows'] == runtime.old['rows'] and result['snapshot_id'] == 'old'
	assert result['sync']['status'] == 'failed' and result['sync']['error_code'] == 'scan_failed'
	assert 'secret' not in repr(runtime.logs) + repr(result['sync'])
	sync.request_sync(if_stale=True)
	assert len(runtime.callbacks) == 0
	sync.request_sync()
	assert len(runtime.callbacks) == 1


@pytest.mark.parametrize("error, expected", [
	(source.ApprovalScanTimeout("secret upstream payload"), "scan_timeout"),
	(source.ApprovalScanLimitExceeded("secret upstream payload"), "scan_limit"),
	(sync.JobTimeoutException("secret upstream payload"), "job_timeout"),
])
def test_typed_failures_retain_progress_and_old_snapshot_without_exposing_payload(runtime, monkeypatch, error, expected):
	sync.request_sync(); runtime.commit()
	def scan(*args, progress, previous):
		progress(completed_templates=8, page_count=17)
		raise error
	monkeypatch.setattr(source, "load_snapshot", scan)
	sync.run_sync(runtime.key, runtime.jobs[0]["token"])
	result = sync.read_snapshot()
	assert result["snapshot_id"] == "old" and result["rows"] == runtime.old["rows"]
	assert result["sync"]["error_code"] == expected
	assert result["sync"]["status"] == "failed" and result["sync"]["page_count"] == 17
	assert result["sync"]["completed_templates"] == 8
	assert "secret upstream payload" not in repr(runtime.logs) + repr(result)
	detail = json.loads(next(log[1] for log in runtime.logs if "failure detail" in log[0]))
	assert detail["stage"] == "scan" and detail["exception_type"] == type(error).__name__
	assert detail["frames"][-1]["function"] == "scan"


def test_projection_can_finish_after_old_lease_without_allowing_a_duplicate_scan(runtime, monkeypatch):
	from deeplinkerp_branding.services import regional_expenses
	sync.request_sync(); runtime.commit()
	def scan(*args, progress, previous):
		runtime.now += timedelta(seconds=120)
		progress(completed_templates=11, page_count=21)
		return {"rows": [], "fetched_at": str(runtime.now)}
	def project(*args):
		# A run with a fast scan may use the remaining worker budget to publish.
		runtime.now += timedelta(seconds=301)
		assert sync.get_status()["active"]
		assert sync.request_sync()["status"] == "running"
		assert len(runtime.jobs) == 1
	monkeypatch.setattr(source, "load_snapshot", scan)
	monkeypatch.setattr(regional_expenses, "refresh_cn", project)
	sync.run_sync(runtime.key, runtime.jobs[0]["token"])
	assert sync.get_status()["status"] == "success"
	assert source.SCAN_TIMEOUT < sync.JOB_TIMEOUT < sync.RUN_LEASE


def test_failed_publication_records_its_stage_and_retains_previous_snapshot(runtime, monkeypatch):
	sync.request_sync(); runtime.commit()
	monkeypatch.setattr(source, "load_snapshot", lambda *a, **kw: {"rows": [], "fetched_at": str(runtime.now)})
	update = store.update
	def reject_payload(key, values):
		if "snapshot_json" in values:
			raise ValueError("secret database payload")
		update(key, values)
	monkeypatch.setattr(store, "update", reject_payload)
	sync.run_sync(runtime.key, runtime.jobs[0]["token"])
	assert sync.read_snapshot()["snapshot_id"] == "old"
	assert sync.get_status()["error_code"] == "publish_failed"
	assert "secret database payload" not in repr(runtime.logs)


def test_oversized_snapshot_fails_before_allocation_and_keeps_old_watermark(runtime, monkeypatch):
	from deeplinkerp_branding.services import regional_expenses
	sync.request_sync(); runtime.commit()
	monkeypatch.setattr(source, 'load_snapshot', lambda *a, **kw: {'rows': [], 'fetched_at': str(runtime.now)})
	monkeypatch.setattr(frappe.db, 'sql', lambda *a: [(100,)])
	monkeypatch.setattr(regional_expenses, 'refresh_cn', lambda *a, **kw: pytest.fail('business writes before size check'))
	sync.run_sync(runtime.key, runtime.jobs[0]['token'])
	assert sync.read_snapshot()['snapshot_id'] == 'old'
	assert sync.get_status()['error_code'] == 'publish_failed'
	assert sync.get_status()['status'] == 'failed'


@pytest.mark.parametrize('failures', [1, 2, 3])
def test_allocation_deadlock_retries_local_transaction_without_rescanning_oa(runtime, monkeypatch, failures):
	from deeplinkerp_branding.services import regional_expenses
	from unittest.mock import Mock
	scan = Mock(return_value={'rows': [], 'fetched_at': str(runtime.now),
		'sync_states': {'template': {'watermark': 'new'}}})
	refresh = Mock(side_effect=[frappe.QueryDeadlockError() for _ in range(failures)] + [None])
	monkeypatch.setattr(source, 'load_snapshot', scan)
	monkeypatch.setattr(regional_expenses, 'refresh_cn', refresh)
	sync.request_sync(); runtime.commit()
	sync.run_sync(runtime.key, runtime.jobs[0]['token'])
	assert scan.call_count == 1 and refresh.call_count == min(failures + 1, 3)
	result = sync.read_snapshot()
	if failures < 3:
		assert result['sync']['status'] == 'success'
		assert result['sync_states']['template']['watermark'] == 'new'
	else:
		assert result['snapshot_id'] == 'old'
		assert result['sync']['error_code'] == 'allocation_failed'


@pytest.mark.parametrize('change', ['configuration', 'token'])
def test_deadlock_retry_rechecks_configuration_and_worker_ownership(runtime, monkeypatch, change):
	from deeplinkerp_branding.services import regional_expenses
	from unittest.mock import Mock
	monkeypatch.setattr(source, 'load_snapshot', lambda *a, **kw: {'rows': [], 'fetched_at': str(runtime.now)})
	def conflict(*args):
		if change == 'configuration':
			monkeypatch.setattr(source, 'configuration', lambda: ('other', []))
		else:
			runtime.records[runtime.key]['sync_token'] = 'replacement'
		raise frappe.QueryDeadlockError()
	refresh = Mock(side_effect=conflict)
	monkeypatch.setattr(regional_expenses, 'refresh_cn', refresh)
	sync.request_sync(); runtime.commit()
	sync.run_sync(runtime.key, runtime.jobs[0]['token'])
	assert refresh.call_count == 1
	assert runtime.records[runtime.key]['snapshot_id'] == 'old'
	if change == 'configuration':
		assert runtime.records[runtime.key]['error_code'] == 'configuration_changed'


def test_connection_loss_keeps_original_diagnostics_when_rollback_is_rejected(runtime, monkeypatch):
	sync.request_sync(); runtime.commit()
	monkeypatch.setattr(source, "load_snapshot", lambda *a, **kw: {"rows": [], "fetched_at": str(runtime.now)})
	original = RuntimeError(2006, "secret database payload")
	update = store.update
	def disconnect(key, values):
		if "snapshot_json" in values:
			raise original
		update(key, values)
	def rejected_rollback():
		raise ValueError("connection execution invalid")
	monkeypatch.setattr(store, "update", disconnect)
	monkeypatch.setattr(frappe.db, "rollback", rejected_rollback)
	with pytest.raises(RuntimeError) as raised:
		sync.run_sync(runtime.key, runtime.jobs[0]["token"])
	assert raised.value is original
	details = [json.loads(log[1]) for log in runtime.logs if "failure detail" in log[0]]
	assert details[0]["stage"] == "publish"
	assert details[0]["numeric_code"] == 2006
	assert details[-1]["stage"] == "failure_recording"
	assert "secret database payload" not in repr(runtime.logs)
	assert sync.read_snapshot()["snapshot_id"] == "old"


def test_queue_failure_is_visible_and_keeps_old_snapshot(runtime, monkeypatch):
	monkeypatch.setattr(frappe, 'enqueue', lambda *a, **kw: (_ for _ in ()).throw(RuntimeError('queue offline')))
	sync.request_sync(); runtime.commit()
	assert sync.get_status()['status'] == 'failed'
	assert sync.get_status()['error_code'] == 'queue_failed'
	assert sync.read_snapshot()['snapshot_id'] == 'old'


@pytest.mark.parametrize('state, field, lease', [('queued', 'queued_at', sync.QUEUE_LEASE), ('running', 'heartbeat_at', sync.RUN_LEASE)])
def test_expired_claim_is_reported_and_replacement_fences_old_worker(runtime, state, field, lease):
	sync.request_sync(); runtime.commit()
	old_token = runtime.jobs[0]['token']
	runtime.records[runtime.key].update(sync_status=state, **{field: runtime.now - timedelta(seconds=lease)})
	assert sync.get_status()['error_code'] == 'sync_timeout'
	sync.request_sync(); runtime.commit()
	assert runtime.jobs[1]['token'] != old_token
	sync.run_sync(runtime.key, old_token)
	assert sync.get_status()['status'] == 'queued' and sync.read_snapshot()['snapshot_id'] == 'old'


@pytest.mark.parametrize('elapsed, expected', [(900, 'running'), (959, 'running'), (960, 'failed'), (961, 'failed')])
def test_running_deadline_is_not_extended_by_a_late_heartbeat(runtime, elapsed, expected):
	started = runtime.now
	runtime.records[runtime.key].update(sync_status='running',
		queued_at=started - timedelta(seconds=sync.QUEUE_LEASE - 1), started_at=started,
		heartbeat_at=started + timedelta(seconds=599))
	runtime.now = started + timedelta(seconds=elapsed)
	result = sync.get_status()
	assert result['status'] == expected
	assert result['active'] is (expected == 'running')
	assert result['error_code'] == ('sync_timeout' if expected == 'failed' else None)
	assert sync.read_snapshot()['snapshot_id'] == 'old'
	if expected == 'running':
		assert sync.request_sync()['status'] == 'running'
		runtime.commit()
		assert runtime.jobs == []


def test_absolute_deadline_allows_retry_and_fences_the_previous_scan(runtime, monkeypatch):
	sync.request_sync(); runtime.commit()
	old_token = runtime.jobs[0]['token']
	def scan(*args, progress, previous):
		runtime.now += timedelta(seconds=599)
		progress(completed_templates=11, page_count=21)
		runtime.now += timedelta(seconds=361)
		assert sync.get_status()['error_code'] == 'sync_timeout'
		with pytest.raises(sync.SupersededSync):
			progress(completed_templates=11, page_count=21)
		assert sync.request_sync()['status'] == 'queued'
		runtime.commit()
		return {'rows': ['outdated'], 'fetched_at': str(runtime.now)}
	monkeypatch.setattr(source, 'load_snapshot', scan)
	sync.run_sync(runtime.key, old_token)
	assert len(runtime.jobs) == 2 and runtime.jobs[1]['token'] != old_token
	assert sync.get_status()['status'] == 'queued'
	result = sync.read_snapshot()
	assert result['snapshot_id'] == 'old' and result['rows'] == runtime.old['rows']


def test_running_record_without_start_time_uses_heartbeat_for_compatibility(runtime):
	runtime.records[runtime.key].update(sync_status='running', started_at=None,
		queued_at=runtime.now - timedelta(hours=1), heartbeat_at=runtime.now)
	assert sync.get_status()['active'] is True
	runtime.now += timedelta(seconds=sync.RUN_LEASE)
	assert sync.get_status()['error_code'] == 'sync_timeout'


def test_replaced_worker_cannot_publish_after_its_scan(runtime, monkeypatch):
	sync.request_sync(); runtime.commit()
	old_token = runtime.jobs[0]['token']
	def scan(*a, **kw):
		runtime.records[runtime.key]['heartbeat_at'] = runtime.now - timedelta(seconds=sync.RUN_LEASE)
		sync.request_sync(); runtime.commit()
		return {'rows': ['partial'], 'fetched_at': str(runtime.now)}
	monkeypatch.setattr(source, 'load_snapshot', scan)
	sync.run_sync(runtime.key, old_token)
	assert sync.read_snapshot()['snapshot_id'] == 'old'
	assert sync.get_status()['status'] == 'queued'


def test_duplicate_delivery_does_not_scan_a_running_job(runtime):
	sync.request_sync(); runtime.commit()
	runtime.records[runtime.key].update(sync_status='running', heartbeat_at=runtime.now)
	sync.run_sync(runtime.key, runtime.jobs[0]['token'])
	assert sync.get_status()['status'] == 'running'


def test_changed_source_cannot_expose_or_overwrite_previous_source(runtime, monkeypatch):
	sync.request_sync(); runtime.commit()
	monkeypatch.setattr(source, 'configuration', lambda: ('changed-url', []))
	assert sync.read_snapshot()['has_snapshot'] is False
	sync.run_sync(runtime.key, runtime.jobs[0]['token'])
	assert runtime.records[runtime.key]['error_code'] == 'configuration_changed'
	assert runtime.records[runtime.key]['snapshot_id'] == 'old'


def test_source_change_during_scan_rejects_publication(runtime, monkeypatch):
	sync.request_sync(); runtime.commit()
	def scan(*a, **kw):
		monkeypatch.setattr(source, 'configuration', lambda: ('changed-url', []))
		return {'rows': ['wrong source'], 'fetched_at': str(runtime.now)}
	monkeypatch.setattr(source, 'load_snapshot', scan)
	sync.run_sync(runtime.key, runtime.jobs[0]['token'])
	assert runtime.records[runtime.key]['snapshot_id'] == 'old'
	assert runtime.records[runtime.key]['sync_status'] == 'failed'


def test_fresh_reads_do_not_auto_queue_but_manual_refresh_can(runtime):
	runtime.records[runtime.key]['last_success_at'] = runtime.now
	assert sync.request_sync(if_stale=True)['status'] == 'success'
	assert not runtime.callbacks
	assert sync.request_sync()['status'] == 'queued'


def test_disabled_sync_keeps_local_data_readable(runtime, monkeypatch):
	monkeypatch.setattr(frappe, 'conf', {'cn_approval_sync_enabled': 0})
	assert sync.read_snapshot()['snapshot_id'] == 'old'
	assert sync.get_status()['auto_sync_due'] is False
	with pytest.raises(ValueError, match='disabled'):
		sync.request_sync()


def test_export_without_initial_snapshot_is_not_empty_success(monkeypatch):
	monkeypatch.setattr(frappe, 'only_for', lambda *a: None)
	monkeypatch.setattr(api.service, 'filters', lambda *a: {})
	monkeypatch.setattr(api.service, 'load', lambda: ({'has_snapshot': False}, [], {}))
	monkeypatch.setattr(api, '_', lambda text: text)
	monkeypatch.setattr(frappe, 'throw', lambda msg, *a: (_ for _ in ()).throw(ValueError(msg)))
	with pytest.raises(ValueError, match='before exporting'):
		api.export_data()



def test_claim_retries_the_whole_transaction_after_a_concurrent_initialization(runtime, monkeypatch):
	calls = []
	def claim(**kwargs):
		calls.append('claim')
		if calls.count('claim') == 1:
			raise frappe.QueryDeadlockError('snapshot conflict')
		return {'status': 'queued'}
	monkeypatch.setattr(sync, 'claim_sync', claim)
	monkeypatch.setattr(frappe.db, 'rollback', lambda: calls.append('rollback'))
	assert sync.request_sync() == {'status': 'queued'}
	assert calls == ['claim', 'rollback', 'claim']


def test_claim_retries_are_bounded_and_never_report_success_on_exhaustion(runtime, monkeypatch):
	calls = []
	def claim(**kwargs):
		calls.append('claim')
		raise frappe.QueryDeadlockError('snapshot conflict')
	monkeypatch.setattr(sync, 'claim_sync', claim)
	monkeypatch.setattr(frappe.db, 'rollback', lambda: calls.append('rollback'))
	with pytest.raises(frappe.QueryDeadlockError):
		sync.request_sync()
	assert calls == ['claim', 'rollback'] * 3


@pytest.mark.parametrize('value, expected', [
	(True, True), (False, False), (1, True), (0, False), ('1', True), ('0', False),
	('true', True), ('false', False), ('TRUE', True), ('FALSE', False), (' false ', False),
	(None, False), ('', False),
])
def test_enqueue_api_preserves_json_and_form_boolean_semantics(monkeypatch, value, expected):
	monkeypatch.setattr(api.service, 'require_reader', lambda: None)
	monkeypatch.setattr(sync, 'request_sync', lambda **kwargs: kwargs)
	assert api.enqueue_sync(if_stale=value) == {'if_stale': expected}


@pytest.mark.parametrize('condition', ['fresh', 'recent_failure'])
@pytest.mark.parametrize('wire_value', ['true', '1'])
def test_form_encoded_automatic_request_obeys_server_freshness_and_retry_backoff(runtime, monkeypatch, condition, wire_value):
	from werkzeug.test import EnvironBuilder
	from werkzeug.wrappers import Request
	monkeypatch.setattr(api.service, 'require_reader', lambda: None)
	if condition == 'fresh':
		runtime.records[runtime.key]['last_success_at'] = runtime.now
	else:
		runtime.records[runtime.key].update(sync_status='failed', queued_at=runtime.now,
			error_code='source_failed')
	request = Request(EnvironBuilder(method='POST', data={'if_stale': wire_value}).get_environ())
	result = api.enqueue_sync(**request.form.to_dict())
	assert result['active'] is False
	assert runtime.callbacks == [] and runtime.jobs == []


@pytest.mark.parametrize('wire_value', ['false', '0'])
def test_form_encoded_manual_request_can_refresh_fresh_data(runtime, monkeypatch, wire_value):
	from werkzeug.test import EnvironBuilder
	from werkzeug.wrappers import Request
	monkeypatch.setattr(api.service, 'require_reader', lambda: None)
	runtime.records[runtime.key]['last_success_at'] = runtime.now
	request = Request(EnvironBuilder(method='POST', data={'if_stale': wire_value}).get_environ())
	assert api.enqueue_sync(**request.form.to_dict())['status'] == 'queued'
	assert len(runtime.callbacks) == 1
