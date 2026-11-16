"""预算台账 HTTP 数据源（``dingtalk-oa`` 的 ``/api/v1/budget/entries*``）。

与 ``BudgetLedgerSource``（PostgreSQL 直连）同名同签名：
``list_budget_entries`` / ``get_stats`` / ``list_expense_details`` /
``list_production_details`` / ``load_remarks`` / ``count_detail_rows``。

口径、部门树映射、去重规则全部在 dingtalk-oa 侧实现，本类只负责搬运，
因此 budget app 不再需要预算库连接。
"""

from __future__ import annotations

from typing import Any

from .budget_read_api_client import BudgetReadAPIClient


class BudgetLedgerAPISource:
	"""``BudgetLedgerSource`` 的 HTTP 版本。"""

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
		"""只在开关显式切回 ``postgres`` 时才使用（应急回退）。"""
		if self._fallback is None:
			from .budget_ledger_source import BudgetLedgerSource

			self._fallback = BudgetLedgerSource()
		return self._fallback

	# ------------------------------------------------------------ 列表
	def list_budget_entries(
		self,
		*,
		budget_type: str | None = None,
		start_month: str | None = None,
		end_month: str | None = None,
		status: str | None = None,
		keyword: str | None = None,
		page: int = 1,
		page_size: int = 10,
	) -> dict:
		return self._api().list_budget_entries(
			budget_type=budget_type,
			start_month=start_month,
			end_month=end_month,
			status=status,
			keyword=keyword,
			page=page,
			page_size=page_size,
		)

	# ------------------------------------------------------------ 统计
	def get_stats(self) -> dict:
		return self._api().get_budget_stats()

	# ------------------------------------------------------------ 明细
	def list_expense_details(self, form_nos: list[str]) -> list[dict]:
		return self._api().list_expense_details(form_nos)

	def list_production_details(self, form_nos: list[str]) -> list[dict]:
		return self._api().list_production_details(form_nos)

	def load_remarks(self, form_nos: list[str]) -> dict[str, str]:
		return self._api().load_remarks(form_nos)

	def count_detail_rows(
		self, start_month: str | None = None, end_month: str | None = None
	) -> dict[str, int]:
		return self._api().count_detail_rows(start_month, end_month)
