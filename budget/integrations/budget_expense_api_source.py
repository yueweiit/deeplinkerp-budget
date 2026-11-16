"""报表用实际支出 HTTP 数据源（``dingtalk-oa`` 的 ``/api/v1/budget/expenses*``）。

与 ``BudgetExpenseSource``（PostgreSQL 直连）同名同签名：
``load_expenses`` / ``load_pending_expenses`` / ``load_splits`` / ``load_payments``。
"""

from __future__ import annotations

from typing import Any

from .budget_read_api_client import BudgetReadAPIClient


class BudgetExpenseAPISource:
	"""``BudgetExpenseSource`` 的 HTTP 版本。"""

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
			from .budget_expense_source import BudgetExpenseSource

			self._fallback = BudgetExpenseSource()
		return self._fallback

	def load_expenses(self) -> list[dict]:
		return self._api().list_completed_expenses()

	def load_pending_expenses(self) -> list[dict]:
		return self._api().list_pending_expenses()

	def load_splits(self) -> dict[str, list[dict]]:
		return self._api().load_expense_splits()

	def load_payments(self) -> dict[str, list[dict]]:
		return self._api().load_expense_payments()
