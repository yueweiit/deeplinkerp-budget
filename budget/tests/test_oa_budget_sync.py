"""台账同步的纯逻辑测试：指纹（增量判定）、运行记录字段与明细解析口径。

不碰站点、不碰数据库——都是纯函数，回归成本最低。
"""

from __future__ import annotations

import copy
import json

from budget.services import oa_budget_parser, oa_budget_sync

ENTRY = {
	"form_no": "202604171454000137284",
	"process_instance_id": "pid-1",
	"company": "悦为集团-拉丁购",
	"department": "财务中心",
	"department_id": "dept-1",
	"budget_kind": "non_production",
	"amount": 1000.0,
	"total_amount": 1000.0,
	"dept_path_ids": '["root", "dept-1"]',
	"dept_path_names": '["悦为集团-拉丁购", "财务中心"]',
	"synced_at": "2026-11-16 10:00:00",
}

DETAILS = [
	{"form_no": "202604171454000137284", "detail_item": "办公费", "detail_amount": 600.0},
	{"form_no": "202604171454000137284", "detail_item": "差旅费", "detail_amount": 400.0},
]


def test_entry_digest_ignores_synced_at() -> None:
	"""synced_at 每轮都变；它若参与指纹，增量就退化成全量。"""

	first = oa_budget_sync._entry_digest(
		{**ENTRY, "synced_at": "2026-11-16 10:00:00"}, DETAILS
	)
	second = oa_budget_sync._entry_digest(
		{**ENTRY, "synced_at": "2026-11-17 23:59:59"}, DETAILS
	)

	assert first == second


def test_entry_digest_is_stable_across_calls() -> None:
	assert oa_budget_sync._entry_digest(ENTRY, DETAILS) == oa_budget_sync._entry_digest(
		ENTRY, DETAILS
	)


def test_entry_digest_changes_with_entry_field() -> None:
	"""主表任一入库字段变化都要能触发更新（部门路径改名也算）。"""

	changed = copy.deepcopy(ENTRY)
	changed["department"] = "管理规划中心/财务中心"

	assert oa_budget_sync._entry_digest(ENTRY, DETAILS) != oa_budget_sync._entry_digest(
		changed, DETAILS
	)


def test_entry_digest_changes_with_details() -> None:
	"""明细是台账的一部分：改了明细金额就必须重建明细。"""

	changed = copy.deepcopy(DETAILS)
	changed[0]["detail_amount"] = 700.0

	assert oa_budget_sync._entry_digest(ENTRY, DETAILS) != oa_budget_sync._entry_digest(
		ENTRY, changed
	)


def test_entry_digest_counts_detail_order() -> None:
	"""明细顺序变化也会改变指纹（宁可多重建一次，也不漏掉顺序调整）。"""

	assert oa_budget_sync._entry_digest(
		ENTRY, DETAILS
	) != oa_budget_sync._entry_digest(ENTRY, list(reversed(DETAILS)))


def test_log_values_omits_missing_counters() -> None:
	"""跳过 / 失败轮次没有这些计数，不能写成 0 冒充「跑过且没变化」。"""

	values = oa_budget_sync._log_values(
		"Skipped",
		{"skipped": True, "reason": "另一个预算台账同步正在运行"},
		finished_at="2026-11-16 10:00:05",
		message="另一个预算台账同步正在运行",
		full_sync=False,
	)

	assert values["status"] == "Skipped"
	assert values["full_sync"] == 0
	assert values["message"] == "另一个预算台账同步正在运行"
	for field in oa_budget_sync.LOG_COUNT_FIELDS:
		assert field not in values


def test_log_values_keeps_zero_and_converts_numbers() -> None:
	values = oa_budget_sync._log_values(
		"Success",
		{
			"remote_total": "46",
			"deduped": 44,
			"created": 0,
			"updated": 0,
			"unchanged": 44,
			"removed": 0,
			"detail_rows": 0,
			"local_total": 44,
			"local_amount": "6332991.11",
		},
		finished_at="2026-11-16 10:00:02",
		duration_sec=0.618,
		full_sync=True,
	)

	assert values["remote_total"] == 46
	assert values["created"] == 0
	assert values["unchanged"] == 44
	assert values["duration_sec"] == 0.62
	assert values["full_sync"] == 1
	assert values["local_amount"] == 6332991.11


def test_log_values_truncates_long_message() -> None:
	values = oa_budget_sync._log_values(
		"Failed", None, finished_at="2026-11-16 10:00:02", message="错" * 2000
	)

	assert len(values["message"]) == 1000


def _instance(components: list[dict], form_no: str = "F0001") -> dict:
	return {
		"form_component_values": json.dumps(components, ensure_ascii=False),
		"raw_payload": json.dumps({"businessId": form_no}, ensure_ascii=False),
		"process_instance_id": "pid-" + form_no,
		"process_code": oa_budget_parser.BUDGET_PROCESS_CODE,
		"status": "COMPLETED",
		"result": "agree",
		"create_time": "2026-11-01T10:00:00",
		"originator_dept_id": "dept-1",
		"originator_dept_name": "悦为集团-拉丁购/财务中心",
		"originator_user_name": "张三",
	}


def _subform(name: str, rows: list[dict]) -> dict:
	return {"name": name, "value": json.dumps([{"rowValue": rows}], ensure_ascii=False)}


def test_operation_detail_keeps_purpose_note() -> None:
	"""「预算项目」子表单的「说明」是业务用途，必须落到 budget_purpose_detail。

	此前只有 hr / office 组取「说明」，预算项目整列的说明都丢了。
	"""
	instance = _instance(
		[
			{"name": "预算类型Tipo de presupuesto", "value": "非生产no producción"},
			{"name": "预算月份Mes presupuestario", "value": "2026-11"},
			{"name": "预算总金额Importe", "value": "100"},
			_subform(
				"预算项目Proyecto presupuestario",
				[
					{
						"label": "预算明细Detalle presupuestario",
						"value": "办公费",
					},
					{"label": "说明", "value": "4月第2次发货采购（海运计划）"},
					{"label": "人民币金额Monto en yuanes chinos", "value": "100"},
				],
			),
		]
	)

	parsed = oa_budget_parser.parse_budget_instance(instance)

	assert len(parsed["details"]) == 1
	detail = parsed["details"][0]
	assert detail["detail_group"] == "operation"
	assert detail["detail_item"] == "办公费"
	assert detail["budget_purpose_detail"] == "4月第2次发货采购（海运计划）"
	assert detail["amount"] == 100.0


def test_hr_detail_falls_back_to_unlabeled_first_cell() -> None:
	"""人资预算第一列没有 label，说明文字在那，不能丢。"""
	instance = _instance(
		[
			{"name": "预算月份Mes presupuestario", "value": "2026-11"},
			_subform(
				"人资预算Presupuesto de Recursos Humanos",
				[
					{"label": "", "value": "工资总额"},
					{"label": "明细项目Ítem detallado", "value": "工资Salario"},
					{"label": "人数Número de personal", "value": "3"},
					{"label": "人民币金额Monto en yuanes chinos", "value": "300"},
				],
			),
		]
	)

	parsed = oa_budget_parser.parse_budget_instance(instance)

	detail = parsed["details"][0]
	assert detail["detail_group"] == "hr"
	assert detail["budget_purpose_detail"] == "工资总额"
	assert detail["headcount"] == 3.0


def test_office_detail_item_rejects_subform_title() -> None:
	"""办公场地预算首行的「明细项目」是子表单标题（合并单元格残留下来的）。

	整串「办公场地预算Presupuesto del espacio de oficina」不是项目名，必须走兜底名，
	但金额和人数要保留。
	"""
	instance = _instance(
		[
			{"name": "预算月份Mes presupuestario", "value": "2026-11"},
			_subform(
				"办公场地预算Presupuesto del espacio de oficina",
				[
					{"label": "明细项目Ítem detallado", "value": "办公场地预算Presupuesto del espacio de oficina"},
					{"label": "人数Número de personal", "value": "14"},
					{"label": "人民币金额Monto en yuanes chinos", "value": "14000"},
				],
			),
		]
	)

	parsed = oa_budget_parser.parse_budget_instance(instance)

	detail = parsed["details"][0]
	assert detail["detail_group"] == "office"
	assert detail["detail_item"] == "办公场地预算-1"
	assert detail["headcount"] == 14.0
	assert detail["amount"] == 14000.0
