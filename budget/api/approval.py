"""审批查询接口（只读）。

命名契约：``get_`` / ``list_`` 前缀表示无副作用，前端可安全重试；
写接口不在本模块，等同步账本 DocType 落地后放到独立的写接口模块。
"""

from __future__ import annotations

import frappe

from ..integrations.oa_factory import get_oa_source
from ..services import approval_query_service

READ_ROLES = ("System Manager", "Accounts Manager")


def get_source():
	"""同一请求内复用数据源实例（连接不池化，按需新建/关闭）。

	数据源由 ``budget_oa_source`` 决定：``postgres``（默认）直连库，``http`` 走
	``dingtalk-oa`` 的只读接口；两者方法签名一致，调用方无感知。
	"""
	source = getattr(frappe.local, "budget_approval_source", None)
	if source is None:
		source = get_oa_source()
		frappe.local.budget_approval_source = source
	return source


def _require_read_permission() -> None:
	frappe.only_for(READ_ROLES)


@frappe.whitelist()
def get_approval_instance(instance_id: str, with_snapshot: bool = True) -> dict | None:
	"""Q4/Q5：按实例 ID 或 businessId 取详情；不存在返回 None。"""
	_require_read_permission()
	return approval_query_service.get_approval_instance(
		get_source(), instance_id, with_snapshot=bool(int(with_snapshot))
	)


@frappe.whitelist()
def list_organization_options() -> dict:
	"""Q6：集团 / 公司 / 部门 三层选项。"""
	_require_read_permission()
	return approval_query_service.list_organization_options(get_source())


@frappe.whitelist()
def get_department_by_service_entity(
	service_entity: str,
	corresponding_department: str | None = None,
	dept_code: str | None = None,
) -> dict | None:
	"""Q7：按业务主体/对应部门/部门编码反查唯一部门，不唯一返回 None。"""
	_require_read_permission()
	return approval_query_service.resolve_department(
		get_source(),
		service_entity,
		corresponding_department=corresponding_department,
		dept_code=dept_code,
	)


@frappe.whitelist()
def list_department_snapshots(dept_ids: list[str] | str) -> dict:
	"""Q9：批量取部门路径快照（当前唯一优先，历史唯一回退）。"""
	_require_read_permission()
	ids = frappe.parse_json(dept_ids) if isinstance(dept_ids, str) else dept_ids
	return approval_query_service.load_department_snapshots(get_source(), ids or [])


@frappe.whitelist()
def get_approval_efficiency(start_date: str | None = None, end_date: str | None = None) -> dict:
	"""Q8：审批效率明细（实例 + 任务两张表）。"""
	_require_read_permission()
	return approval_query_service.get_approval_efficiency(
		get_source(), start_date=start_date or None, end_date=end_date or None
	)
