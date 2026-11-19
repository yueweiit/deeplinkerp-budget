from __future__ import annotations

import pytest

from budget.services import report_service


@pytest.fixture(autouse=True)
def legacy_report_context(monkeypatch):
	monkeypatch.setattr(report_service, "_", lambda text, *a, **kw: text)
	monkeypatch.setattr(report_service, "current_report_source", lambda: None)


class FakeLedger:
	"""只提供 build_report 用到的两个方法。"""

	def __init__(self, rows: list[dict]):
		self.rows = rows

	def list_budget_entries(self, *, page=1, page_size=10, **_kwargs):
		return {"rows": self.rows if page == 1 else [], "total": len(self.rows)}

	def count_detail_rows(self, start_month=None, end_month=None):
		return {"production": 0, "non_production": len(self.rows)}


class FakeExpense:
	def __init__(self, rows: list[dict]):
		self.rows = rows

	def list_approved_expenses(self, start_date=None, end_date=None):
		return self.rows


@pytest.fixture
def patch_sources(monkeypatch):
	def apply(
		budget_rows: list[dict],
		expense_rows: list[dict],
		pending_rows: list[dict] | None = None,
	) -> None:
		monkeypatch.setattr(report_service, "get_ledger_source", lambda: FakeLedger(budget_rows))
		monkeypatch.setattr(report_service, "get_expense_source", lambda: FakeExpense(expense_rows))
		# 这里一并替换掉在途支出源，所以测试不要再自己 monkeypatch 同一个名字，
		# 否则会被这行覆盖成空数据（曾经因此让「审批中」金额恒为 0）
		monkeypatch.setattr(
			report_service,
			"get_budget_expense_source",
			lambda: FakePendingExpense(pending_rows or []),
		)

		def no_oa():
			raise RuntimeError("审批数据源不可用")

		monkeypatch.setattr(report_service, "get_oa_source", no_oa)

	return apply


class FakePendingExpense:
	def __init__(self, rows: list[dict] | None = None):
		self.rows = rows or []

	def load_pending_expenses(self):
		return self.rows


def test_build_report_groups_departments_by_display_name(patch_sources) -> None:
	"""台账行只有 department_display，没有 department；取错字段会让部门图为空。"""
	patch_sources(
		[
			{
				"budget_kind": "non_production",
				"budget_month": "2026-11-01",
				"amount": 1000.0,
				"department_display": "管理规划中心/财务中心",
			}
		],
		[
			{
				"department": "财务中心",
				"amount": 250.0,
				"accounting_date": "2026-11-06",
				"category": "operation",
			}
		],
	)

	data = report_service.build_report("2026-11-01", "2026-11-30")

	# 只比对业务字段：deptId 之类新增的展示字段不该让用例跟着改
	assert [
		{key: value for key, value in row.items() if key != "deptId"}
		for row in data["execution"]
	] == [
		{
			"deptName": "管理规划中心/财务中心",
			"shortName": "管理规划中心/财务中心",
			"budget": 1000.0,
			"approved": 250.0,
			"executionRate": 25.0,
		}
	]
	assert data["stats"]["budgetSubmittedApprovedTotal"] == 250.0
	assert data["warnings"]  # 审批数据源不可用时要有提示，但不影响预算/支出


def test_build_report_keeps_rows_without_department(patch_sources) -> None:
	"""空部门的老单据要归到「未知部门」，不能丢——否则部门图数量与金额都会少算。"""
	patch_sources(
		[
			{
				"budget_kind": "non_production",
				"budget_month": "2026-11-01",
				"amount": 300.0,
				"dept_name": "悦为集团-拉丁购",
			},
			{
				"budget_kind": "non_production",
				"budget_month": "2026-11-01",
				"amount": 200.0,
			},
		],
		[],
	)

	data = report_service.build_report("2026-11-01", "2026-11-30")

	names = [(row["deptName"], row["shortName"]) for row in data["dept_distribution"]]
	assert names == [("悦为集团-拉丁购", "拉丁购"), ("未知部门", "未知部门")]


def test_execution_status_includes_pending_departments_without_budget(
	patch_sources,
) -> None:
	"""在途支出没有对应预算部门时也要单独成行（源项目显示为「待确认」）。"""

	patch_sources(
		[
			{
				"budget_kind": "non_production",
				"budget_month": "2026-11-01",
				"amount": 1000.0,
				"dept_name": "财务中心",
			}
		],
		[],
		pending_rows=[
			{"applicant_department": "财务中心", "base_currency_amount": 100.0},
			{"applicant_department": "无预算部门", "base_currency_amount": 300.0},
			{"applicant_department": None, "base_currency_amount": 50.0},
		],
	)

	data = report_service.build_report("2026-11-01", "2026-11-30")
	rows = {row["deptName"]: row for row in data["execution_status"]}

	assert rows["财务中心"]["inProgress"] == 100.0
	assert rows["财务中心"]["unexecuted"] == 900.0
	assert rows["无预算部门"]["total"] == 0.0
	assert rows["无预算部门"]["inProgress"] == 300.0
	assert rows["待确认"]["inProgress"] == 50.0


def test_build_report_falls_back_to_department_column(patch_sources) -> None:
	"""老数据/替身仍可能只有 department，保持兼容。"""
	patch_sources(
		[
			{
				"budget_kind": "non_production",
				"budget_month": "2026-11-01",
				"amount": 500.0,
				"department": "财务中心",
			}
		],
		[],
	)

	data = report_service.build_report("2026-11-01", "2026-11-30")

	assert [row["deptName"] for row in data["execution"]] == ["财务中心"]


def test_unavailable_expenses_preserve_budget_but_hide_expense_dependent_metrics(patch_sources, monkeypatch):
	patch_sources([{"budget_kind": "non_production", "budget_month": "2026-11-01",
		"amount": 1000, "department": "Finance"}], [])
	def unavailable():
		raise RuntimeError("source binding mismatch")
	monkeypatch.setattr(report_service, "get_expense_source", unavailable)
	data = report_service.build_report("2026-11-01", "2026-11-30")
	assert data["availability"]["expenses"] is False
	assert data["availability"]["approval"] is False
	assert data["stats"]["nonProductionTotal"] == 1000
	for key in ("approvedTotal", "budgetSubmittedApprovedTotal", "overallRate"):
		assert data["stats"][key] is None
	assert all(row["actualExpense"] is None for row in data["trend"])
	assert all(row["expense"] is None for row in data["region_distribution"])
	for key in ("execution", "execution_status", "dept_compare"):
		assert data[key] == []
	assert data["dept_distribution"]


def test_unavailable_pending_expenses_do_not_become_unexecuted_budget(patch_sources, monkeypatch):
	patch_sources([{"budget_kind": "non_production", "budget_month": "2026-11-01",
		"amount": 1000, "department": "Finance"}], [])
	def unavailable():
		raise RuntimeError("pending expenses offline")
	monkeypatch.setattr(report_service, "get_budget_expense_source", unavailable)
	data = report_service.build_report("2026-11-01", "2026-11-30")
	assert data["availability"]["expenses"] is True
	assert data["availability"]["pending_expenses"] is False
	assert data["stats"]["approvedTotal"] == 0
	assert data["execution_status"] == []
	assert data["execution"]  # Confirmed expenses remain readable.
