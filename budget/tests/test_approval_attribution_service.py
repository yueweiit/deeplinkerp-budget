from __future__ import annotations

import datetime

from budget.services import approval_attribution_service as service


def test_resolve_name_prefers_snapshot_when_source_is_numeric_employee_id() -> None:
	assert service.resolve_originator_user_name("10086", "u1", "张三") == "张三"


def test_resolve_name_prefers_snapshot_when_source_equals_user_id() -> None:
	assert service.resolve_originator_user_name("u1", "u1", "张三") == "张三"


def test_resolve_name_prefers_snapshot_when_source_is_empty() -> None:
	assert service.resolve_originator_user_name("", "u2", "王五") == "王五"


def test_resolve_name_keeps_real_name_over_snapshot() -> None:
	assert service.resolve_originator_user_name("李四", "u3", "张三三") == "李四"


def test_resolve_name_ignores_fullwidth_digits_as_employee_id() -> None:
	assert service.resolve_originator_user_name("１２３", "u4", None) == "１２３"


def test_resolve_name_falls_back_to_user_id() -> None:
	assert service.resolve_originator_user_name(None, "u9", None) == "u9"


def test_resolve_name_returns_none_without_any_source() -> None:
	assert service.resolve_originator_user_name(None, None, None) is None


def test_build_instance_detail_prefers_raw_payload_fields() -> None:
	row = {
		"corp_id": "corp-1",
		"process_instance_id": "inst-1",
		"process_code": "PROC-1",
		"title": "行标题",
		"raw_payload": {"title": "载荷标题", "businessId": "B-1"},
	}
	detail = service.build_instance_detail(row)

	assert detail["title"] == "载荷标题"
	assert detail["businessId"] == "B-1"
	assert detail["processInstanceId"] == "inst-1"
	assert detail["originator"]["userId"] is None


def test_build_instance_detail_falls_back_to_row_fields() -> None:
	row = {
		"process_instance_id": "inst-2",
		"create_time": datetime.datetime(2026, 11, 1, 1, 0, 0),
		"form_component_values": [{"id": "field-1"}],
	}
	detail = service.build_instance_detail(row)

	assert detail["createTime"] == "2026-11-01T01:00:00"
	assert detail["formComponentValues"] == [{"id": "field-1"}]
	assert detail["tasks"] == []


def test_apply_originator_snapshot_updates_originator_object() -> None:
	detail = {"originatorUserName": "10086", "originatorUserId": "u1", "originator": {"name": "10086"}}

	service.apply_originator_snapshot(detail, "张三")

	assert detail["originatorUserName"] == "张三"
	assert detail["originator"]["name"] == "张三"


