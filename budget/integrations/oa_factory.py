"""OA 审批数据源选择，与预算台账共用 ``budget_data_source`` 开关。

默认 ``local``：台账存本地，OA 读取复用 Budget Settings 指定的连接和企业范围；
显式 ``http`` / ``postgres`` 保持历史配置行为。实现暴露同一组方法：
``list_departments`` / ``list_department_candidates`` / ``get_instance_row`` /
``list_approval_efficiency_rows`` / ``list_department_snapshot_rows``。

调用方统一用 ``get_oa_source()``，不要直接实例化具体实现。
"""

from __future__ import annotations

from .budget_factory import data_source_mode, use_http_sources
from .oa_api_source import OAAPISource
from .oa_approval_source import OAApprovalSource


def use_http_source() -> bool:
	"""只有 ``http`` 模式才走接口；``local`` 模式下审批数据尚未本地化，仍读 PostgreSQL。"""
	return use_http_sources()


def get_oa_source() -> OAApprovalSource | OAAPISource:
	if data_source_mode() == "local":
		from ..services.ledger_sync_settings import get_ledger_oa_source

		return get_ledger_oa_source()
	return OAAPISource() if use_http_source() else OAApprovalSource()
