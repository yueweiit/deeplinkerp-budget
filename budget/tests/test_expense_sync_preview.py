"""Unified previews read once and preserve date, identity and schema validation."""
import json
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import Mock

import frappe
import pytest

from budget.services import expense_sync_preview as s


@pytest.fixture
def preview(monkeypatch):
	row = dict(corp_id="corp", process_code="PROC-00000000-0000-0000-0000-000000000001",
		approval_type="operating", template_name="Test", application_fields="{}", expense_fields="{}")
	state = SimpleNamespace(row=row, raw=[{"process_instance_id": "a"}, {"process_instance_id": "b"}])
	state.read = Mock(return_value=(state.raw, {}))
	state.cn = Mock(return_value={"issues": []})
	state.mx = Mock(return_value=({"request_date": "2026-11-01"}, [{"base_issues": []}]))
	monkeypatch.setattr(s, "_", lambda text: text)
	monkeypatch.setattr(frappe, "throw", lambda text, *args: (_ for _ in ()).throw(frappe.ValidationError(text)))
	monkeypatch.setattr(s.mx.http, "configuration", lambda: ("https://oa.invalid", ["corp"]))
	monkeypatch.setattr(s.mx.http, "read_template_page", state.read)
	monkeypatch.setattr(s.contract, "project", state.cn)
	monkeypatch.setattr(s.mx.projection, "project", state.mx)
	return state


def test_both_adapters_use_one_bounded_sample_without_duplicate_counting_or_finance(preview):
	result = s.preview(preview.row, "America/Mexico_City", ["corp"], ("cn", "mx"))
	assert result == [{"rule": "approval", "count": 2, "issues": []}]
	preview.read.assert_called_once_with("https://oa.invalid", "corp", preview.row["process_code"], limit=3)
	assert preview.cn.call_count == preview.mx.call_count == 2
	assert all(call.kwargs == {"include_finance": False} for call in preview.mx.call_args_list)
	assert [call.args[0] for call in preview.cn.call_args_list] == [call.args[0] for call in preview.mx.call_args_list]


@pytest.mark.parametrize("value,issue", [(None, False), ("", False), ("invalid", True), ("2026-11-02", False)])
def test_absent_or_blank_application_date_can_use_creation_date(preview, value, issue):
	for row in preview.raw:
		row["form_component_values"] = [] if value is None else [
			{"id": "DDDateField_B0K470VSL0O0", "componentType": "DDDateField", "name": "申请日期", "value": value}]
	result = s.preview(preview.row, "America/Mexico_City", ["corp"], ("mx",))
	assert bool(result[0]["issues"]) == issue


@pytest.mark.parametrize("change", ["timezone", "corp", "mapping"])
def test_invalid_configuration_is_rejected_before_any_oa_read(preview, change):
	timezone = "America/Mexico_City"
	if change == "timezone": timezone = "invalid/timezone"
	elif change == "corp": preview.row["corp_id"] = "unauthorized"
	else: preview.row["expense_fields"] = '{"unknown": ["field"]}'
	with pytest.raises(frappe.ValidationError): s.preview(preview.row, timezone, ["corp"], ("mx",))
	preview.read.assert_not_called()


def test_amount_issues_are_visible_and_no_raw_source_is_returned(preview):
	preview.mx.return_value[1][0]["base_issues"] = ["amount", "total"]
	preview.raw[0]["private_data"] = "never-return-this"
	result = s.preview(preview.row, "America/Mexico_City", ["corp"], ("mx",))
	assert len(result[0]["issues"]) == 2
	assert "never-return-this" not in str(result)


def test_empty_sample_explains_disabled_first_workflow(preview):
	preview.read.return_value = ([], {})
	result = s.preview(preview.row, "America/Mexico_City", ["corp"], ("mx",))
	assert result[0]["count"] == 0
	assert result[0]["issues"] == ["No sample approvals found; save this template disabled first"]
	preview.mx.assert_not_called()


@pytest.mark.parametrize("processors", [("cn", "mx"), ("cn",)])
@pytest.mark.parametrize("fields,submitted,valid", [
	([], "2026-11-01", True),
	([], None, False),
	([{"name": "申请日期", "value": ""}], "2026-11-01", True),
	([{"name": "申请日期", "value": "invalid"}], "2026-11-01", False),
	([{"name": "申请日期", "value": "2026-11-01"},
	  {"name": "申请日期", "value": "2026-10-01"}], "2026-11-01", False),
])
def test_shared_preview_uses_effective_date_policy_without_hiding_other_issues(preview, processors, fields, submitted, valid):
	preview.cn.return_value = {"issues": ["period", "currency"]}
	preview.mx.return_value[0]["request_date"] = submitted
	for raw in preview.raw:
		raw["form_component_values"] = fields
		raw["create_time"] = submitted + "T18:00:00Z" if submitted else None
	result = s.preview(preview.row, "America/Mexico_City", ["corp"], processors)[0]
	assert (s.contract.ISSUES["period"] in result["issues"]) is not valid
	assert s.contract.ISSUES["currency"] in result["issues"]


@pytest.mark.parametrize("kind", ["operating", "purchase"])
@pytest.mark.parametrize("processors", [("cn",), ("cn", "mx")])
def test_real_parsers_accept_absent_application_date_with_approval_creation(monkeypatch, kind, processors):
	from test_mexico_expense_templates import sample
	template, raw = sample(kind)
	row = {key: template[key] for key in ("corp_id", "process_code", "approval_type", "template_name")}
	monkeypatch.setattr(s.mx.http, "configuration", lambda: ("https://oa.invalid", [row["corp_id"]]))
	read = Mock(return_value=([raw], {}))
	monkeypatch.setattr(s.mx.http, "read_template_page", read)
	assert "period" in s.contract.project(raw, template)["issues"]  # Original CN semantics stay strict.
	result = s.preview(row, "America/Mexico_City", [row["corp_id"]], processors)
	assert result[0]["count"] == 1
	assert s.contract.ISSUES["period"] not in result[0]["issues"]
	read.assert_called_once()


@pytest.mark.parametrize("table_id", ["TableField_1T6MS9XEQUE80", "TableField_copy"])
@pytest.mark.parametrize("change,valid", [
	("renamed", True), ("reference_amount", True), ("copied_column", True),
	("ambiguous_label", False), ("unknown_label", False),
	("blank_known_id", False), ("duplicate_id", False),
])
def test_department_amount_binding_agrees_in_both_parsers_and_unified_preview(monkeypatch, table_id, change, valid):
	from .test_cn_approvals import field, sample
	from deeplinkerp_branding.services import operating_expense_contract, regional_expense_contract

	monkeypatch.setattr(operating_expense_contract, "_", lambda text: text)
	monkeypatch.setattr(s, "_", lambda text: text)
	raw, template = sample()
	template["process_code"] = raw["process_code"] = "PROC-00000000-0000-0000-0000-000000000001"
	template.update(template_name="Department allocation", field_mapping=json.dumps({"allocation_table": [table_id]}))
	rows = []
	for index, value in enumerate(("10.01", "20.02", "30.03", "39.94"), 1):
		money = {"key": "MoneyField_D5PL7PJDHD40", "label": "金额（元）Monto (yuan)", "value": value}
		cells = [{"key": "DepartmentField_1C2A4A8ZG8TC0", "label": "部门Departamento", "value": f"Department {index}"}, money]
		if change == "renamed":
			money["label"] = "分摊金额"
		elif change == "reference_amount":
			cells.append({"key": "MoneyField_reference", "label": "总金额（参考）", "value": "900"})
		elif change in ("copied_column", "ambiguous_label", "unknown_label"):
			money["key"] = "MoneyField_copy"
			if change == "ambiguous_label":
				cells.append({**money, "key": "MoneyField_other"})
			elif change == "unknown_label":
				money["label"] = "金额（待核对）"
		elif change == "blank_known_id":
			cells.append({**money, "key": "MoneyField_other"})
			money["value"] = ""
		elif change == "duplicate_id":
			cells.append(dict(money))
		rows.append({"rowNumber": str(index), "rowValue": cells})
	raw["form_component_values"].append(field(table_id, json.dumps(rows), "TableField", "明细detalle"))
	cn = s.contract.project(raw, template)
	mx_source, mx_lines = s.mx.projection.project(raw, template, "Asia/Shanghai", include_finance=False)
	if valid:
		assert cn["detail_total"] == "100.00" and cn["issues"] == []
		assert len(mx_lines) == 4 and all(not line["base_issues"] for line in mx_lines)
		assert sum(Decimal(line["amount"]) for line in mx_lines) == Decimal(mx_source["total_amount"]) == 100
	else:
		assert cn["detail_total"] is None and "detail_amount" in cn["issues"]
		assert any(regional_expense_contract.BLOCKING & set(line["base_issues"]) for line in mx_lines)
	merged = {key: template[key] for key in ("corp_id", "process_code", "approval_type", "template_name")}
	merged.update(application_fields=template["field_mapping"], expense_fields=template["field_mapping"])
	monkeypatch.setattr(s.mx.http, "configuration", lambda: ("https://oa.invalid", [template["corp_id"]]))
	read = Mock(return_value=([raw], {}))
	monkeypatch.setattr(s.mx.http, "read_template_page", read)
	result = s.preview(merged, "Asia/Shanghai", [template["corp_id"]], ("cn", "mx"))[0]
	assert result["count"] == 1
	assert (result["issues"] == []) is valid
	if not valid:
		assert s.contract.ISSUES["detail_amount"] in result["issues"]
	read.assert_called_once_with("https://oa.invalid", template["corp_id"], template["process_code"], limit=3)
