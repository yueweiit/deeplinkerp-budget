"""OA 审批数据源选择：默认走 ``dingtalk-oa`` 的只读接口，PostgreSQL 仅作应急回退。

开关与预算台账共用一套（见 ``budget_factory``）：``budget_data_source``（旧键
``budget_oa_source``），默认 ``http``。两种实现暴露同一组方法：
``list_departments`` / ``list_department_candidates`` / ``get_instance_row`` /
``list_approval_efficiency_rows`` / ``list_department_snapshot_rows``。

HTTP 版本现在也覆盖了部门树（``/api/v1/budget/departments*``），因此默认模式下
budget app 不会再建立任何 PostgreSQL 连接。

调用方统一用 ``get_oa_source()``，不要直接实例化具体实现。
"""

from __future__ import annotations

from .budget_factory import use_http_sources
from .oa_api_source import OAAPISource
from .oa_approval_source import OAApprovalSource


def use_http_source() -> bool:
	"""只有 ``http`` 模式才走接口；``local`` 模式下审批数据尚未本地化，仍读 PostgreSQL。"""
	return use_http_sources()


def get_oa_source() -> OAApprovalSource | OAAPISource:
	return OAAPISource() if use_http_source() else OAApprovalSource()
