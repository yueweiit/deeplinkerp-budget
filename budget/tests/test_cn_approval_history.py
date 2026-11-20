"""Disabled templates retain only verified snapshots from the current source."""
import json
import pytest
from budget.integrations.cn_approval_history import merge_retired

TEMPLATE={'corp_id':'corp','process_code':'PROC-old','approval_type':'budget'}
def record(rows, source='source', templates=None, time='2026-11-20'):
	return {'last_success_at':time,'snapshot_json':json.dumps({'rows':rows,'source_hash':source,'templates':templates if templates is not None else [TEMPLATE]})}
ROW={'key':'old','corp_id':'corp','process_code':'PROC-old','kind':'budget','amount':'100.00'}


def test_latest_verified_snapshot_wins_and_other_connections_are_excluded():
	result=merge_retired({'rows':[]},[TEMPLATE],[record([{**ROW,'amount':'999.00'}],source='other'),record([ROW]),record([{**ROW,'amount':'50.00'}])],'source')
	assert len(result['rows'])==1 and result['rows'][0]['amount']=='100.00'
	assert result['rows'][0]['retired_template'] and result['missing_history_templates']==0


def test_verified_empty_snapshot_does_not_resurrect_older_records():
	assert merge_retired({'rows':[]},[TEMPLATE],[record([]),record([ROW])],'source')['rows']==[]


def test_active_rows_win_and_unverified_history_is_explicitly_missing():
	result=merge_retired({'rows':[{**ROW,'amount':'150.00'}]},[TEMPLATE],[record([ROW])],'source')
	assert result['rows'][0]['amount']=='150.00' and len(result['rows'])==1
	result=merge_retired({'rows':[]},[TEMPLATE],[record([ROW],templates=[])],'source')
	assert not result['rows'] and result['missing_history_templates']==1


def test_disabled_corporation_outside_current_whitelist_cannot_reenter_reports(monkeypatch):
	from types import SimpleNamespace
	from budget.integrations import cn_approval_history as history
	monkeypatch.setattr(history.source,'get_snapshot',lambda:{'rows':[]})
	monkeypatch.setattr(history.settings,'read_saved',lambda:{'templates':[{**TEMPLATE,'enabled':False}]})
	monkeypatch.setattr(history.source,'configuration',lambda:('url',[]))
	monkeypatch.setattr(history.source,'client',lambda:SimpleNamespace(configuration=lambda:('url',['different-corp'])))
	monkeypatch.setattr(history.frappe,'get_all',lambda *a,**kw:(_ for _ in ()).throw(AssertionError('out-of-scope history lookup')))
	assert history.read_snapshot()['rows']==[]


def test_never_enabled_templates_require_no_history_or_archive_lookup(monkeypatch):
	from budget.integrations import cn_approval_history as history
	snapshot = {'rows': [ROW]}
	monkeypatch.setattr(history.source, 'get_snapshot', lambda: snapshot)
	monkeypatch.setattr(history.settings, 'read_saved', lambda: {'templates': [
		{**TEMPLATE, 'enabled': 0, 'history_state': 'never_enabled'}]})
	monkeypatch.setattr(history.frappe, 'get_all', lambda *a, **kw: pytest.fail('unused templates must not look up history'))
	assert history.read_snapshot() == snapshot
	assert merge_retired(snapshot, [{**TEMPLATE, 'history_state': 'never_enabled'}], [], 'source')['missing_history_templates'] == 0


@pytest.mark.parametrize('state', [None, '', 'expected'])
def test_legacy_or_previously_enabled_templates_still_require_verified_history(state):
	disabled = [{**TEMPLATE, 'history_state': state},
		{**TEMPLATE, 'process_code': 'PROC-new', 'history_state': 'never_enabled'}]
	assert merge_retired({'rows': []}, disabled, [], 'source')['missing_history_templates'] == 1
	verified = merge_retired({'rows': []}, disabled, [record([]), record([ROW])], 'source')
	assert verified['missing_history_templates'] == 0 and verified['rows'] == []
