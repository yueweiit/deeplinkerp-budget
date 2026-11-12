"""审批数据编排服务：连接数据源 + 应用归因规则，输出可直接返回的结构。

分层约定：本层可以调用 ``integrations/``，但判定逻辑（归因、分页游标、唯一性、分层）
都委托给 ``approval_attribution_service`` / ``organization_service`` 这类纯函数，
保证这些规则能脱库单测。
"""

from __future__ import annotations

from collections.abc import Iterable

from ..integrations.oa_approval_source import OAApprovalSource
from . import approval_attribution_service as attribution
from . import organization_service as organization


def get_approval_instance(
	source: OAApprovalSource, instance_id: str, *, with_snapshot: bool = True
) -> dict | None:
	"""Q4/Q5：取实例详情；``with_snapshot=False`` 走 budget 口径（不做快照补名）。"""
	row = source.get_instance_row(instance_id, with_snapshot=with_snapshot)
	if not row:
		return None
	detail = attribution.build_instance_detail(row)
	if with_snapshot:
		attribution.apply_originator_snapshot(detail, row.get("snapshot_user_name"))
	return detail


def list_organization_options(source: OAApprovalSource) -> dict[str, list[dict]]:
	"""Q6：集团 / 公司 / 部门 三层选项。"""
	return organization.build_organization_options(source.list_departments(current_only=True))


def resolve_department(
	source: OAApprovalSource,
	service_entity: str,
	*,
	corresponding_department: str | None = None,
	dept_code: str | None = None,
) -> dict | None:
	"""Q7：反查唯一部门；零条或多条都返回 None（不猜）。"""
	rows = source.list_department_candidates(
		service_entity,
		corresponding_department=corresponding_department,
		dept_code=dept_code,
	)
	selected = organization.pick_unique_department(rows)
	if not selected:
		return None
	return {
		"dept_id": organization.text_of(selected.get("dept_id")),
		"department": organization.text_of(selected.get("name")),
		"corp_id": organization.text_of(selected.get("corp_id")),
		"path_ids": selected.get("path_ids"),
		"path_names": selected.get("path_names"),
	}


def load_department_snapshots(source: OAApprovalSource, dept_ids: Iterable[str]) -> dict[str, dict]:
	"""Q9：部门路径快照（当前唯一优先，历史唯一回退）。"""
	return organization.normalize_department_snapshots(source.list_department_snapshot_rows(dept_ids))


def get_approval_efficiency(
	source: OAApprovalSource, *, start_date: str | None = None, end_date: str | None = None
) -> dict[str, list[dict]]:
	"""Q8：审批效率明细（实例 + 任务），聚合留给报表层。"""
	instances, tasks = source.list_approval_efficiency_rows(start_date=start_date, end_date=end_date)
	return {"instances": instances, "tasks": tasks}
