"""Confirmation checks source permissions, revisions and leaves original facts intact."""
from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import Mock
import json

import frappe
import pytest

from budget.api import expenses as api
from budget.services import expense_amount_review as service, expense_approval_links as links
from budget.services import expense_report_refresh, expense_report_store, expense_group_mappings
from test_expense_amount_review import purchase


@pytest.fixture
def runtime(monkeypatch):
	raw, template, source, lines, entries = purchase()
	record = frappe._dict(name=source['source_key'], corp_id=source['corp_id'], process_code=source['process_code'],
		process_instance_id=source['process_instance_id'], origin='mx', state='stale', modified='revision-1',
		source_json=service.regional.encode(raw), template_json=service.regional.encode(template),
		facts_json=service.regional.fact_payload(source, lines), allocations_json=json.dumps(entries))
	settings = frappe._dict(business_timezone='America/Mexico_City', templates=[template])
	roles, companies, events, group_rows = ['Accounts Manager'], {'MX'}, [], []
	def only_for(allowed):
		if not set(roles).intersection(allowed):
			raise frappe.PermissionError('Role denied')
	def throw(message, exc=frappe.ValidationError, **kwargs):
		raise exc(message)
	def get_settings(lock=False):
		events.append(('settings', lock))
		return settings
	def get_doc(doctype, name, for_update=False):
		assert doctype == service.regional.DOCTYPE and name == record.name
		events.append(('record', for_update))
		return record
	def read_groups(*, lock=False):
		events.append(('groups', lock))
		return deepcopy(group_rows)
	def persist(doc):
		assert doc is record
		doc.modified = 'revision-2'
	saved = Mock(side_effect=persist)
	enqueue = Mock()
	monkeypatch.setattr(frappe, 'session', SimpleNamespace(user='manager'))
	monkeypatch.setattr(frappe, 'get_roles', lambda *_: roles)
	monkeypatch.setattr(frappe, 'only_for', only_for)
	monkeypatch.setattr(frappe, 'throw', throw)
	monkeypatch.setattr(frappe, 'get_doc', get_doc)
	monkeypatch.setattr(service, '_', lambda text: text)
	monkeypatch.setattr(service, 'now_datetime', lambda: '2026-11-27 12:00:00')
	monkeypatch.setattr(service.mx, '_settings', get_settings)
	monkeypatch.setattr(expense_group_mappings, 'read', read_groups)
	monkeypatch.setattr(service.mx, '_whole_source_allowed', lambda *a, **k: True)
	monkeypatch.setattr(links, 'mx_source_readable', lambda: True)
	monkeypatch.setattr(service.approvals, 'allowed_companies', lambda: companies)
	monkeypatch.setattr(service.regional, 'require_connection', lambda doc: None)
	monkeypatch.setattr(service.regional, 'current_candidate', lambda *a, **k: (deepcopy(raw), deepcopy(template)))
	monkeypatch.setattr(service.regional, 'save_record', saved)
	monkeypatch.setattr(service.automatic, 'configuration', lambda *_: {'version': 'current'})
	monkeypatch.setattr(expense_report_refresh, 'after_source_change', enqueue)
	key = service.regional.digest([record.name, 'regional-mx', 0])
	args = dict(key=key, source_key=record.name, expected_revision=record.modified,
		expected_fingerprint=service.contract.fingerprint(source, lines), note=' Verified approval amount ')
	return SimpleNamespace(**locals())


def test_confirmation_is_audited_atomic_and_idempotent(runtime):
	r = runtime
	originals = (r.record.source_json, r.record.template_json, r.record.allocations_json)
	assert service.confirm(**r.args) == {'ok': True}
	source, lines = service.regional.decode(r.record.facts_json)
	decision = source[service.contract.REVIEW]
	assert decision['note'] == 'Verified approval amount' and decision['reviewed_by'] == 'manager'
	assert decision['reviewed_at'] == '2026-11-27 12:00:00' and decision['amount'] == '6400.00'
	assert decision['evidence']['tables'][0]['total'] == '6401.00'
	assert 'total' in lines[0]['base_issues'] and r.record.state == 'stale'
	assert (r.record.source_json, r.record.template_json, r.record.allocations_json) == originals
	assert r.events[:3] == [('settings', True), ('groups', True), ('record', True)]
	assert service.confirm(**r.args) == {'ok': True}
	r.saved.assert_called_once()
	r.enqueue.assert_called_once()


@pytest.mark.parametrize('note', ['', '   ', None, 'x' * 1001])
def test_reason_is_required_and_bounded(runtime, note):
	with pytest.raises(frappe.ValidationError, match='note'):
		service.confirm(**{**runtime.args, 'note': note})
	runtime.saved.assert_not_called()


@pytest.mark.parametrize('case', ['role', 'company', 'whole_source', 'cn_source', 'invalid_key'])
def test_existing_role_and_whole_source_boundaries_cannot_be_bypassed(runtime, monkeypatch, case):
	r = runtime
	args = dict(r.args)
	if case == 'role':
		r.roles[:] = ['Accounts User']
	elif case == 'company':
		r.companies.clear()
	elif case == 'whole_source':
		monkeypatch.setattr(service.mx, '_whole_source_allowed', lambda *a, **k: False)
	elif case == 'cn_source':
		r.record.origin = 'cn'  # Source inspection still requires System Manager.
	else:
		args['key'] = 'f' * 64
	with pytest.raises(frappe.PermissionError):
		service.confirm(**args)
	r.saved.assert_not_called()
	r.enqueue.assert_not_called()


@pytest.mark.parametrize('case', ['revision', 'fingerprint', 'raw_changed', 'mapping_version', 'pending', 'split'])
def test_stale_or_ineligible_confirmation_never_writes(runtime, monkeypatch, case):
	r = runtime
	args = dict(r.args)
	if case == 'revision':
		args['expected_revision'] = 'old'
	elif case == 'fingerprint':
		args['expected_fingerprint'] = 'f' * 64
	elif case == 'raw_changed':
		r.raw['updated_at'] = '2026-11-27T12:01:00Z'
	elif case == 'mapping_version':
		monkeypatch.setattr(service.automatic, 'configuration', lambda *_: {'version': 'new'})
	elif case == 'pending':
		r.raw.update(status='RUNNING')
		r.raw['raw_payload']['status'] = 'RUNNING'
	else:
		r.entries.append({**r.entries[0], 'amount': '0'})
		r.record.allocations_json = json.dumps(r.entries)
	with pytest.raises(frappe.ValidationError):
		service.confirm(**args)
	r.saved.assert_not_called()
	r.enqueue.assert_not_called()


def test_concurrent_different_reason_cannot_overwrite_first_review(runtime):
	service.confirm(**runtime.args)
	with pytest.raises(frappe.ValidationError, match='allocation changed'):
		service.confirm(**{**runtime.args, 'note': 'Different decision'})
	runtime.saved.assert_called_once()


@pytest.mark.parametrize('kind', ['entity', 'organization'])
def test_group_assignment_after_opening_detail_revokes_finance_confirmation(runtime, kind):
	r = runtime
	assert service.preview(r.record, {'allocation_modified': r.record.modified})['can_confirm']
	r.group_rows.append({'match_kind': kind, 'corp_id': r.record.corp_id if kind == 'organization' else '',
		'source_name': r.lines[0]['source_entity']})
	before = r.record.facts_json
	with pytest.raises(frappe.PermissionError):
		service.confirm(**r.args)
	assert r.record.facts_json == before
	r.saved.assert_not_called()
	r.enqueue.assert_not_called()


def test_group_scope_is_rechecked_even_for_an_identical_retry(runtime):
	r = runtime
	service.confirm(**r.args)
	r.group_rows.append({'match_kind': 'entity', 'source_name': r.lines[0]['source_entity']})
	with pytest.raises(frappe.PermissionError):
		service.confirm(**r.args)
	r.saved.assert_called_once()


@pytest.mark.parametrize('manager', [False, True])
def test_unnamed_group_uses_the_same_manager_boundary(runtime, manager):
	r = runtime
	r.raw['raw_payload']['formComponentValues'][0]['value'] = ''
	source, lines = service.regional.facts(r.raw, r.template, 'America/Mexico_City')
	r.entries[0].update(company='', region='pending', matched_company='', assignment_issue='unmapped_company')
	r.record.source_json = service.regional.encode(r.raw)
	r.record.facts_json = service.regional.fact_payload(source, lines)
	r.record.allocations_json = service.regional.encode(r.entries)
	args = {**r.args, 'key': service.regional.digest([r.record.name, 'regional-pending', 0]),
		'expected_fingerprint': service.contract.fingerprint(source, lines)}
	if manager:
		r.roles.append('System Manager')
		assert service.confirm(**args) == {'ok': True}
		r.saved.assert_called_once()
	else:
		with pytest.raises(frappe.PermissionError):
			service.confirm(**args)
		r.saved.assert_not_called()


@pytest.mark.parametrize('case', ['manager', 'other_corporation', 'other_kind', 'other_name'])
def test_group_guard_preserves_authorized_manager_and_unrelated_source_access(runtime, case):
	r = runtime
	rule = {'match_kind': 'entity', 'corp_id': '', 'source_name': r.lines[0]['source_entity']}
	if case == 'manager':
		r.roles.append('System Manager')
	elif case == 'other_corporation':
		rule.update(match_kind='organization', corp_id='different-corporation')
	elif case == 'other_kind':
		rule['match_kind'] = 'department'
	else:
		rule['source_name'] = 'Different source'
	r.group_rows.append(rule)
	assert service.confirm(**r.args) == {'ok': True}
	r.saved.assert_called_once()


def test_preview_requires_current_publication_and_never_publishes_notes(runtime):
	r = runtime
	assert service.preview(r.record, {'allocation_modified': 'old'}) is None
	result = service.preview(r.record, {'allocation_modified': r.record.modified})
	assert result['can_confirm'] and result['header_amount'] == '6400.00'
	assert 'form_fingerprint' not in result
	service.confirm(**r.args)
	result = service.preview(r.record, {'allocation_modified': r.record.modified})
	assert not result['can_confirm'] and result['review']['state'] == 'confirmed'
	assert result['review']['note'] == 'Verified approval amount'
	from budget.services import expense_management
	assert 'amount_reconciliation' not in expense_management.public({'amount_reconciliation': result})
	row = {'key': 'line', 'source_key': 'source', 'kind': 'purchase', 'status': 'approved',
		'source_category_key': 'category', 'currency': 'MXN', 'eligible': True, 'issues': [],
		'amounts': {'CNY': '1', 'MXN': '1'}, 'amount_reconciliation': result, 'amount_reviewed': True}
	packed = expense_report_store.pack(row)
	assert 'amount_reconciliation' not in packed['detail_json'] and 'Verified approval amount' not in packed['detail_json']
	assert expense_report_store.unpack(packed)['amount_reviewed'] is True


def test_api_uses_the_validated_confirmation_service(monkeypatch):
	confirm = Mock(return_value={'ok': True})
	monkeypatch.setattr(service, 'confirm', confirm)
	assert api.confirm_approval_amount('key', 'source', 'revision', 'fingerprint', 'reason') == {'ok': True}
	confirm.assert_called_once_with('key', 'source', 'revision', 'fingerprint', 'reason')
	assert frappe.allowed_http_methods_for_whitelisted_func[api.confirm_approval_amount] == ['POST']
