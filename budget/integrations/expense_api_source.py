"""实际支出 HTTP 数据源（``dingtalk-oa`` 的 ``/api/v1/budget/expenses*``）。

与 ``ExpenseSource``（PostgreSQL 直连）同名同签名：
``list_approved_expenses`` / ``load_detail_meta``。
"""

from __future__ import annotations

from datetime import date
from typing import Any

from .budget_read_api_client import BudgetReadAPIClient


class ExpenseAPISource:
	"""``ExpenseSource`` 的 HTTP 版本。"""

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
			from .expense_source import ExpenseSource

			self._fallback = ExpenseSource()
		return self._fallback

	def list_approved_expenses(self, start_date: date | str, end_date: date | str) -> list[dict]:
		return self._api().list_approved_expenses(start_date, end_date)

	def load_detail_meta(self, business_ids: list[str]) -> dict[str, dict]:
		return self._api().load_expense_detail_meta(business_ids)
