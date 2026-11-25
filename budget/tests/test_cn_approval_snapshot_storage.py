"""Large approval snapshots must publish without losing source evidence."""
import base64
import json
import zlib
from copy import deepcopy
from types import SimpleNamespace

import pytest

from budget.integrations import cn_approval_store as store
from budget.integrations.cn_approval_history import merge_retired


def payload():
	return {'source_hash': 'source', 'templates': [
		{'corp_id': 'corp', 'process_code': 'PROC-test', 'approval_type': 'operating'}],
		'rows': [{'key': 'one', 'corp_id': 'corp', 'process_code': 'PROC-test', 'kind': 'operating',
			'amount': '120.01', '_routing_source': {'row': {'raw_payload': {'text': '审批 Español \\ "\n' * 100}}}}],
		'sync_states': {'template': {'watermark': '2026-11-25T03:00:00Z'}},
		'sync_fingerprints': {'one': 'fingerprint'}}


def test_legacy_snapshot_and_small_writes_remain_json_compatible():
	data = payload()
	assert store.decode_snapshot(json.dumps(data)) == data
	assert json.loads(store.encode_snapshot(data)) == data


def test_large_snapshot_crosses_old_packet_limit_but_roundtrips_every_fact_and_watermark(monkeypatch):
	data = payload()
	data['rows'][0]['_routing_source']['row']['raw_payload']['text'] *= 10000
	before = deepcopy(data)
	assert len(json.dumps(data, ensure_ascii=False).encode()) > 16 * 1024 * 1024
	encoded = store.encode_snapshot(data)
	assert len(encoded.encode()) < 1024 * 1024
	assert store.decode_snapshot(encoded) == before == data
	assert '"source_hash": "source"' in encoded, 'historical SQL lookup remains compatible'
	record = {'snapshot_id': 'new', 'snapshot_json': encoded, 'last_success_at': '2026-11-25'}
	assert store.snapshot(record)['rows'] == data['rows']


def test_compressed_history_retains_disabled_template_and_excludes_other_connections(monkeypatch):
	monkeypatch.setattr(store, 'COMPRESS_AFTER', 1)
	data = payload()
	good = {'snapshot_json': store.encode_snapshot(data), 'last_success_at': '2026-11-25'}
	other = {'snapshot_json': store.encode_snapshot({**data, 'source_hash': 'other'}), 'last_success_at': '2026-11-26'}
	result = merge_retired({'rows': []}, data['templates'], [other, good], 'source')
	assert result['missing_history_templates'] == 0
	assert result['rows'][0] == {**data['rows'][0], 'retired_template': True}


@pytest.mark.parametrize('corruption', ['encoding', 'base64', 'truncated', 'trailing', 'source', 'limit'])
def test_corrupt_or_oversized_compressed_data_is_rejected(monkeypatch, corruption):
	monkeypatch.setattr(store, 'COMPRESS_AFTER', 1)
	envelope = json.loads(store.encode_snapshot(payload()))
	if corruption == 'encoding':
		envelope['encoding'] = 'unknown'
	elif corruption == 'base64':
		envelope['data'] = '!invalid!'
	elif corruption in ('truncated', 'trailing'):
		blob = base64.b64decode(envelope['data'])
		envelope['data'] = base64.b64encode(blob[:-2] if corruption == 'truncated' else blob + b'extra').decode()
	elif corruption == 'source':
		envelope['source_hash'] = 'other'
	else:
		monkeypatch.setattr(store, 'MAX_SNAPSHOT_BYTES', 10)
	with pytest.raises((ValueError, zlib.error)):
		store.decode_snapshot(json.dumps(envelope))


def test_size_guard_uses_escaped_utf8_bytes_and_keeps_database_connection_usable(monkeypatch):
	data = payload()
	text = store.encode_snapshot(data)
	escape = lambda value: "'" + value.replace('\\', '\\\\').replace("'", "\\'") + "'"
	needed = len(escape(text).encode()) + 65536
	monkeypatch.setattr(store.frappe, 'db', SimpleNamespace(db_type='mariadb', escape=escape,
		sql=lambda query: [(needed,)]))
	with pytest.raises(ValueError, match='packet limit'):
		store.prepare_snapshot(data)
	monkeypatch.setattr(store.frappe.db, 'sql', lambda query: [(needed + 1,)])
	assert store.prepare_snapshot(data) == text


def test_encode_rejects_excessive_snapshot_before_compression(monkeypatch):
	monkeypatch.setattr(store, 'MAX_SNAPSHOT_BYTES', 10)
	with pytest.raises(ValueError, match='size limit'):
		store.encode_snapshot(payload())
