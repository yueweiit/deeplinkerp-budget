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


@pytest.mark.parametrize("region", ["墨西哥Mexico", "", None, "other"])
def test_only_explicit_china_region_is_included(region):
	row, config = sample()
	next(f for f in row["form_component_values"] if f["id"] == "DDSelectField_1FEAISH1BZ4W0")["value"] = region
	assert contract.project(row, config) is None


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


@pytest.mark.parametrize("method,kwargs", [(api.get_data, {}), (api.refresh_data, {}), (api.get_detail, {"key": "a"*64}), (api.export_data, {})])
def test_api_checks_roles_before_reading_any_approval(monkeypatch, method, kwargs):
	monkeypatch.setattr(frappe, "only_for", lambda *a: (_ for _ in ()).throw(PermissionError()))
	monkeypatch.setattr(source, "get_snapshot", lambda **kw: pytest.fail("unauthorized read"))
	with pytest.raises(PermissionError): method(**kwargs)


def test_full_snapshot_reuses_template_client_and_checks_all_pages(monkeypatch):
	row, config = sample();calls = []
	def read(url, corp, code, cursor=None):
		calls.append(cursor)
		return [{**row, "process_instance_id": cursor or "first"}], {"syncUntil": "fixed", "hasMore": cursor is None, "nextCursor": "second"}
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
	with pytest.raises(ValueError, match="too large"):
		source.load_snapshot("url", [config], http=http)


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


def test_failed_refresh_preserves_previous_complete_snapshot(monkeypatch):
	previous={'rows':['existing']};writes=[]
	monkeypatch.setattr(source,'configuration',lambda:('url',[]))
	monkeypatch.setattr(frappe,'cache',SimpleNamespace(get_value=lambda key,**kw:previous,make_key=lambda key:key,lock=lambda *a,**kw:SimpleNamespace(acquire=lambda **kw:True,release=lambda:None),set_value=lambda *a,**kw:writes.append(a)))
	monkeypatch.setattr(source,'load_snapshot',lambda *a:(_ for _ in ()).throw(RuntimeError('source unavailable')))
	assert source.get_snapshot() is previous
	with pytest.raises(RuntimeError):source.get_snapshot(refresh=True)
	assert writes==[]


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
@pytest.mark.parametrize("region,included", [("中国China", True), ("墨西哥Mexico", False), ("", False)])
def test_configured_region_binding_takes_precedence_over_legacy_china_field(kind, region, included):
	import json
	row, config = sample(kind)
	config["field_mapping"] = json.dumps({"region": ["custom-region"]})
	row["form_component_values"].append(field("custom-region", region, name="地区Region"))
	assert (contract.project(row, config) is not None) == included


@pytest.mark.parametrize("kind", contract.KINDS)
def test_region_override_does_not_fall_back_when_missing_or_ambiguous(kind):
	import json
	row, config = sample(kind)
	config["field_mapping"] = json.dumps({"region": ["new-region-1", "new-region-2"]})
	assert contract.project(row, config) is None
	row["form_component_values"].extend([
		field("new-region-1", "中国China"), field("new-region-2", "墨西哥Mexico")])
	assert contract.project(row, config) is None


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
