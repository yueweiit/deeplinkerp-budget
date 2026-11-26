"""New OA identities are configurable, bounded previews never write business data."""
import copy
import json
from unittest.mock import Mock

import frappe
import pytest

from budget.services import cn_approval_contract as cn
from budget.services import expense_sync_settings as settings
from budget.services import expense_template_editor as editor
from deeplinkerp_branding.services import expense_template_mapping as bindings
from deeplinkerp_branding.services import mexico_expense_projection as mx
from deeplinkerp_branding.services import mexico_expense_templates as templates
from test_operating_department_allocation import approval, set_rows


def mapped_allocation(currency="CNY"):
	config, raw, rows = approval()
	fields = raw["raw_payload"]["formComponentValues"]
	fields[1].update(id="MoneyField_new_total", name="New total")
	fields[2]["value"] = currency
	fields[-1].update(id="TableField_new_details", name="New details")
	for row in rows:
		for cell, key in zip(row["rowValue"], ("DepartmentField_new_owner", "MoneyField_new_amount", "TextField_new_note")):
			cell.update(key=key, label="Unrelated label")
	set_rows(raw, rows)
	config["field_mapping"] = json.dumps({"amount": ["MoneyField_new_total"], "allocation_table": ["TableField_new_details"],
		"table_fields": {"allocation_table": {"entity": ["DepartmentField_new_owner"], "amount": ["MoneyField_new_amount"],
			"description": ["TextField_new_note"]}}, "table_currency": {"allocation_table": "header"}})
	return config, raw, rows


@pytest.fixture(autouse=True)
def no_io(monkeypatch):
	monkeypatch.setattr(frappe, "throw", lambda message, *a: (_ for _ in ()).throw(frappe.ValidationError(message)))
	monkeypatch.setattr(editor, "_", lambda message: message)
	monkeypatch.setattr("deeplinkerp_branding.services.operating_expense_contract._", lambda message: message)


@pytest.mark.parametrize("currency", ["CNY", "MXN", "USD"])
def test_changed_header_table_and_child_ids_are_shared_by_both_processors(currency):
	config, raw, rows = mapped_allocation(currency)
	templates.normalize(config, require_finance=False)
	source, lines = mx.project(raw, config, "Asia/Shanghai", include_finance=False)
	item = cn.project(raw, config)
	assert item["amount"] == item["detail_total"] == source["total_amount"] == "100.00"
	assert item["detail_currency"] == currency
	assert item["issues"] == ["period"]  # Legacy CN alone requires the application date.
	assert all(not line["base_issues"] for line in lines)
	assert [line["amount"] for line in lines] == [detail["amount"] for detail in item["tables"][0]["rows"]]
	assert [line["_source_entity_id"] for line in lines] == [detail["entity_id"] for detail in item["tables"][0]["rows"]]
	assert [line["description"] for line in lines] == [f"Note {i}" for i in range(1, 5)]
	assert raw["raw_payload"]["formComponentValues"][-1]["id"] == "TableField_new_details"


@pytest.mark.parametrize("role", ["entity", "amount", "description"])
@pytest.mark.parametrize("failure", ["missing", "duplicate", "type"])
def test_explicit_child_missing_or_ambiguous_is_never_replaced_by_legacy_labels(role, failure):
	config, raw, rows = mapped_allocation()
	key = json.loads(config["field_mapping"])["table_fields"]["allocation_table"][role][0]
	cell = next(cell for cell in rows[0]["rowValue"] if cell["key"] == key)
	if failure == "duplicate": rows[0]["rowValue"].append(copy.deepcopy(cell))
	elif failure == "type": cell["componentType"] = "DDAttachment"
	else: cell["key"] = "DifferentField"
	set_rows(raw, rows)
	assert "structure" in cn.project(raw, config)["issues"]
	assert all("structure" in line["base_issues"] for line in mx.project(raw, config, "Asia/Shanghai", include_finance=False)[1])


@pytest.mark.parametrize("mapping", [
	{"table_fields": []}, {"table_fields": {"unsupported": {}}},
	{"table_fields": {"allocation_table": {"amount": "MoneyField_bad"}}},
	{"table_fields": {"allocation_table": {"amount": ["MoneyField_x"], "entity": ["MoneyField_x"]}}},
	{"table_fields": {"allocation_table": {"amount": ["x", "x"]}}},
	{"table_currency": {"allocation_table": "USD"}}, {"table_currency": {"salary_table": "header"}},
])
def test_invalid_nested_mappings_are_rejected(mapping):
	with pytest.raises(ValueError, match="Invalid template field mapping"):
		bindings.validate(mapping, "operating")


def test_one_table_cannot_be_given_two_financial_roles():
	config, raw, _rows = mapped_allocation()
	config["field_mapping"] = json.dumps({"salary_table": ["TableField_new_details"], "social_table": ["TableField_new_details"]})
	assert "structure" in cn.project(raw, config)["issues"]
	assert all("structure" in line["base_issues"] for line in mx.project(raw, config, "Asia/Shanghai", include_finance=False)[1])


def test_copied_purchase_rows_check_multiplication_through_configured_columns():
	from test_mexico_expense_templates import sample, component, table_row
	config, raw = sample("purchase")
	row = table_row("1")
	roles = {"amount": ("CalculateField_1D7YT3J5Z4YO0", "CalculateField_new"),
		"quantity": ("NumberField_6Q22XFQ51VW0", "NumberField_quantity"),
		"unit_price": ("NumberField_1P237DCILQKG0", "NumberField_price")}
	for old, new in roles.values():
		next(cell for cell in row["rowValue"] if cell["key"] == old).update(key=new, label="New label")
	config["field_mapping"] = json.dumps({"items_table": ["TableField_new"], "table_fields": {
		"items_table": {role: [new] for role, (_old, new) in roles.items()}}})
	raw["raw_payload"]["formComponentValues"].append(component("TableField_new", [row], "TableField"))
	assert not mx.project(raw, config, "Asia/Shanghai", include_finance=False)[1][0]["base_issues"]
	next(cell for cell in row["rowValue"] if cell["key"] == "NumberField_price")["value"] = "51"
	assert "total" in mx.project(raw, config, "Asia/Shanghai", include_finance=False)[1][0]["base_issues"]


def test_gastos_child_overrides_apply_even_when_table_id_is_unchanged():
	from test_mexico_expense_contract import approval as gastos
	raw = gastos()
	config = next(row for row in templates.default_templates() if row["process_code"] == raw["process_code"])
	table = raw["form_component_values"][-1]
	rows = json.loads(table["value"])
	roles = dict(zip(("entity", "amount", "description"), ("DDSelectField_new", "MoneyField_new", "TextField_new")))
	for cell, key in zip(rows[0]["rowValue"], roles.values()): cell["key"] = key
	table["value"] = json.dumps(rows)
	config["field_mapping"] = json.dumps({"table_fields": {"expense_table": {role: [key] for role, key in roles.items()}}})
	_, lines = mx.project(raw, config, "America/Mexico_City", include_finance=False)
	assert lines[0]["source_entity"] == "LATINGO MX" and lines[0]["amount"] == "251739.00"
	assert lines[0]["description"] == "Electricity" and not lines[0]["base_issues"]


def test_budget_headers_and_detail_fields_preserve_cny_budget_semantics():
	from budget.tests.test_cn_approvals import sample
	raw, config = sample("budget")
	fields = raw["form_component_values"]
	fields[5]["id"] = "MoneyField_new_header"
	fields[3]["id"] = "DDDateField_new_month"
	table = fields[-1]; table.update(id="TableField_new", name="Different table label")
	cells = table["value"][0]["rowValue"]
	cells[1].update(key="TextField_item", label="Different label")
	cells[2].update(key="MoneyField_detail", label="Different label")
	config["field_mapping"] = json.dumps({"amount": [fields[5]["id"]], "budget_month": [fields[3]["id"]],
		"hr_table": [table["id"]], "table_fields": {"hr_table": {"label": [cells[1]["key"]], "amount": [cells[2]["key"]]}}})
	bindings.validate(json.loads(config["field_mapping"]), "budget")
	item = cn.project(raw, config)
	assert not item["issues"] and item["detail_total"] == "100.00"
	assert item["tables"][0]["group"] == "hr" and item["tables"][0]["rows"][0]["label"] == "Services"


@pytest.mark.parametrize("key,value,kind,label,output,expected", [
	("amount", "100", "MoneyField", "预算总金额", "amount", "100.00"),
	("entity", "Organization A", "DepartmentField", "部门", "entity", "Organization A"),
	("budget_month", "2026-11", "DDDateField", "预算月份", "month", "2026-11"),
	("application_date", "2026-11-01", "DDDateField", "申请日期", "application_date", "2026-11-01"),
	("expense_type", "Services", "DDSelectField", "预算类型", "category", "Services"),
	("description", "Approved plan", "TextareaField", "备注", "description", "Approved plan"),
])
def test_explicit_budget_header_skips_ambiguous_legacy_labels(key, value, kind, label, output, expected):
	from budget.tests.test_cn_approvals import sample, field
	raw, config = sample("budget")
	fields = raw["form_component_values"]
	fields[:] = [item for item in fields if item["id"] not in bindings.BUDGET_FIELDS[key][0]]
	selected = kind + "_selected"
	fields.extend([field(selected, value, kind, label), field(kind + "_reference", value, kind, label + "（参考）")])
	# Without a mapping the two populated legacy candidates must remain ambiguous.
	assert "structure" in cn.project(raw, config)["issues"]
	config["field_mapping"] = json.dumps({key: [selected]})
	item = cn.project(raw, config)
	assert item[output] == expected and not item["issues"]
	assert item["amount"] == item["detail_total"] == "100.00"


@pytest.mark.parametrize("kind", ["operating", "purchase"])
def test_explicit_application_date_skips_ambiguous_legacy_dates(kind):
	from budget.tests.test_cn_approvals import sample, field
	raw, config = sample(kind)
	fields = raw["form_component_values"]
	fields[:] = [item for item in fields if item["id"] not in bindings.APPLICATION_DATE[0]]
	fields.extend([field("DDDateField_selected", "2026-11-01", "DDDateField", "申请日期"),
		field("DDDateField_reference", "2026-10-01", "DDDateField", "申请日期（参考）")])
	assert "structure" in cn.project(raw, config)["issues"]
	config["field_mapping"] = json.dumps({"application_date": ["DDDateField_selected"]})
	item = cn.project(raw, config)
	assert item["application_date"] == "2026-11-01" and item["month"] == "2026-11"
	assert not item["issues"]


@pytest.mark.parametrize("failure", ["missing", "duplicate", "type"])
def test_invalid_explicit_budget_amount_does_not_fall_back_to_valid_legacy_amount(failure):
	from budget.tests.test_cn_approvals import sample, field
	raw, config = sample("budget")
	config["field_mapping"] = json.dumps({"amount": ["MoneyField_selected"]})
	selected = field("MoneyField_selected", "100", "TextField" if failure == "type" else "MoneyField")
	if failure != "missing": raw["form_component_values"].append(selected)
	if failure == "duplicate": raw["form_component_values"].append(copy.deepcopy(selected))
	item = cn.project(raw, config)
	assert item["amount"] is None and "amount" in item["issues"]
	if failure != "missing": assert "structure" in item["issues"]


@pytest.mark.parametrize("currency", ["CNY", "MXN", "USD"])
@pytest.mark.parametrize("currency_rule", ["header", "CNY"])
def test_cn_only_preview_keeps_the_parsed_detail_currency(currency, currency_rule):
	from budget.tests.test_cn_approvals import field
	config, raw, _rows = mapped_allocation(currency)
	mapping = json.loads(config["field_mapping"])
	mapping["table_currency"]["allocation_table"] = currency_rule
	config["field_mapping"] = json.dumps(mapping)
	raw["raw_payload"]["formComponentValues"].append(field("DDDateField_29I59O630HUS", "2026-11-01", "DDDateField"))
	item = cn.project(raw, config)
	result = editor.sample_preview(raw, {"cn": config}, "Asia/Shanghai", 1)
	expected_currency = currency if currency_rule == "header" else "CNY"
	assert result["detail_currency"] == item["detail_currency"] == expected_currency
	assert all(line["currency"] == expected_currency for line in result["lines"])
	assert result["currency"] == currency and result["detail_total"] == item["detail_total"] == "100.00"
	assert result["issues"] == ([cn.ISSUES["detail_currency"]] if expected_currency != currency else [])


def test_cn_budget_preview_keeps_cny_for_converted_detail_amounts():
	from budget.tests.test_cn_approvals import sample
	raw, config = sample("budget")
	raw["form_component_values"][-1]["value"][0]["rowValue"].extend([
		{"key": "MoneyField_original", "label": "原币金额", "value": "900"},
		{"key": "DDSelectField_currency", "label": "币种", "value": "MXN"},
		{"key": "NumberField_rate", "label": "汇率", "value": "9"}])
	result = editor.sample_preview(raw, {"cn": config}, "Asia/Shanghai", 1)
	assert result["detail_currency"] == result["lines"][0]["currency"] == "CNY"
	assert result["detail_total"] == result["header_total"] == "100.00" and not result["issues"]


def test_inspect_reads_one_bounded_sample_and_returns_metadata_and_monetary_preview(monkeypatch):
	config, raw, _ = mapped_allocation()
	raw["raw_payload"]["secret-task"] = "NEVER_RETURN"
	row = {key: config[key] for key in ("approval_type", "corp_id", "process_code", "template_name")}
	row.update(application_fields=config["field_mapping"], expense_fields=config["field_mapping"])
	read = Mock(return_value=([raw], {}))
	monkeypatch.setattr(editor.preview.mx.http, "configuration", lambda: ("https://oa.invalid", [row["corp_id"]]))
	monkeypatch.setattr(editor.preview.mx.http, "read_template_page", read)
	result = editor.inspect(row, "Asia/Shanghai", [row["corp_id"]], ("cn", "mx"))
	read.assert_called_once_with("https://oa.invalid", row["corp_id"], row["process_code"], limit=3)
	assert result["sample_count"] == 1 and result["samples"][0]["detail_total"] == "100.00"
	assert not result["samples"][0]["issues"] and len(result["samples"][0]["lines"]) == 4
	assert all(set(field) == {"id", "label", "type"} for field in result["fields"])
	assert {f["id"] for f in result["tables"]["TableField_new_details"]} == {
		"DepartmentField_new_owner", "MoneyField_new_amount", "TextField_new_note"}
	assert "NEVER_RETURN" not in json.dumps(result) and "Note 1" not in json.dumps(result)
	row["expense_fields"] = '{"table_fields":{"unknown":{}}}'
	read.reset_mock()
	with pytest.raises(frappe.ValidationError): editor.inspect(row, "Asia/Shanghai", [row["corp_id"]], ("cn", "mx"))
	read.assert_not_called()


def test_inspect_requires_global_manager_before_reading_oa(monkeypatch):
	manager = Mock(side_effect=frappe.PermissionError)
	read = Mock()
	monkeypatch.setattr(settings.cn, "require_manager", manager)
	monkeypatch.setattr(editor.preview.mx.http, "read_template_page", read)
	with pytest.raises(frappe.PermissionError): settings.inspect_template({}, "Asia/Shanghai")
	manager.assert_called_once(); read.assert_not_called()


@pytest.mark.parametrize("bad", ["invalid", "duplicate", "missing"])
def test_configured_date_remains_blocked_in_preview(bad):
	config, raw, _rows = mapped_allocation()
	mapping = json.loads(config["field_mapping"]); mapping["application_date"] = ["DDDateField_new"]
	config["field_mapping"] = json.dumps(mapping)
	field = {"id": "DDDateField_new", "componentType": "DDDateField", "value": "bad" if bad == "invalid" else "2026-11-01"}
	fields = raw["raw_payload"]["formComponentValues"]
	if bad != "missing": fields.append(field)
	if bad == "duplicate": fields.append(copy.deepcopy(field))
	result = editor.sample_preview(raw, {"cn": config, "mx": config}, "Asia/Shanghai", 1)
	assert cn.ISSUES["period"] in result["issues"]
