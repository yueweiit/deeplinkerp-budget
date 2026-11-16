"""可视化报表聚合：预算 + 实际支出 + 审批效率。

口径对齐 ``dingtalk-budget``：

- 预算行复用 ``BudgetLedgerSource.list_budget_entries``（已按部门+月份去重、
  已排除无效状态），金额取 ``total_amount``；
- 支出取自 ``ExpenseSource``（六个分支，见 ``integrations/expense_source.py``）；
- 执行率 = 实际支出 / (生产预算 + 非生产预算)，与原系统
  ``chartHelpers.js: buildSummaryStats`` 一致；
- 审批效率超时标准 24 小时，``services/approval-efficiency.js`` 口径。
"""

from __future__ import annotations

from collections.abc import Iterable
from datetime import UTC, datetime, timezone
from typing import Any

from ..integrations.budget_factory import (
	get_budget_expense_source,
	get_expense_source,
	get_ledger_source,
)
from ..integrations.budget_ledger_source import NON_PRODUCTION, PRODUCTION
from ..integrations.oa_factory import get_oa_source

TIMEOUT_HOURS = 24
BATCH = 100


# ---------------------------------------------------------------- 工具
def _month_of(value: Any) -> str:
	return str(value or "")[:7]


def _to_datetime(value: Any) -> datetime | None:
	if not value:
		return None
	if isinstance(value, datetime):
		return value
	text = str(value).strip().replace(" ", "T")
	try:
		return datetime.fromisoformat(text)
	except ValueError:
		return None


def _hours(start: Any, end: Any) -> float | None:
	start_at, end_at = _to_datetime(start), _to_datetime(end)
	if not start_at or not end_at:
		return None
	# 数据库返回的时间可能有时区也可能没有，统一按 UTC 比较
	if start_at.tzinfo is None:
		start_at = start_at.replace(tzinfo=UTC)
	if end_at.tzinfo is None:
		end_at = end_at.replace(tzinfo=UTC)
	if end_at < start_at:
		return None
	return round((end_at - start_at).total_seconds() / 3600, 2)


def _average(values: Iterable[float]) -> float:
	items = [value for value in values if value is not None]
	return round(sum(items) / len(items), 2) if items else 0.0


def _round(value: float) -> float:
	return round(float(value or 0), 2)


def _date_in_range(value: Any, start_date: Any, end_date: Any) -> bool:
	"""按 YYYY-MM-DD 前缀比较，避免依赖 date 对象/时区。"""
	text = str(value or "")[:10]
	if not text:
		return False
	if start_date and text < str(start_date)[:10]:
		return False
	if end_date and text > str(end_date)[:10]:
		return False
	return True


def _short_department(name: str) -> str:
	"""把「集团-公司-中心」路径压成最后一段，供坐标轴标签使用。"""
	text = str(name or "").strip()
	last = text.split("/")[-1]
	for sep in ("-", "—"):
		last = last.split(sep)[-1]
	return last.strip() or text


def _department_of(row: dict) -> tuple[str, str]:
	"""返回 (展示名, 轴标签短名)。

	源项目 ``departmentDisplayName`` 优先展示完整部门名（``deptName``），空值回退到
	``department_display``，最终兜底「未知部门」；分组也按展示名，与报表口径一致。
	"""
	display = str(row.get("dept_name") or "").strip()
	short = str(row.get("department_display") or "").strip()
	name = (
		display
		or short
		or str(row.get("department") or "").strip()
		or "未知部门"
	)
	return name, short or _short_department(name)


def _region_label(value: Any) -> str:
	"""把 ``execution_region`` 的原始值归一到图表标签（中国 / 墨西哥）。"""
	text = str(value or "").strip()
	if not text:
		return ""
	lowered = text.lower()
	if "中国" in text or "china" in lowered or lowered == "cn":
		return "中国"
	if "墨西哥" in text or "mexico" in lowered or lowered == "mx":
		return "墨西哥"
	return text


# ---------------------------------------------------------------- 预算
def _all_budget_rows(source: Any) -> list[dict]:
	rows: list[dict] = []
	page = 1
	while True:
		data = source.list_budget_entries(page=page, page_size=BATCH)
		batch = data.get("rows") or []
		rows.extend(batch)
		if not batch or len(rows) >= int(data.get("total") or 0):
			break
		page += 1
	return rows


def _match_budget_department(expense_department: str, budget_departments: list[str]) -> str | None:
	"""支出部门是短名（如「广州凌翔」），预算是带路径的全名，按包含关系匹配。"""
	text = str(expense_department or "").strip()
	if not text:
		return None
	for name in budget_departments:
		if text in name or name in text:
			return name
	return None


# ---------------------------------------------------------------- 审批效率
def _approval_efficiency(rows: dict, now: datetime | None = None) -> dict:
	instances = rows.get("instances") or []
	tasks = rows.get("tasks") or []
	now = now or datetime.now()

	measured_instances = []
	for row in instances:
		elapsed = _hours(row.get("create_time"), row.get("finish_time") or now)
		measured_instances.append({**row, "elapsed": elapsed, "overdue": bool(elapsed and elapsed > TIMEOUT_HOURS)})
	completed_hours = [
		row["elapsed"] for row in measured_instances if row.get("finish_time") and row["elapsed"] is not None
	]
	measurable = [row for row in measured_instances if row["elapsed"] is not None]
	overdue_instances = sum(1 for row in measurable if row["overdue"])

	measured_tasks = []
	for task in tasks:
		elapsed = _hours(task.get("start_time"), task.get("end_time") or now)
		if elapsed is None:
			continue
		measured_tasks.append({**task, "elapsed": elapsed, "overdue": elapsed > TIMEOUT_HOURS})
	overdue_tasks = sum(1 for task in measured_tasks if task["overdue"])

	by_process: dict[str, dict] = {}
	instance_map = {row.get("process_instance_id"): row for row in measured_instances}
	for row in measured_instances:
		code = row.get("process_code") or "unknown"
		bucket = by_process.setdefault(
			code,
			{
				"processCode": code,
				"processName": row.get("process_name") or code,
				"totalInstances": 0,
				"completedInstances": 0,
				"runningInstances": 0,
				"overdueInstances": 0,
				"completionHours": [],
				"taskHours": [],
				"overdueTasks": 0,
			},
		)
		bucket["totalInstances"] += 1
		if row.get("finish_time"):
			bucket["completedInstances"] += 1
			if row["elapsed"] is not None:
				bucket["completionHours"].append(row["elapsed"])
		else:
			bucket["runningInstances"] += 1
		if row["overdue"]:
			bucket["overdueInstances"] += 1

	for task in measured_tasks:
		instance = instance_map.get(task.get("process_instance_id")) or {}
		code = instance.get("process_code") or task.get("process_code") or "unknown"
		bucket = by_process.get(code)
		if not bucket:
			continue
		bucket["taskHours"].append(task["elapsed"])
		if task["overdue"]:
			bucket["overdueTasks"] += 1

	process_rows = []
	for bucket in by_process.values():
		total = bucket["totalInstances"]
		task_total = len(bucket["taskHours"])
		process_rows.append(
			{
				"processName": bucket["processName"],
				"totalInstances": total,
				"completedInstances": bucket["completedInstances"],
				"runningInstances": bucket["runningInstances"],
				"averageCompletionHours": _average(bucket["completionHours"]),
				"averageTaskHours": _average(bucket["taskHours"]),
				"overdueTasks": bucket["overdueTasks"],
				"overdueRate": round(bucket["overdueInstances"] / total * 100, 2) if total else 0,
				"taskOverdueRate": round(bucket["overdueTasks"] / task_total * 100, 2) if task_total else 0,
			}
		)
	process_rows.sort(key=lambda row: row["totalInstances"], reverse=True)

	return {
		"timeoutHours": TIMEOUT_HOURS,
		"summary": {
			"totalInstances": len(instances),
			"completedInstances": sum(1 for row in measured_instances if row.get("finish_time")),
			"runningInstances": sum(1 for row in measured_instances if not row.get("finish_time")),
			"averageCompletionHours": _average(completed_hours),
			"overdueRate": round(overdue_instances / len(measurable) * 100, 2) if measurable else 0,
			"taskOverdueRate": round(overdue_tasks / len(measured_tasks) * 100, 2) if measured_tasks else 0,
			"averageTaskHours": _average(task["elapsed"] for task in measured_tasks),
		},
		"byProcess": process_rows[:15],
	}


# ---------------------------------------------------------------- 主流程
def build_report(start_date: str, end_date: str) -> dict:
	start_month, end_month = _month_of(start_date), _month_of(end_date)
	ledger = get_ledger_source()
	# 趋势按自然年展示，所以保留全量行；KPI / 部门图再用区间过滤后的 rows
	all_rows = _all_budget_rows(ledger)
	rows = [
		row
		for row in all_rows
		if (not start_month or _month_of(row.get("budget_month")) >= start_month)
		and (not end_month or _month_of(row.get("budget_month")) <= end_month)
	]
	# 起止同年才做全年趋势，跨年区间退回区间序列，避免拼出两段年份
	start_year, end_year = start_month[:4], end_month[:4]
	trend_year = start_year if start_year and start_year == end_year else ""

	production_rows = [row for row in rows if row.get("budget_kind") == PRODUCTION]
	operation_rows = [row for row in rows if row.get("budget_kind") != PRODUCTION]
	production_total = _round(sum(float(row.get("amount") or 0) for row in production_rows))
	non_production_total = _round(sum(float(row.get("amount") or 0) for row in operation_rows))

	details = ledger.count_detail_rows(start_month or None, end_month or None)

	# 支出库与审批库是独立连接，任一不可用时仍要出预算部分，避免整页报错
	warnings: list[str] = []
	# 趋势要画全年，所以支出按年取；KPI 仍只用筛选区间内的行
	expense_start, expense_end = start_date, end_date
	if trend_year:
		expense_start, expense_end = f"{trend_year}-01-01", f"{trend_year}-12-31"
	try:
		year_expenses = get_expense_source().list_approved_expenses(expense_start, expense_end)
	except Exception as exc:  # 数据源不可用时降级
		year_expenses = []
		warnings.append(f"实际支出读取失败：{exc}")
	expenses = (
		[row for row in year_expenses if _date_in_range(row.get("accounting_date"), start_date, end_date)]
		if trend_year
		else year_expenses
	)
	approved_total = _round(sum(float(row.get("amount") or 0) for row in expenses))

	# 部门 → 预算金额（按期间），用于「有提交预算部门支出」与部门对比图
	budget_by_department: dict[str, float] = {}
	budget_pairs: set[tuple[str, str]] = set()
	# 部门 id → 展示名：支出行带 department_id，优先按 id 匹配，避免长/短部门名对不上
	budget_department_by_id: dict[str, str] = {}
	# 展示名 → 部门 id：图表点击后用 id 跳转台账，比按名称模糊匹配准确
	department_dept_ids: dict[str, str] = {}
	# 部门 → 生产/非生产预算 + 执行地区，供「部门预算分布 / 地区分布 / 执行状态」三张图复用
	dept_stats: dict[str, dict] = {}
	for row in rows:
		# 展示名对齐源项目：dept_name 是完整路径（悦为集团-公司-部门），department_display
		# 只有短名；两者都为空的行（个别老单据）归到「未知部门」，不能丢弃，否则部门图和
		# 金额都会少算（11 月 6 行里就有 2 行没有 display）。
		department, short_name = _department_of(row)
		amount = float(row.get("amount") or 0)
		budget_by_department[department] = budget_by_department.get(department, 0.0) + amount
		budget_pairs.add((department, _month_of(row.get("budget_month"))))
		dept_id = str(row.get("dept_id") or "").strip()
		if dept_id:
			budget_department_by_id[dept_id] = department
			department_dept_ids.setdefault(department, dept_id)
		stats = dept_stats.setdefault(
			department,
			{
				"production": 0.0,
				"nonProduction": 0.0,
				"region": "",
				"short": short_name,
			},
		)
		if row.get("budget_kind") == PRODUCTION:
			stats["production"] += amount
		else:
			stats["nonProduction"] += amount
			# 支出主要走非生产预算，用它的执行地区代表部门归属地区
			if not stats["region"]:
				stats["region"] = _region_label(row.get("execution_region"))
	budget_departments = list(budget_by_department)

	approved_by_department: dict[str, float] = {}
	budget_submitted_approved = 0.0
	region_expense: dict[str, float] = {}
	for row in expenses:
		amount = float(row.get("amount") or 0)
		# 先按部门 id 精确匹配（支出行带 department_id），匹配不到再退回名称包含匹配
		expense_dept_id = str(row.get("department_id") or "").strip()
		matched = budget_department_by_id.get(expense_dept_id) if expense_dept_id else None
		if not matched:
			matched = _match_budget_department(row.get("department"), budget_departments)
		key = matched or str(row.get("department") or "").strip() or "待确认"
		approved_by_department[key] = approved_by_department.get(key, 0.0) + amount
		if matched and (matched, _month_of(row.get("accounting_date"))) in budget_pairs:
			budget_submitted_approved += amount
		region = (dept_stats.get(matched or key) or {}).get("region") or "待确认"
		region_expense[region] = region_expense.get(region, 0.0) + amount

	budget_total = production_total + non_production_total
	overall_rate = round(approved_total / budget_total * 100, 2) if budget_total else 0

	# 月度趋势：固定看筛选区间所在年份的 1–12 月，不随区间收缩
	trend_source_rows = all_rows if trend_year else rows
	trend_source_expenses = year_expenses if trend_year else expenses
	trend: dict[str, dict] = {}
	if trend_year:
		for month in range(1, 13):
			key = f"{trend_year}-{month:02d}"
			trend[key] = {
				"month": key,
				"production": 0.0,
				"nonProduction": 0.0,
				"actualExpense": 0.0,
			}
	for row in trend_source_rows:
		month = _month_of(row.get("budget_month"))
		if not month or (trend_year and not month.startswith(trend_year)):
			continue
		bucket = trend.setdefault(month, {"month": month, "production": 0.0, "nonProduction": 0.0, "actualExpense": 0.0})
		key = "production" if row.get("budget_kind") == PRODUCTION else "nonProduction"
		bucket[key] += float(row.get("amount") or 0)
	for row in trend_source_expenses:
		month = _month_of(row.get("accounting_date"))
		if not month or (trend_year and not month.startswith(trend_year)):
			continue
		bucket = trend.setdefault(month, {"month": month, "production": 0.0, "nonProduction": 0.0, "actualExpense": 0.0})
		bucket["actualExpense"] += float(row.get("amount") or 0)
	trend_rows = [{**value, **{k: _round(v) for k, v in value.items() if k != "month"}} for _, value in sorted(trend.items())]

	# 部门执行率 + 预算 vs 支出（Top 10）
	execution = []
	for department, budget in budget_by_department.items():
		approved = approved_by_department.get(department, 0.0)
		execution.append(
			{
				"deptId": department_dept_ids.get(department) or "",
				"deptName": department,
				"shortName": (dept_stats.get(department) or {}).get("short")
				or _short_department(department),
				"budget": _round(budget),
				"approved": _round(approved),
				"executionRate": round(approved / budget * 100, 2) if budget else 0,
			}
		)
	# 有支出但没有预算的部门也要出现在执行率图里（执行率记 0，源项目显示为「待确认」）
	for department, approved in approved_by_department.items():
		if department in budget_by_department:
			continue
		execution.append(
			{
				"deptId": "",
				"deptName": department,
				"shortName": _short_department(department),
				"budget": 0.0,
				"approved": _round(approved),
				"executionRate": 0,
			}
		)
	# 执行率图按执行率降序（源项目 buildExecutionRateData），不是按预算额
	execution.sort(key=lambda row: row["executionRate"], reverse=True)

	# 审批中支出（执行状态分布里的「审批中」）；读取失败只降级，不影响其它区块
	pending_by_department: dict[str, float] = {}
	try:
		for row in get_budget_expense_source().load_pending_expenses():
			# 没有部门的在途单据归到「待确认」，不能丢，否则审批中金额会少算
			department = str(row.get("applicant_department") or "").strip() or "待确认"
			matched = _match_budget_department(department, budget_departments)
			key = matched or department
			pending_by_department[key] = pending_by_department.get(key, 0.0) + float(
				row.get("base_currency_amount") or 0
			)
	except Exception as exc:  # 审批库不可用时降级
		warnings.append(f"审批中支出读取失败：{exc}")

	# 各部门预算分布（Top 12）
	dept_distribution = [
		{
			"deptId": department_dept_ids.get(department) or "",
			"deptName": department,
			"shortName": stats.get("short") or _short_department(department),
			"production": _round(stats["production"]),
			"nonProduction": _round(stats["nonProduction"]),
			"total": _round(stats["production"] + stats["nonProduction"]),
		}
		for department, stats in sorted(
			dept_stats.items(),
			key=lambda item: item[1]["production"] + item[1]["nonProduction"],
			reverse=True,
		)[:12]
	]

	# 地区预算分布（预算 vs 支出）
	region_budget: dict[str, float] = {}
	for stats in dept_stats.values():
		region = stats["region"] or "待确认"
		region_budget[region] = (
			region_budget.get(region, 0.0) + stats["production"] + stats["nonProduction"]
		)
	region_distribution = [
		{
			"region": region,
			"budget": _round(region_budget.get(region, 0.0)),
			"expense": _round(region_expense.get(region, 0.0)),
		}
		for region in sorted(
			set(region_budget) | set(region_expense),
			key=lambda name: region_budget.get(name, 0.0),
			reverse=True,
		)
	]

	# 预算类型占比
	type_distribution = [
		{"kind": "production", "value": production_total},
		{"kind": "non_production", "value": non_production_total},
	]

	# 预算执行状态分布（Top 10）：未执行 = 预算 - 已执行 - 审批中
	execution_status = []
	for department, stats in dept_stats.items():
		budget_amount = stats["production"] + stats["nonProduction"]
		executed = approved_by_department.get(department, 0.0)
		in_progress = pending_by_department.get(department, 0.0)
		execution_status.append(
			{
				"deptName": department,
				"shortName": stats.get("short") or _short_department(department),
				"total": _round(budget_amount),
				"executed": _round(executed),
				"inProgress": _round(in_progress),
				"unexecuted": _round(max(0.0, budget_amount - executed - in_progress)),
			}
		)
	# 审批中但没有预算的部门也占一行（源项目里显示为「待确认」），total 记 0，
	# 排序后排在预算部门之后，正好补满 Top 10。
	known_departments = set(dept_stats)
	for department, in_progress in pending_by_department.items():
		if department in known_departments:
			continue
		execution_status.append(
			{
				"deptName": department,
				"shortName": _short_department(department),
				"total": 0.0,
				"executed": 0.0,
				"inProgress": _round(in_progress),
				"unexecuted": 0.0,
			}
		)

	execution_status.sort(key=lambda row: row["total"], reverse=True)

	try:
		instances, tasks = get_oa_source().list_approval_efficiency_rows(
			start_date=start_date, end_date=end_date
		)
		approval = _approval_efficiency({"instances": instances, "tasks": tasks})
	except Exception as exc:  # OA 库不可用时降级
		approval = {"timeoutHours": TIMEOUT_HOURS, "summary": {}, "byProcess": []}
		warnings.append(f"审批效率读取失败：{exc}")

	return {
		"stats": {
			"productionCount": int(details.get(PRODUCTION) or 0),
			"operationCount": int(details.get(NON_PRODUCTION) or 0),
			"productionTotal": production_total,
			"nonProductionTotal": non_production_total,
			"approvedTotal": approved_total,
			"budgetSubmittedApprovedTotal": _round(budget_submitted_approved),
			"overallRate": f"{overall_rate}%",
		},
		"execution": execution[:10],
		"trend": trend_rows,
		"dept_compare": sorted(
			[row for row in execution if row["budget"] > 0],
			key=lambda row: row["budget"] + row["approved"],
			reverse=True,
		)[:10],
		"dept_distribution": dept_distribution,
		"region_distribution": region_distribution,
		"type_distribution": type_distribution,
		"execution_status": execution_status[:10],
		"approval": approval,
		"warnings": warnings,
	}
