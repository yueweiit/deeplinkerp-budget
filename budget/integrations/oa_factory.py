"""OA 审批数据源选择。

``budget_oa_source`` 取 ``http`` 时走 ``dingtalk-oa`` 的只读接口
（``GET /api/v1/approvals``），否则直连 PostgreSQL（默认，与原项目一致）。

两种实现暴露同一组方法：``list_departments`` / ``list_department_candidates`` /
``get_instance_row`` / ``list_approval_efficiency_rows`` /
``list_department_snapshot_rows``。HTTP 版本未实现的方法（部门树、用户快照）
会回退到数据库。

调用方统一用 ``get_oa_source()``，不要直接实例化具体实现。
"""

from __future__ import annotations

from .oa_api_source import OAAPISource
from .oa_approval_source import OAApprovalSource, _runtime_config_value

_HTTP_VALUES = {"http", "api", "oa"}


def use_http_source() -> bool:
	value = (_runtime_config_value("budget_oa_source", default="postgres") or "").strip().lower()
	return value in _HTTP_VALUES


def get_oa_source() -> OAApprovalSource | OAAPISource:
	return OAAPISource() if use_http_source() else OAApprovalSource()
