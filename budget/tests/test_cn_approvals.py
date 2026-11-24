"""Synthetic forms mirror the supplied schemas without retaining personal data."""
from copy import deepcopy
from types import SimpleNamespace

import frappe
import pytest

from budget.services import cn_approval_contract as contract, cn_approval_workbench as service
from budget.integrations import cn_approval_source as source
from budget.api import cn_approvals as api


def field(identity, value=None, kind="DDSelectField", name=""):
	return {"id": identity, "value": value, "componentType": kind, "name": name}


def sample(kind="operating", **changes):
	config = {"approval_type": kind, "corp_id": "corp-A", "process_code": "PROC-test"}
	if kind == "budget":
		fields = [field("DepartmentField-K0C3VCCH", "Organization A", "DepartmentField"),
			field("DDSelectField_1Y8RI4CCPWN40", "非生产no producción"),
			field("DDDateField_TFZ1WT5RP9C0", "2026-11-01", "DDDateField"),
			field("DDDateField_RG3T9I5UX1S0", "2026-11", "DDDateField"),
			field("DDSelectField_19YR4TJTCWIO0", "中国China"),
			field("MoneyField_1JA1U3NP3Q4G0", "100", "MoneyField"),
			field("MoneyField_NAO8CPC23000", None, "MoneyField"),
			field("TableField_1VIO6UO9ZJI80", [{"rowValue": [
				{"key": "row-note", "label": "说明", "value": "Template hint"},
				{"key": "row-item", "label": "明细项目Ítem detallado", "value": "Services"},
				{"key": "row-money", "label": "人民币金额Monto en yuanes chinos", "value": "100"}]}], "TableField", "人资预算")]
	else:
		fields = [field("CascadeField_G8SEQLCWG1S0", "Organization A", "CascadeField"),
			field("MoneyField_1FPZGEQOZ4M80", "100", "MoneyField"),
			field("DDSelectField_199XDJ3I2RMO0", "人民币RMB"),
			field("DDDateField_29I59O630HUS" if kind == "operating" else "DDDateField_B0K470VSL0O0", "2026-11-10", "DDDateField"),
			field("DDSelectField_1FEAISH1BZ4W0", "中国China"),
			field("TextField_HQJISZX4Y480", "800", "TextField"), field("TextField_SKUJ6DR8EFK0", "20", "TextField")]
		fields += ([field("DDSelectField_RIQQC9Q99KW0", "管理费用Gastos administrativos"),
			field("DDSelectField_1CHIGP4L45K00", "工资中国Salario en China")] if kind == "operating" else
			[field("DDSelectField_187IN92KF6O00", "服务类采购Compra De Servicios"),
			field("DDSelectField_1XAH98VL8H0G0", "服务采购类报销")])
	row = {"corp_id": "corp-A", "process_code": "PROC-test", "process_instance_id": "instance-1", "business_id": "approval-1",
		"status": "COMPLETED", "result": "agree", "form_component_values": fields, "create_time": "2026-10-28T12:00:00Z", **changes}
	return row, config


@pytest.fixture(autouse=True)
def runtime(monkeypatch):
	monkeypatch.setattr(service.settings, "read_saved", lambda: None)
	monkeypatch.setattr(service.settings, "can_manage", lambda *a: False)
	from deeplinkerp_branding.services import operating_expense_contract
	monkeypatch.setattr(operating_expense_contract, "_", lambda text: text)
	monkeypatch.setattr(source, "_", lambda text: text)
	monkeypatch.setattr(service, "_", lambda text: text)
	monkeypatch.setattr(frappe, "throw", lambda message, *a: (_ for _ in ()).throw(ValueError(message)))
	monkeypatch.setattr(frappe.utils, "today", lambda: "2026-11-20")
	monkeypatch.setattr(frappe.utils, "now", lambda: "2026-11-20 12:00:00")


@pytest.mark.parametrize("kind", contract.KINDS)
def test_three_supplied_schema_families_share_header_identity_but_not_totals(kind):
	row, config = sample(kind)
	item = contract.project(row, config)
	assert item["kind"] == kind and item["amount"] == "100.00" and item["currency"] == "CNY"
	assert item["month"] == "2026-11" and item["status"] == "approved" and not item["issues"]
	assert len(item["key"]) == 64 and item["entity"] == "Organization A"
	if kind == "budget":
		assert item["detail_total"] == "100.00" and item["budget_snapshot"] == {"budget": None, "used": None}
	else:
		assert item["detail_total"] is None and item["budget_snapshot"] == {"budget": "800.00", "used": "20.00"}


@pytest.mark.parametrize("kind", contract.KINDS)
@pytest.mark.parametrize("region", ["中国", "中国China", "China", "CN", "墨西哥Mexico", "", None, "other"])
def test_execution_region_does_not_change_inclusion_or_projection(kind, region):
	row, config = sample(kind)
	expected = contract.project(row, config)
	region_id = "DDSelectField_19YR4TJTCWIO0" if kind == "budget" else "DDSelectField_1FEAISH1BZ4W0"
	next(f for f in row["form_component_values"] if f["id"] == region_id)["value"] = region
	assert contract.project(row, config) == expected
	row["form_component_values"] = [f for f in row["form_component_values"] if f["id"] != region_id]
	assert contract.project(row, config) == expected


@pytest.mark.parametrize("kind", contract.KINDS)
@pytest.mark.parametrize("changes", [{"corp_id": "foreign"}, {"process_code": "PROC-other"}])
def test_region_independent_inclusion_still_requires_configured_identity(kind, changes):
	row, config = sample(kind, **changes)
	with pytest.raises(ValueError, match="Unexpected CN approval identity"):
		contract.project(row, config)


def test_budget_duplicate_blank_total_is_ignored_but_two_populated_totals_are_ambiguous():
	row, config = sample("budget")
	row["form_component_values"][5]["value"] = "0"
	assert contract.project(row, config)["amount"] == "0.00"
	row["form_component_values"][6]["value"] = "100"
	result = contract.project(row, config)
	assert result["amount"] is None and "structure" in result["issues"]


@pytest.mark.parametrize("value", [None, "", "invalid", "NaN", "-1", "99999999999999"])
def test_invalid_amount_is_not_silently_converted_to_zero(value):
	row, config = sample()
	row["form_component_values"][1]["value"] = value
	item = contract.project(row, config)
	assert item["amount"] is None and "amount" in item["issues"]


@pytest.mark.parametrize("status,result,deleted,expected", [("COMPLETED", "refuse", None, "rejected"),
	("RUNNING", "", None, "pending"), ("TERMINATED", "agree", None, "terminated"),
	("COMPLETED", "agree", "2026-11-01", "withdrawn"), ("CANCELED", "", None, "withdrawn")])
def test_inactive_approvals_remain_visible_with_their_actual_state(status,result,deleted,expected):
	row, config = sample(status=status, result=result, deleted_at=deleted)
	assert contract.project(row, config)["status"] == expected


def test_operating_allocation_checks_header_without_adding_it_twice():
	row, config = sample()
	row["form_component_values"].append(field("TableField_1T6MS9XEQUE80", [
		{"rowValue": [{"label": "部门Departamento", "value": "A"}, {"label": "金额（元）Monto (yuan)", "value": "60"}]},
		{"rowValue": [{"label": "部门Departamento", "value": "B"}, {"label": "金额（元）Monto (yuan)", "value": "50"}]}], "TableField"))
	item = contract.project(row, config)
	assert item["amount"] == "100.00" and item["detail_total"] == "110.00"
	assert "detail_mismatch" in item["issues"]
	assert service.aggregate([item])["cards"][1]["totals"][0]["amount"] == "100.00"


def test_purchase_supporting_items_are_not_added_to_header():
	row, config = sample("purchase")
	row["form_component_values"].append(field("TableField_119WFD19L8R40", [
		{"rowValue": [{"label": "金额", "value": "300"}]}], "TableField", "需求明细"))
	item = contract.project(row, config)
	assert item["amount"] == "100.00" and item["detail_total"] is None
	assert item["tables"][0]["is_amount"] is False and "detail_mismatch" not in item["issues"]


def test_missing_period_is_only_reviewable_and_does_not_enter_monthly_totals():
	row, config = sample()
	row["form_component_values"][3]["value"] = "invalid"
	item = contract.project(row, config)
	assert not service.select([item], service.filters())
	assert service.select([item], service.filters({"review": 1})) == [item]
	result = service.aggregate([item])
	assert result["months"] == [] and result["cards"][1]["totals"] == []


def test_currency_totals_remain_separate_and_incomplete_amounts_are_not_partial_totals():
	row, config = sample();item = contract.project(row, config)
	rows = [item, {**item, "currency": "USD", "amount": "3.00"}, {**item, "amount": None}]
	totals = service.money_totals(rows)
	assert totals == [{"currency": "CNY", "amount": None, "known_amount": "100.00", "incomplete": 1},
		{"currency": "USD", "amount": "3.00", "known_amount": "3.00", "incomplete": 0}]


def test_company_authorization_applies_to_lists_details_and_every_total_without_mutating_cache():
	row, config = sample();item = contract.project(row, config)
	rows = [item, {**item, "entity": "Allowed Co"}, {**item, "entity": "Denied Co"}]
	context = {"manager": False, "companies": {"Allowed Co"}, "known_companies": {"Allowed Co", "Denied Co"}, "aliases": {("corp-A", "Denied Co"): "Denied Co"}}
	assert [r["entity"] for r in service.authorized(rows, context)] == ["Allowed Co"]
	assert "company" not in item and "organization" not in item["issues"]
	visible = service.authorized(rows, {**context, "manager": True})
	assert len(visible) == 2 and "organization" in visible[0]["issues"]


@pytest.mark.parametrize("changes", [{"kind": "expense"}, {"from_month": "2026-99"},
	{"from_month": "2026-12", "to_month": "2026-11"}, {"status": "all;drop"}, {"url": "http://evil.test"}])
def test_invalid_filters_are_rejected(changes):
	with pytest.raises(ValueError, match="filters"):
		service.filters(changes)


@pytest.mark.parametrize("method,kwargs", [(api.get_data, {}), (api.refresh_data, {}), (api.enqueue_sync, {}), (api.get_sync_status, {}), (api.get_detail, {"key": "a"*64}), (api.export_data, {})])
def test_api_checks_roles_before_reading_any_approval(monkeypatch, method, kwargs):
	monkeypatch.setattr(frappe, "only_for", lambda *a: (_ for _ in ()).throw(PermissionError()))
	monkeypatch.setattr(source, "get_snapshot", lambda **kw: pytest.fail("unauthorized read"))
	with pytest.raises(PermissionError): method(**kwargs)


@pytest.mark.parametrize("kind", contract.KINDS)
def test_full_snapshot_reads_all_pages_including_foreign_and_missing_regions(kind):
	row, config = sample(kind);calls = []
	region_id = "DDSelectField_19YR4TJTCWIO0" if kind == "budget" else "DDSelectField_1FEAISH1BZ4W0"
	def read(url, corp, code, cursor=None):
		calls.append(cursor)
		assert (url, corp, code) == ("url", config["corp_id"], config["process_code"])
		item = deepcopy(row)
		item["process_instance_id"] = cursor or "first"
		next(f for f in item["form_component_values"] if f["id"] == region_id)["value"] = None if cursor else "墨西哥Mexico"
		return [item], {"syncUntil": "fixed", "hasMore": cursor is None, "nextCursor": "second"}
	result = source.load_snapshot("url", [config], http=SimpleNamespace(read_template_page=read))
	assert calls == [None, "second"] and len(result["rows"]) == 2


@pytest.mark.parametrize("failure", ["duplicate", "snapshot", "cursor", "identity"])
def test_partial_or_inconsistent_snapshot_cannot_become_a_success(failure):
	row, config = sample();calls = []
	def read(url, corp, code, cursor=None):
		calls.append(cursor)
		item = {**row, "process_instance_id": "first" if failure == "duplicate" else str(len(calls))}
		if failure == "identity": item["corp_id"] = "foreign"
		return [item], {"syncUntil": str(len(calls)) if failure == "snapshot" else "fixed",
			"hasMore": True, "nextCursor": "repeat" if failure == "cursor" else str(len(calls))}
	with pytest.raises(ValueError):
		source.load_snapshot("url", [config], http=SimpleNamespace(read_template_page=read))


def test_snapshot_size_limit_fails_instead_of_showing_partial_statistics(monkeypatch):
	row, config = sample()
	monkeypatch.setattr(source, "MAX_ROWS", 1)
	http = SimpleNamespace(read_template_page=lambda *a, **kw: ([row, {**row, "process_instance_id": "other"}], {"syncUntil": "fixed", "hasMore": False}))
	with pytest.raises(source.ApprovalScanLimitExceeded, match="row or page limit"):
		source.load_snapshot("url", [config], http=http)


def test_slow_eleven_template_scan_completes_beyond_the_old_75_second_limit():
	row, config = sample()
	clock = SimpleNamespace(now=0)
	templates = [{**config, "process_code": f"PROC-{index}"} for index in range(11)]
	progress = []
	def read(url, corp, code, cursor=None):
		clock.now += 4.5
		item = {**row, "process_code": code, "process_instance_id": f"{code}-{cursor or 'first'}"}
		return [item], {"syncUntil": "fixed", "hasMore": cursor is None and code != "PROC-10",
			"nextCursor": "second"}
	result = source.load_snapshot("url", templates, http=SimpleNamespace(read_template_page=read),
		clock=lambda: clock.now, progress=lambda **values: progress.append(values))
	assert clock.now == 94.5
	assert len(result["rows"]) == 21
	assert progress[-1] == {"completed_templates": 11, "page_count": 21}


@pytest.mark.parametrize("has_more", [False, True])
def test_scan_deadline_still_rejects_late_pages_including_the_final_page(has_more):
	row, config = sample()
	clock = SimpleNamespace(now=0)
	def read(*args, **kwargs):
		clock.now = source.SCAN_TIMEOUT
		return [row], {"syncUntil": "fixed", "hasMore": has_more, "nextCursor": "second"}
	with pytest.raises(source.ApprovalScanTimeout):
		source.load_snapshot("url", [config], http=SimpleNamespace(read_template_page=read), clock=lambda: clock.now)


def test_scan_page_limit_is_independent_of_the_larger_time_budget(monkeypatch):
	row, config = sample()
	monkeypatch.setattr(source, "MAX_PAGES", 1)
	calls = []
	def read(*args, **kwargs):
		calls.append(kwargs)
		return [row], {"syncUntil": "fixed", "hasMore": True, "nextCursor": "second"}
	with pytest.raises(source.ApprovalScanLimitExceeded):
		source.load_snapshot("url", [config], http=SimpleNamespace(read_template_page=read))
	assert len(calls) == 1


@pytest.mark.parametrize('configured', [
	[{'approval_type':'budget','corp_id':'foreign','process_code':'PROC-test'}],
	[{'approval_type':'operating','corp_id':'corp-A','process_code':'PROC-test','field_mapping':'invalid'}],
	[{'approval_type':'operating','corp_id':'corp-A','process_code':'PROC-test','classification_fields':'[["duplicate"],["duplicate"]]'}],
	[{'approval_type':'expense','corp_id':'corp-A','process_code':'PROC-test'}],
])
def test_template_configuration_rejects_foreign_scope_and_invalid_fields(monkeypatch, configured):
	monkeypatch.setattr(source,'client',lambda:SimpleNamespace(configuration=lambda:('url',['corp-A'])))
	monkeypatch.setattr(frappe,'conf',{'cn_approval_templates':configured})
	with pytest.raises(ValueError,match='templates'):
		source.configuration()


def test_refresh_enqueues_without_loading_upstream_and_returns_previous_snapshot(monkeypatch):
	from budget.services import cn_approval_sync as sync
	previous={'rows':['existing']};requests=[]
	monkeypatch.setattr(sync,'request_sync',lambda:requests.append(True))
	monkeypatch.setattr(sync,'read_snapshot',lambda:previous)
	monkeypatch.setattr(source,'load_snapshot',lambda *a,**kw:pytest.fail('upstream read during page request'))
	assert source.get_snapshot() is previous
	assert source.get_snapshot(refresh=True) is previous
	assert requests==[True]


def test_detail_cannot_reveal_a_row_outside_permission_scope(monkeypatch):
	row,config=sample();item=contract.project(row,config)
	monkeypatch.setattr(service,'load',lambda:({},[],{}))
	with pytest.raises(ValueError,match='permission'):
		service.detail(item['key'])


def test_export_keeps_untrusted_spreadsheet_cells_literal(monkeypatch):
	import base64
	from io import BytesIO
	import openpyxl
	monkeypatch.setattr(api,'_',lambda s:s)
	monkeypatch.setattr(frappe,'get_system_settings',lambda key:{'date_format':'yyyy-mm-dd','time_format':'HH:mm:ss'}.get(key))
	monkeypatch.setattr(frappe,'only_for',lambda *a:None)
	row,config=sample();item=contract.project(row,config)
	item.update(number='=HYPERLINK("https://example.test")',company='<b>=1+1</b>')
	monkeypatch.setattr(service,'load',lambda:({},[item],{}))
	result=api.export_data()
	workbook=openpyxl.load_workbook(BytesIO(base64.b64decode(result['content'])))
	cell=workbook.active['A2']
	assert cell.data_type=='s' and cell.value.startswith("'=HYPERLINK")
	assert workbook.active['I2'].value==100 and workbook.active['I2'].data_type=='n'
	assert workbook.active['D2'].value=='<b>=1+1</b>' and workbook.active['D2'].data_type=='s'


def test_live_bilingual_dollar_and_social_insurance_total_fields():
	row,config=sample()
	row['form_component_values'][2]['value']='美元Dólar'
	assert contract.project(row,config)['currency']=='USD'
	row['form_component_values'][2]['value']='人民币RMB'
	row['form_component_values'].append(field('TableField_G2ELEALN0S80',[{'rowValue':[
		{'label':'社保+公积金（公司）SS + Vivienda (Emp.)','value':'70'},
		{'label':'社保+公积金（个人）SS + Vivienda (Pers.)','value':'30'},
		{'label':'总金额（元）Importe Total (CNY)','value':'100'}]}],'TableField'))
	item=contract.project(row,config)
	assert item['detail_total']=='100.00' and not item['issues']


def test_cny_allocation_is_not_compared_with_foreign_currency_header():
	row,config=sample();row['form_component_values'][2]['value']='美元Dólar'
	row['form_component_values'].append(field('TableField_1T6MS9XEQUE80',[{'rowValue':[
		{'label':'金额（元）Monto (yuan)','value':'700'}]}],'TableField'))
	item=contract.project(row,config)
	assert item['detail_currency']=='CNY' and item['currency']=='USD'
	assert 'detail_currency' in item['issues'] and 'detail_mismatch' not in item['issues']


def test_purchase_unit_and_total_columns_do_not_make_header_ambiguous():
	row,config=sample('purchase')
	row['form_component_values'].append(field('TableField_support',[{'rowValue':[
		{'label':'Monto unitario','value':'10'},{'label':'Monto total','value':'100'}]}],'TableField'))
	item=contract.project(row,config)
	assert item['detail_total'] is None and not item['issues']


@pytest.mark.parametrize("kind", contract.KINDS)
@pytest.mark.parametrize("region", ["中国China", "墨西哥Mexico", ""])
def test_saved_region_mapping_does_not_filter_approvals(kind, region):
	import json
	row, config = sample(kind)
	config["field_mapping"] = json.dumps({"region": ["custom-region"]})
	row["form_component_values"].append(field("custom-region", region, name="地区Region"))
	item = contract.project(row, config)
	assert item["amount"] == "100.00" and not item["issues"]


@pytest.mark.parametrize("kind", contract.KINDS)
def test_missing_ambiguous_or_mistyped_region_mappings_do_not_raise_review_issues(kind):
	import json
	row, config = sample(kind)
	config["field_mapping"] = json.dumps({"region": ["new-region-1", "new-region-2"]})
	assert not contract.project(row, config)["issues"]
	row["form_component_values"].extend([
		field("new-region-1", "中国China"), field("new-region-2", "墨西哥Mexico")])
	assert not contract.project(row, config)["issues"]
	row["form_component_values"][-1]["componentType"] = "TextField"
	assert not contract.project(row, config)["issues"]


@pytest.mark.parametrize("kind", ["operating", "purchase"])
def test_other_field_overrides_still_apply_when_region_is_ignored(kind):
	import json
	row, config = sample(kind)
	config["field_mapping"] = json.dumps({"amount": ["custom-amount"], "region": ["missing-region"]})
	row["form_component_values"].append(field("custom-amount", "123.45", "MoneyField"))
	item = contract.project(row, config)
	assert item["amount"] == "123.45" and not item["issues"]


def test_restricted_manager_cannot_reclassify_known_company_as_unmapped(monkeypatch):
	import base64
	from io import BytesIO
	import openpyxl
	from budget.services import ledger_sync_settings
	monkeypatch.setattr(frappe, "only_for", lambda *args: None)
	monkeypatch.setattr(frappe, "session", SimpleNamespace(user="restricted-manager"))
	monkeypatch.setattr(frappe, "get_roles", lambda: ["System Manager"])
	monkeypatch.setattr(frappe, "conf", {})
	monkeypatch.setattr(frappe, "get_list", lambda *args, **kw: ["Allowed Co"])
	monkeypatch.setattr(frappe, "get_all", lambda *args, **kw: ["Allowed Co", "Denied Co", "Group Co"])
	monkeypatch.setattr(ledger_sync_settings, "current_source", lambda: {"organization_mappings": [], "corp_id": "corp-A"})
	monkeypatch.setattr(api, "_", lambda text: text)
	monkeypatch.setattr(frappe, "get_system_settings", lambda key: {"date_format": "yyyy-mm-dd", "time_format": "HH:mm:ss"}.get(key))
	rows = []
	for index, company in enumerate(("Allowed Co", "Denied Co", "Unknown source", "Group Co")):
		row, config = sample(process_instance_id=f"instance-{index}")
		rows.append({**contract.project(row, config), "entity": company})
	monkeypatch.setattr(source, "get_snapshot", lambda **kw: {"rows": rows, "fetched_at": "2026-11-20"})
	result = api.get_data()
	assert result["total"] == 2
	assert result["options"]["entities"] == ["Allowed Co", "Unknown source"]
	assert result["summary"]["cards"][1]["totals"][0]["amount"] == "200.00"
	assert "organization" in next(row for row in result["rows"] if row["entity"] == "Unknown source")["issues"]
	for item in (rows[1], rows[3]):
		with pytest.raises(ValueError, match="permission"):
			api.get_detail(item["key"])
	workbook = openpyxl.load_workbook(BytesIO(base64.b64decode(api.export_data()["content"])))
	assert set(row[2].value for row in list(workbook.active.rows)[1:]) == {"Allowed Co", "Unknown source"}
	assert all("company" not in row and "organization" not in row["issues"] for row in rows)
