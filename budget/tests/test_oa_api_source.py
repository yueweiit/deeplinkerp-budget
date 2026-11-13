from __future__ import annotations

import pytest

from budget.integrations.oa_api_source import OAAPIConfig, OAAPISource, OAAPISourceError
from budget.integrations.oa_read_api_client import ApprovalPage

CONFIG = OAAPIConfig(base_url="https://oa.example", corp_id="ding-1", page_size=200)


class FakeClient:
	"""按队列返回分页，记录调用参数。"""

	def __init__(self, pages: list[ApprovalPage] | None = None, detail: dict | None = None):
		self.pages = list(pages or [])
		self.detail = detail
		self.calls: list[tuple] = []

	def list_approvals(self, corp_id, **kwargs) -> ApprovalPage:
		self.calls.append(("list_approvals", corp_id, kwargs))
		return self.pages.pop(0)

	def get_approval(self, corp_id, process_instance_id, *, by_approval_no=None):
		self.calls.append(("get_approval", corp_id, process_instance_id, by_approval_no))
		return self.detail


class FakeFallback:
	"""接口不覆盖的部门查询应原样落到这里。"""

	def __init__(self):
		self.calls: list[tuple] = []

	def list_departments(self, *, current_only=True):
		self.calls.append(("list_departments", current_only))
		return [{"dept_id": "d1"}]

	def list_department_candidates(self, service_entity, *, corresponding_department=None, dept_code=None):
		self.calls.append(("list_department_candidates", service_entity))
		return [{"dept_id": "d1"}]

	def list_department_snapshot_rows(self, dept_ids):
		self.calls.append(("list_department_snapshot_rows", tuple(dept_ids)))
		return [{"dept_id": "d1"}]

	def get_instance_row(self, instance_id, *, with_snapshot=True):
		self.calls.append(("get_instance_row", instance_id))
		return {"snapshot_user_name": "张三"}


def approval_item(**overrides) -> dict:
	item = {
		"corpId": "ding-1",
		"processInstanceId": "inst-1",
		"processCode": "PROC-1",
		"templateName": "运营支出",
		"title": "示例审批",
		"status": "COMPLETED",
		"result": "agree",
		"originatorUserId": "u1",
		"originatorUserName": "张三",
		"originatorDeptId": "d1",
		"originatorDeptName": "财务部",
		"createTime": "2026-11-02T01:00:00.000000Z",
		"finishTime": "2026-11-02T03:00:00.000000Z",
		"formComponentValues": [{"name": "金额"}],
		"rawPayload": {
			"businessId": "B-1",
			"tasks": [
				{
					"taskId": "t1",
					"activityId": "审批节点",
					"userId": "u2",
					"userName": "李四",
					"status": "COMPLETED",
					"result": "agree",
					"createTime": "2026-11-02T01:00:00.000000Z",
					"finishTime": "2026-11-02T02:00:00.000000Z",
				}
			],
		},
	}
	item.update(overrides)
	return item


def test_efficiency_rows_map_instance_and_tasks() -> None:
	client = FakeClient([ApprovalPage(rows=[approval_item()])])
	source = OAAPISource(CONFIG, client=client)

	instances, tasks = source.list_approval_efficiency_rows()

	assert client.calls[0][1] == "ding-1"
	assert instances == [
		{
			"process_instance_id": "inst-1",
			"process_code": "PROC-1",
			"process_name": "运营支出",
			"title": "示例审批",
			"status": "COMPLETED",
			"result": "agree",
			"create_time": "2026-11-02T01:00:00.000000Z",
			"finish_time": "2026-11-02T03:00:00.000000Z",
		}
	]
	assert tasks[0]["task_id"] == "t1"
	assert tasks[0]["node_name"] == "审批节点"
	assert tasks[0]["approver_user_id"] == "u2"
	assert tasks[0]["approver_user_name"] == "李四"
	assert tasks[0]["start_time"] == "2026-11-02T01:00:00.000000Z"


def test_efficiency_rows_filter_by_create_time_and_narrow_by_updated_after() -> None:
	in_window = approval_item()
	before = approval_item(
		processInstanceId="inst-old", createTime="2026-10-01T00:00:00.000000Z"
	)
	after = approval_item(
		processInstanceId="inst-new", createTime="2026-12-01T00:00:00.000000Z"
	)
	client = FakeClient([ApprovalPage(rows=[in_window, before, after])])
	source = OAAPISource(CONFIG, client=client)

	instances, _ = source.list_approval_efficiency_rows(
		start_date="2026-11-01", end_date="2026-11-30"
	)

	assert [row["process_instance_id"] for row in instances] == ["inst-1"]
	assert client.calls[0][2]["updated_after"] == "2026-11-01T00:00:00+00:00"


def test_efficiency_rows_follow_cursor_pages() -> None:
	first = ApprovalPage(rows=[approval_item(processInstanceId="inst-1")], next_cursor="c1", has_more=True)
	second = ApprovalPage(rows=[approval_item(processInstanceId="inst-2")])
	client = FakeClient([first, second])
	source = OAAPISource(CONFIG, client=client)

	instances, _ = source.list_approval_efficiency_rows()

	assert [row["process_instance_id"] for row in instances] == ["inst-1", "inst-2"]
	assert client.calls[1][2]["cursor"] == "c1"


def test_instance_row_maps_api_item() -> None:
	client = FakeClient(detail=approval_item())
	source = OAAPISource(CONFIG, client=client)

	row = source.get_instance_row("inst-1", with_snapshot=False)

	assert row["process_instance_id"] == "inst-1"
	assert row["originator_user_name"] == "张三"
	assert row["form_component_values"] == [{"name": "金额"}]
	assert row["raw_payload"]["businessId"] == "B-1"
	assert row["snapshot_user_name"] is None


def test_instance_row_falls_back_to_approval_no() -> None:
	class SequentialClient(FakeClient):
		def get_approval(self, corp_id, process_instance_id, *, by_approval_no=None):
			self.calls.append(("get_approval", corp_id, process_instance_id, by_approval_no))
			return None if by_approval_no is None else approval_item()

	client = SequentialClient()
	source = OAAPISource(CONFIG, client=client)

	row = source.get_instance_row("B-1", with_snapshot=False)

	assert row["process_instance_id"] == "inst-1"
	assert client.calls == [
		("get_approval", "ding-1", "B-1", None),
		("get_approval", "ding-1", "B-1", "B-1"),
	]


def test_instance_row_enriches_snapshot_only_when_name_looks_like_user_id() -> None:
	fallback = FakeFallback()
	client = FakeClient(detail=approval_item(originatorUserName="u1"))
	source = OAAPISource(CONFIG, client=client, fallback=fallback)

	row = source.get_instance_row("inst-1")

	assert row["snapshot_user_name"] == "张三"
	assert fallback.calls == [("get_instance_row", "inst-1")]

	fallback = FakeFallback()
	client = FakeClient(detail=approval_item(originatorUserName="王五"))
	source = OAAPISource(CONFIG, client=client, fallback=fallback)

	assert source.get_instance_row("inst-1")["snapshot_user_name"] is None
	assert fallback.calls == []


def test_department_queries_fall_back_to_postgres() -> None:
	fallback = FakeFallback()
	source = OAAPISource(CONFIG, client=FakeClient(), fallback=fallback)

	assert source.list_departments() == [{"dept_id": "d1"}]
	assert source.list_department_candidates("主体") == [{"dept_id": "d1"}]
	assert source.list_department_snapshot_rows(["d1"]) == [{"dept_id": "d1"}]
	assert fallback.calls == [
		("list_departments", True),
		("list_department_candidates", "主体"),
		("list_department_snapshot_rows", ("d1",)),
	]


def test_missing_corp_id_is_rejected() -> None:
	source = OAAPISource(OAAPIConfig(base_url="https://oa.example"), client=FakeClient())

	with pytest.raises(OAAPISourceError) as excinfo:
		source.list_approval_efficiency_rows()

	assert "corp_id" in str(excinfo.value)


def test_factory_defaults_to_postgres_and_switches_on_http(monkeypatch) -> None:
	from budget.integrations import oa_factory

	monkeypatch.delenv("BUDGET_OA_SOURCE", raising=False)
	assert oa_factory.use_http_source() is False

	monkeypatch.setenv("BUDGET_OA_SOURCE", "http")
	assert oa_factory.use_http_source() is True
	assert isinstance(oa_factory.get_oa_source(), OAAPISource)

	monkeypatch.setenv("BUDGET_OA_SOURCE", "postgres")
	assert oa_factory.use_http_source() is False
