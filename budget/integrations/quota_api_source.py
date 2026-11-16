"""预算额度 HTTP 数据源（``dingtalk-oa`` 的 ``/api/v1/budget/quota*``）。

与 ``QuotaSource``（PostgreSQL 直连）同名同签名：
``get_options`` / ``get_summary`` / ``list_quotas``。
"""

from __future__ import annotations

from typing import Any

from .budget_read_api_client import BudgetReadAPIClient


class QuotaAPISource:
	"""``QuotaSource`` 的 HTTP 版本。"""

	def __init__(
		self,
		client: BudgetReadAPIClient | None = None,
		*,
		fallback: Any | None = None,
	):
		self._client = client
		self._fallback = fallback

	def _api(self) -> BudgetReadAPIClient:
		if self._client is None:
			self._client = BudgetReadAPIClient()
		return self._client

	def _pg(self) -> Any:
		if self._fallback is None:
			from .quota_source import QuotaSource

			self._fallback = QuotaSource()
		return self._fallback

	def get_options(self) -> dict:
		return self._api().get_quota_options()

	def get_summary(self, **filters) -> dict:
		return self._api().get_quota_summary(**filters)

	def list_quotas(self, *, page: int = 1, page_size: int = 20, **filters) -> dict:
		return self._api().list_quotas(page=page, page_size=page_size, **filters)
