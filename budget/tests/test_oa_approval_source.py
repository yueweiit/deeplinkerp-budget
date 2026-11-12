from __future__ import annotations

from typing import Any

import pytest

from budget.integrations.oa_approval_source import (
	OAApprovalSource,
	OAApprovalSourceConfig,
	OAApprovalSourceScopeError,
	postgres_connection_kwargs,
)


class FakeCursor:
	def __init__(self, rows: list[dict], log: list[tuple[str, tuple]]):
		self._rows = rows
		self._log = log

	def __enter__(self) -> FakeCursor:
		return self

	def __exit__(self, *exc_info: Any) -> bool:
		return False

	def execute(self, sql: str, params: tuple) -> None:
		self._log.append((sql, params))

	def fetchall(self) -> list[dict]:
		return list(self._rows)


class FakeConnection:
	def __init__(self, rows: list[dict], log: list[tuple[str, tuple]]):
		self._rows = rows
		self._log = log
		self.closed = False

	def cursor(self) -> FakeCursor:
		return FakeCursor(self._rows, self._log)

	def close(self) -> None:
		self.closed = True


class FakeConnect:
	def __init__(self, rows: list[dict] | None = None):
		self.rows = rows or []
		self.log: list[tuple[str, tuple]] = []
		self.kwargs: list[dict] = []
		self.connection: FakeConnection | None = None

	def __call__(self, kwargs: dict) -> FakeConnection:
		self.kwargs.append(kwargs)
		self.connection = FakeConnection(self.rows, self.log)
		return self.connection


CONFIG = OAApprovalSourceConfig(
	host="db.example",
	database="dingtalk_oa",
	user="reader",
	password="pw",
	corp_id="corp-1",
)


def test_connection_kwargs_enable_read_only_session() -> None:
	kwargs = postgres_connection_kwargs(CONFIG)

	assert kwargs["dbname"] == "dingtalk_oa"
	assert "default_transaction_read_only=on" in kwargs["options"]
	assert "statement_timeout=10000" in kwargs["options"]


def test_connection_kwargs_can_opt_out_of_read_only() -> None:
	kwargs = postgres_connection_kwargs(CONFIG, read_only=False)

	assert "default_transaction_read_only" not in kwargs["options"]


def test_budget_detail_skips_snapshot_join() -> None:
	connect = FakeConnect([{"process_instance_id": "inst-1"}])
	source = OAApprovalSource(CONFIG, connect=connect)

	source.get_instance_row("inst-1", with_snapshot=False)

	sql = connect.log[0][0]
	assert "snapshot_user_name" not in sql
	assert "ding_user_snapshot" not in sql


def test_expense_sync_detail_joins_user_snapshot() -> None:
	connect = FakeConnect([{"process_instance_id": "inst-1", "snapshot_user_name": "张三"}])
	source = OAApprovalSource(CONFIG, connect=connect)

	row = source.get_instance_row("B-1")

	assert row is not None
	assert row["snapshot_user_name"] == "张三"
	assert "ding_user_snapshot" in connect.log[0][0]
	assert connect.log[0][1] == ("corp-1", "B-1", "B-1", "B-1")


def test_detail_returns_none_when_not_found() -> None:
	connect = FakeConnect([])
	source = OAApprovalSource(CONFIG, connect=connect)

	assert source.get_instance_row("missing") is None


def test_department_snapshots_skip_query_for_empty_input() -> None:
	connect = FakeConnect()
	source = OAApprovalSource(CONFIG, connect=connect)

	assert source.list_department_snapshot_rows([]) == []
	assert connect.kwargs == []


def test_department_snapshots_pass_id_array() -> None:
	connect = FakeConnect([{"dept_id": "d3"}])
	source = OAApprovalSource(CONFIG, connect=connect)

	source.list_department_snapshot_rows(["d3", " d4 ", ""])

	assert connect.log[0][1] == ("corp-1", ["d3", "d4"])
	assert "current_matches" in connect.log[0][0]
	assert "corp_id = %s" in connect.log[0][0]


def test_efficiency_query_returns_both_tables() -> None:
	connect = FakeConnect([{"process_instance_id": "inst-1"}])
	source = OAApprovalSource(CONFIG, connect=connect)

	instances, tasks = source.list_approval_efficiency_rows(start_date="2026-11-01", end_date="2026-11-30")

	assert instances == [{"process_instance_id": "inst-1"}]
	assert tasks == [{"process_instance_id": "inst-1"}]
	assert connect.log[0][1] == ("corp-1", "2026-11-01", "2026-11-01", "2026-11-30", "2026-11-30")
	assert connect.log[1][1] == ("corp-1", "2026-11-01", "2026-11-01", "2026-11-30", "2026-11-30")


def test_every_query_is_scoped_to_one_corp() -> None:
	"""OA 库是多企业共库：任一查询漏掉 corp_id 都会把第二家企业的数据混进来。"""
	connect = FakeConnect([])
	source = OAApprovalSource(CONFIG, connect=connect)

	source.get_instance_row("inst-1")
	source.list_departments()
	source.list_departments(current_only=False)
	source.list_department_candidates("采购部")
	source.list_department_snapshot_rows(["d3"])
	source.list_approval_efficiency_rows()

	assert len(connect.log) == 7  # 效率报表是 instances + tasks 两条 SQL
	for sql, params in connect.log:
		assert "corp_id = %s" in sql
		assert params[0] == "corp-1"


def test_missing_corp_scope_refuses_to_query() -> None:
	"""未配置企业范围时必须报错，不能退化成读全库。"""
	connect = FakeConnect([])
	source = OAApprovalSource(
		OAApprovalSourceConfig(host="db.example", database="dingtalk_oa"), connect=connect
	)

	with pytest.raises(OAApprovalSourceScopeError):
		source.list_departments()

	assert connect.kwargs == []
