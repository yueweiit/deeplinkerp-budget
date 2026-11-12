from __future__ import annotations

from budget.services import approval_query_service as service


class FakeSource:
	def __init__(self, **data):
		self.data = data
		self.calls: list[tuple] = []

	def get_instance_row(self, instance_id, *, with_snapshot=True):
		self.calls.append(("get_instance_row", instance_id, with_snapshot))
		return self.data.get("instance_row")

	def list_departments(self, *, current_only=True):
		return self.data.get("department_rows", [])

	def list_department_candidates(self, service_entity, *, corresponding_department=None, dept_code=None):
		return self.data.get("candidate_rows", [])

	def list_department_snapshot_rows(self, dept_ids):
		return self.data.get("snapshot_rows", [])

	def list_approval_efficiency_rows(self, *, start_date=None, end_date=None):
		return self.data.get("instances", []), self.data.get("tasks", [])


def test_instance_detail_applies_snapshot_attribution() -> None:
	source = FakeSource(
		instance_row={
			"process_instance_id": "inst-1",
			"originator_user_id": "u1",
			"originator_user_name": "10086",
			"snapshot_user_name": "张三",
			"raw_payload": {"originatorUserName": "10086"},
		}
	)

	detail = service.get_approval_instance(source, "inst-1")

	assert detail is not None
	assert detail["originatorUserName"] == "张三"
	assert detail["originator"]["name"] == "张三"


def test_budget_detail_keeps_raw_name_without_snapshot() -> None:
	source = FakeSource(
		instance_row={
			"process_instance_id": "inst-1",
			"originator_user_name": "10086",
			"snapshot_user_name": "张三",
		}
	)

	detail = service.get_approval_instance(source, "inst-1", with_snapshot=False)

	assert detail is not None
	assert detail["originatorUserName"] == "10086"


def test_missing_instance_returns_none() -> None:
	source = FakeSource(instance_row=None)

	assert service.get_approval_instance(source, "missing") is None


def test_resolve_department_returns_none_when_candidates_are_ambiguous() -> None:
	rows = [
		{"dept_id": "d3", "name": "采购部", "is_current": True},
		{"dept_id": "d4", "name": "采购部", "is_current": True},
	]
	source = FakeSource(candidate_rows=rows)

	assert service.resolve_department(source, "采购部") is None


def test_resolve_department_returns_single_match() -> None:
	source = FakeSource(
		candidate_rows=[{"dept_id": "d3", "name": "采购部", "corp_id": "corp-1", "is_current": True}]
	)

	result = service.resolve_department(source, "采购部")

	assert result is not None
	assert result["dept_id"] == "d3"
	assert result["department"] == "采购部"


def test_efficiency_returns_both_tables() -> None:
	source = FakeSource(instances=[{"process_instance_id": "i"}], tasks=[{"task_id": "t"}])

	result = service.get_approval_efficiency(source)

	assert result == {"instances": [{"process_instance_id": "i"}], "tasks": [{"task_id": "t"}]}
