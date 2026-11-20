"""Management decisions must reconcile across companies, versions and drilldowns."""
from copy import deepcopy
from decimal import Decimal
import hashlib

import frappe
import pytest

from budget.services import cn_approval_management as management
from budget.services import cn_approval_management_rules as rules
from budget.services import cn_approval_contract as contract


def context(manager=True, companies=None):
	return {"manager": manager, "companies": set(companies or ["A", "B", "Empty"]),
		"known_companies": {"A", "B", "Empty"}, "aliases": {("corp", "Dept A"): "A", ("corp", "Dept B"): "B"}}


def approval(name, kind="operating", value="100.00", company="A", month="2026-11", **kwargs):
	key = hashlib.sha256(name.encode()).hexdigest()
	return {"key": key, "corp_id": "corp", "process_code": "PROC-test", "process_instance_id": name,
		"number": name, "applicant": "Applicant", "entity": company, "entity_id": "", "company": company,
		"kind": kind, "month": month, "application_date": f"{month}-10", "amount": value, "currency": "CNY",
		"status": "approved", "approved_at": "2026-11-10T12:00:00+00:00", "category": "管理费用Gastos administrativos",
		"classification": ["管理费用Gastos administrativos", "工资中国Salario en China"], "issues": [], "tables": [],
		"facts": [], "attachments": [], "budget_snapshot": {}, "description": "", **kwargs}


def budget_parts(values):
	return [{"group": "hr", "label": "HR budget", "is_amount": True, "rows": [
		{"label": "工资Salario", "amount": value, "cells": []} for value in values]}]


def projected(rows, ctx=None, f=None):
	ctx = ctx or context()
	lines = management.analytical_lines(rows, ctx, rules.defaults())
	for line in lines:
		line["converted_amount"] = line["amount"]
	return lines, management.aggregate(lines, ctx, rules.defaults(), f or {"company": ""})


@pytest.fixture(autouse=True)
def runtime(monkeypatch):
	monkeypatch.setattr(management, "_", lambda message: message)
	monkeypatch.setattr(frappe, "throw", lambda message, *a: (_ for _ in ()).throw(ValueError(message)))


def test_latest_budget_is_per_company_month_not_department_or_updated_time():
	old = approval("old", "budget", "500.00", "Dept A", approved_at="2026-11-01T00:00:00+00:00", updated_at="2099-01-01")
	new = approval("new", "budget", "200.00", "A", approved_at="2026-11-05T00:00:00+00:00", tables=budget_parts(["200.00"]))
	lines, result = projected([old, new, approval("op", value="50.00"), approval("po", "purchase", "30.00")])
	assert result["totals"]["budget"] == "200.00"
	assert result["totals"]["approved"] == "80.00"
	assert result["totals"]["balance"] == "120.00"
	assert result["totals"]["execution_rate"] == "40.00"
	assert next(row for row in lines if row["number"] == "old")["budget_state"] == "superseded"
	assert result["totals"]["superseded_count"] == 1


@pytest.mark.parametrize("time", [None, "2026-11-10T12:00:00+00:00"])
def test_missing_or_tied_completion_times_do_not_select_an_arbitrary_budget(time):
	_, result = projected([approval("one", "budget"), approval("two", "budget", approved_at=time)])
	assert result["totals"]["budget"] is None
	assert result["totals"]["balance"] is None
	assert "incomplete" in result["totals"]["flags"]


def test_pending_or_withdrawn_budget_never_replaces_the_last_approved_budget():
	_, result = projected([approval("one", "budget"), approval("two", "budget", "900.00", status="pending"),
		approval("three", "budget", "800.00", status="withdrawn")])
	assert result["totals"]["budget"] == "100.00"
	assert result["totals"]["pending"] == "0.00"


def test_all_purchase_types_occupy_budget_and_pending_amount_is_only_a_scenario():
	_, result = projected([approval("b", "budget"), approval("op", value="20.00"),
		approval("investment", "purchase", "70.00", classification=["投资采购Compra de inversión"]),
		approval("waiting", "purchase", "30.00", status="pending"), approval("refused", "purchase", "999.00", status="rejected")])
	total = result["totals"]
	assert (total["approved"], total["pending"], total["balance"], total["forecast_balance"]) == ("90.00", "30.00", "10.00", "-20.00")
	assert "forecast_over" in total["flags"]


def test_budget_details_replace_header_for_analysis_and_never_add_it_twice():
	lines, result = projected([approval("b", "budget", "100.00", tables=budget_parts(["60.00", "40.00"]))])
	assert len(lines) == 2
	assert result["totals"]["budget"] == "100.00"
	assert result["totals"]["budget_count"] == 1
	assert result["categories"][0]["management_category"] == "salary"
	assert result["categories"][0]["budget"] == "100.00"


def test_mismatching_budget_detail_preserves_header_but_cannot_invent_category_amounts():
	lines, result = projected([approval("b", "budget", "100.00", tables=budget_parts(["60.00", "45.00"]))])
	assert len(lines) == 1 and lines[0]["amount"] == "100.00"
	assert lines[0]["management_category"] == ""
	assert "budget_detail" in lines[0]["issues"]
	assert result["totals"]["budget"] == "100.00"


def allocation_table(values):
	return [{"label": "Allocation", "is_amount": True, "rows": [{"label": company, "amount": value,
		"cells": [{"label": "部门Departamento", "value": company}]} for company, value in values]}]


def test_allocation_permission_applies_before_every_company_and_total_without_mutating_source():
	row = approval("op", value="100.00", company="Header org", tables=allocation_table([("Dept A", "60.00"), ("Dept B", "40.00")]))
	original = deepcopy(row)
	lines, result = projected([row], context(False, ["A"]))
	assert len(lines) == 1 and lines[0]["company"] == "A"
	assert result["totals"]["approved"] == "60.00"
	assert all(group["company"] == "A" for group in result["companies"])
	assert row == original


def test_unreconciled_allocation_cannot_assign_the_header_total_to_a_company():
	row = approval("op", value="100.00", tables=allocation_table([("Dept A", "60.00"), ("Dept B", "50.00")]))
	lines, result = projected([row])
	assert len(lines) == 1 and lines[0]["company"] == ""
	assert result["quality"]["unassigned"] == 1
	assert "allocation" in lines[0]["issues"]
	assert projected([row], context(False, ["A"]))[0] == []


def test_zero_budget_is_distinct_from_no_budget_and_empty_companies_remain_available():
	_, result = projected([approval("zero", "budget", "0.00"), approval("op", value="10.00"), approval("other", company="B")])
	companies = {row["company"]: row for row in result["companies"]}
	assert companies["A"]["budget"] == "0.00" and companies["A"]["balance"] == "-10.00"
	assert companies["A"]["execution_rate"] is None
	assert companies["B"]["budget"] is None and companies["B"]["balance"] is None
	assert companies["B"]["flags"] == ["no_budget"]
	assert companies["Empty"]["count"] == 0


def test_other_month_or_company_surplus_cannot_mask_missing_budget():
	_, result = projected([approval("b", "budget", "1000.00"), approval("op", "operating", "50.00", month="2026-12"),
		approval("other", "operating", "10.00", company="B")])
	assert result["totals"]["balance"] is None
	assert next(row for row in result["companies"] if row["company"] == "A")["balance"] is None


def test_exact_category_mapping_does_not_guess_from_substrings_and_preserves_source_path():
	config = rules.clean({"category_mappings": [{"kind": "purchase", "source_path": '["Services","Custom"]', "category": "it"}]})
	assert rules.classify("purchase", ["Services", "Custom"], config) == "it"
	assert rules.classify("operating", ["Services", "Custom"], config) == ""
	assert rules.classify("operating", ["工资中国Salario en China", "Unknown child"], config) == ""
	with pytest.raises(ValueError):
		rules.clean({"category_mappings": config["category_mappings"] * 2})


def test_conversion_cent_allocation_reconciles_header_and_filtered_subreports():
	lines = management.analytical_lines([approval("b", "budget", "0.03", tables=budget_parts(["0.01", "0.01", "0.01"]))], context(), rules.defaults())
	converted = management.convert_lines(lines, "USD", loader=lambda code, day: Decimal(2))
	assert sum(Decimal(row["converted_amount"]) for row in converted) == Decimal("0.02")
	for line in lines:
		assert management.convert_lines([line], "USD", loader=lambda code, day: Decimal(2))[0]["converted_amount"] == next(row["converted_amount"] for row in converted if row["key"] == line["key"])
	lines = management.analytical_lines([approval("many", "budget", "1.00", tables=budget_parts(["0.01"] * 100))], context(), rules.defaults())
	converted = management.convert_lines(lines, "USD", loader=lambda code, day: Decimal(2))
	assert min(Decimal(row["converted_amount"]) for row in converted) >= 0
	assert sum(Decimal(row["converted_amount"]) for row in converted) == Decimal("0.50")


def test_approval_completion_requires_a_source_instant_and_is_normalized_to_utc():
	assert contract.approval_finished_at({"finishTime": "2026-11-02T10:00:00+08:00"}) == "2026-11-02T02:00:00+00:00"
	assert contract.approval_finished_at({"updatedAt": "2026-11-02T10:00:00Z"}) is None
	assert contract.approval_finished_at({"finishTime": "2026-11-02 10:00:00"}) is None


def test_complete_budget_absent_category_is_zero_but_unknown_budget_is_not():
	rows = [approval('b', 'budget', tables=budget_parts(['100.00'])),
		approval('new', classification=['税费Impuestos'])]
	_, result = projected(rows)
	tax = next(row for row in result['category_months'] if row['management_category'] == 'tax')
	assert (tax['budget'], tax['balance']) == ('0.00', '-100.00')
	rows[0]['tables'] = []
	_, result = projected(rows)
	tax = next(row for row in result['category_months'] if row['management_category'] == 'tax')
	assert tax['budget'] is None and tax['balance'] is None


def test_unclassified_categories_cannot_offset_each_other_and_missing_other_company_is_not_hidden():
	_, result = projected([approval('b', 'budget'), approval('op', classification=['unknown'])])
	assert result['categories'][0]['balance'] is None
	_, result = projected([approval('b', 'budget', tables=budget_parts(['100.00'])), approval('op', company='B')])
	assert next(row for row in result['categories'] if row['management_category'] == 'salary')['balance'] is None
	assert result['months'][0]['balance'] is None


def test_pending_without_budget_is_not_normal_and_unassigned_filter_has_no_empty_companies():
	_, result = projected([approval('op', status='pending')])
	assert 'no_budget' in result['companies'][0]['flags']
	_, result = projected([approval('op', company='Unknown')], f={'company':'', 'unassigned':'1'})
	assert len(result['companies']) == 1


def test_project_drilldown_and_blank_project_are_exact():
	lines, _ = projected([approval('p1', 'purchase', facts=[{'label':'Project','value':'A'}]),
		approval('p2', 'purchase', facts=[{'label':'Project','value':'AB'}]), approval('p3', 'purchase')])
	f = {'from_month':'2026-11','to_month':'2026-11','company':'','project':'A'}
	assert [row['number'] for row in management.selected_lines(lines,f)] == ['p1']
	assert [row['number'] for row in management.selected_lines(lines,{**f,'project':'','no_project':'1'})] == ['p3']


def test_comparisons_are_closed_periods_and_do_not_invent_absent_baselines(monkeypatch):
	monkeypatch.setattr(frappe.utils,'today',lambda:'2026-11-20')
	lines,_=projected([approval('new',month='2026-10'),approval('old',value='40.00',month='2026-09')])
	f={'from_month':'2026-10','to_month':'2026-10','company':'','reporting_currency':'CNY'}
	result=management.comparisons(lines,f,{'has_snapshot':True})
	previous, year=result['items']
	assert (previous['change'],previous['percent']) == ('60.00','150.00')
	assert year['previous'] is None and year['change'] is None
	assert result['drivers'][0]['change']=='60.00'
	assert management.comparisons(lines,{**f,'to_month':'2026-11'}, {})['reason']=='open_period'
	assert management.comparisons(lines,f,{'missing_history_templates':1})['reason']=='incomplete'


def test_partial_company_detail_has_no_other_allocations_attachments_or_original_link(monkeypatch):
	from budget.integrations import cn_approval_history as history
	row=approval('allocated',value='100.00',company='Header',tables=allocation_table([('Dept A','60.00'),('Dept B','40.00')]),
		facts=[{'label':'Private source fact','value':'Private'}],attachments=[{'name':'Private attachment'}])
	ctx=context(False,['A']);line=management.analytical_lines([row],ctx,rules.defaults())[0]
	monkeypatch.setattr(management.approvals,'require_reader',lambda:None)
	monkeypatch.setattr(management.approvals,'access_context',lambda:ctx)
	monkeypatch.setattr(history,'read_snapshot',lambda:{'rows':[row]})
	monkeypatch.setattr(management.settings,'management_rules',rules.defaults)
	monkeypatch.setattr(management,'validate_filters',lambda f:f)
	result=management.detail(line['key'])
	assert result['amount']=='60.00' and result['converted_amount']=='60.00'
	assert not result['facts'] and not result['attachments'] and not result['approval_url']
	assert len(result['tables'])==1 and len(result['tables'][0]['rows'])==1
	assert 'Dept B' not in str(result) and 'allocation_amounts' not in result
	other=management.analytical_lines([row],context(),rules.defaults())[1]
	with pytest.raises(ValueError):management.detail(other['key'])


def test_report_and_export_share_calculation_and_preserve_literal_source_text(monkeypatch):
	import base64
	from io import BytesIO
	from openpyxl import load_workbook
	from budget.api import cn_approvals as api
	rows,result=projected([approval('b','budget',tables=budget_parts(['100.00']),company='=literal'),approval('op',company='=literal')],
		{**context(),'companies':{'=literal'},'known_companies':{'=literal'}})
	f={'company':'','from_month':'2026-11','to_month':'2026-11','reporting_currency':'CNY','management_category':''}
	monkeypatch.setattr(management,'query',lambda *a,**k:({'has_snapshot':True,'fetched_at':'now'},rows,{**context(),'companies':{'=literal'}},rules.defaults(),f))
	monkeypatch.setattr(management.settings,'can_manage',lambda *a:False)
	monkeypatch.setattr(api,'_',lambda x:x)
	monkeypatch.setattr(management.approvals,'require_reader',lambda:None)
	monkeypatch.setattr(frappe,'get_system_settings',lambda key:{'date_format':'yyyy-mm-dd','time_format':'HH:mm:ss'}.get(key))
	report=management.report_data('company');export=api.export_management_report('company')
	wb=load_workbook(BytesIO(base64.b64decode(export['content'])))
	data=list(wb.active.values)
	assert data[-1][0] in ('=literal',"'=literal") and wb.active.cell(len(data),1).data_type=='s'
	assert [Decimal(str(data[-1][i])) for i in (1,2,3,4)]==[Decimal('100'),Decimal('100'),Decimal('0'),Decimal('100')]
	assert report['rows'][0]['balance']=='0.00'


def test_empty_first_report_returns_sync_envelope_but_export_refuses(monkeypatch):
	from budget.api import cn_approvals as api
	f={'company':'','from_month':'2026-11','to_month':'2026-11','reporting_currency':'CNY','management_category':''}
	monkeypatch.setattr(management,'query',lambda *a,**k:({'has_snapshot':False,'sync':{'active':True}},[],context(),rules.defaults(),f))
	monkeypatch.setattr(management.settings,'can_manage',lambda *a:False)
	monkeypatch.setattr(management.approvals,'require_reader',lambda:None)
	monkeypatch.setattr(api,'_',lambda x:x)
	assert management.report_data('company')['sync']['active']
	with pytest.raises(ValueError):api.export_management_report('company')


@pytest.mark.parametrize('method,args',[('get_management',{}),('get_management_lines',{}),('get_management_detail',{'key':'a'*64}),('get_management_report',{}),('export_management_report',{}),('export_management_lines',{})])
def test_every_management_api_checks_reader_role_before_data_access(monkeypatch,method,args):
	from budget.api import cn_approvals as api
	def deny():raise PermissionError('reader role required')
	monkeypatch.setattr(management.approvals,'require_reader',deny)
	monkeypatch.setattr(management,'load_lines',lambda:pytest.fail('unauthorized data access'))
	with pytest.raises(PermissionError):getattr(api,method)(**args)


def test_query_rejects_unavailable_company_before_conversion(monkeypatch):
	lines,_=projected([approval('one')])
	monkeypatch.setattr(management,'validate_filters',lambda f:f)
	monkeypatch.setattr(management,'load_lines',lambda:({'rows':[]},lines,context(False,['A']),rules.defaults()))
	with pytest.raises(ValueError):management.query({'company':'B'})


def test_missing_retired_history_prevents_valid_budget_difference():
	lines,_=projected([approval('b','budget'),approval('op',value='20.00')])
	for row in lines:row['issues'].append('history')
	result=management.aggregate(lines,context(),rules.defaults(),{'company':''})
	assert result['totals']['balance'] is None and 'incomplete' in result['totals']['flags']


def test_historical_rate_failure_does_not_discard_current_period_amounts(monkeypatch):
	monkeypatch.setattr(frappe.utils,'today',lambda:'2026-11-20')
	lines,_=projected([approval('new',month='2026-10'),approval('old',month='2026-09')])
	original=management.convert_lines
	def convert(rows,target,**kwargs):
		if any(row['month']=='2026-09' for row in rows):raise frappe.ValidationError('Historical rate unavailable')
		return original(rows,target,**kwargs)
	monkeypatch.setattr(management,'convert_lines',convert)
	f={'company':'','from_month':'2026-10','to_month':'2026-10','reporting_currency':'CNY'}
	result=management.comparisons(lines,f,{'has_snapshot':True})
	assert result['items'][0]['current']=='100.00' and result['items'][0]['reason']=='exchange_rate'
	assert result['items'][0]['change'] is None


def report_runtime(monkeypatch, approvals):
	"""Exercise actual projection, filtering, FX, aggregation and XLSX export."""
	from budget.api import cn_approvals as api
	monkeypatch.setattr(frappe.utils, 'today', lambda: '2026-11-20')
	monkeypatch.setattr(management.approvals, 'require_reader', lambda: None)
	monkeypatch.setattr(management.settings, 'can_manage', lambda *a: False)
	monkeypatch.setattr(api, '_', lambda text: text)
	monkeypatch.setattr(frappe, 'get_system_settings', lambda key: {'date_format': 'yyyy-mm-dd', 'time_format': 'HH:mm:ss'}.get(key))
	lines = management.analytical_lines(approvals, context(), rules.defaults())
	monkeypatch.setattr(management, 'load_lines', lambda: ({'has_snapshot': True}, lines, context(), rules.defaults()))
	return {'company': 'A', 'from_month': '2026-11', 'to_month': '2026-11', 'reporting_currency': 'CNY'}


def exported_rows(report_id, filters):
	import base64
	from io import BytesIO
	from openpyxl import load_workbook
	from budget.api import cn_approvals as api
	result = api.export_management_report(report_id, filters)
	return list(load_workbook(BytesIO(base64.b64decode(result['content']))).active.values)


@pytest.mark.parametrize('unknown_spend', ['100.00', '1100.00'])
def test_unclassified_filter_preserves_amounts_but_never_invents_comparable_limits(monkeypatch, unknown_spend):
	tables = budget_parts(['1000.00'])
	tables[0]['rows'].append({'label': 'Unknown budget', 'amount': '1000.00', 'cells': []})
	f = report_runtime(monkeypatch, [approval('b', 'budget', '2000.00', tables=tables),
		approval('salary', value='1500.00'), approval('unknown', value=unknown_spend, classification=['Unknown expense'])])
	snapshot, rows, ctx, config, validated = management.query({**f, 'management_category': 'unclassified'})
	result = management.aggregate(rows, ctx, config, validated)
	for group in [result['totals'], *result['companies'], *result['months'], *result['company_months'], *result['categories']]:
		assert (group['budget'], group['approved']) == ('1000.00', unknown_spend)
		assert group['balance'] is None and group['forecast_balance'] is None and group['execution_rate'] is None
		assert group['flags'] == ['incomplete']
	for report_id in ('company', 'budget', 'trend'):
		report = management.report_data(report_id, validated)
		assert report['rows'][0]['balance'] is None and report['rows'][0]['flags'] == ['incomplete']
		data = exported_rows(report_id, validated)
		for key in ('balance', 'execution_rate'):
			index = next(i for i, column in enumerate(report['columns']) if column['fieldname'] == key)
			assert data[-1][index] == 'Not available'
	# A company-wide budget remains comparable, and known salary limits remain independent.
	assert management.report_data('company', f)['rows'][0]['balance'] == str(Decimal('500.00') - Decimal(unknown_spend))
	assert management.report_data('company', {**f, 'management_category': 'salary'})['rows'][0]['balance'] == '-500.00'


@pytest.mark.parametrize('report_id', ['purchase', 'hr'])
def test_report_and_export_ignore_out_of_scope_fx_failures_but_not_relevant_failures(monkeypatch, report_id):
	selected = approval('selected', 'purchase' if report_id == 'purchase' else 'operating')
	unrelated = approval('unrelated', value='9.00', currency='USD', classification=['税费Impuestos'])
	f = report_runtime(monkeypatch, [selected, unrelated])
	conversion = management.fx.Conversion
	def missing_rate(*args):
		raise frappe.ValidationError('Missing rate for test')
	monkeypatch.setattr(management.fx, 'Conversion', lambda target, loader=None: conversion(target, missing_rate))
	report = management.report_data(report_id, f)
	assert len(report['rows']) == 1 and report['rows'][0]['approved'] == '100.00'
	assert exported_rows(report_id, f)[-1][0] == 'A'
	# The same failure must remain visible when that currency belongs to this report.
	report_runtime(monkeypatch, [{**selected, 'currency': 'USD'}, unrelated])
	with pytest.raises(frappe.ValidationError, match='Missing rate'):
		management.report_data(report_id, f)
	with pytest.raises(frappe.ValidationError, match='Missing rate'):
		exported_rows(report_id, f)


def test_hr_scope_cannot_resurrect_a_superseded_budget(monkeypatch):
	latest = budget_parts(['200.00'])
	latest[0]['rows'][0]['label'] = '税费Impuestos'
	f = report_runtime(monkeypatch, [approval('old', 'budget', tables=budget_parts(['100.00']), approved_at='2026-11-01T00:00:00+00:00'),
		approval('latest', 'budget', '200.00', tables=latest), approval('salary', value='50.00')])
	row = management.report_data('hr', f)['rows'][0]
	assert row['budget'] == '0.00' and row['balance'] == '-50.00'
	assert row['superseded_count'] == 1
