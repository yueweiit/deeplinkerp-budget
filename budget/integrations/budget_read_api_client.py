"""钉钉 OA 预算只读 HTTP 客户端（消费方）。

上游：``dingtalk-oa`` 新增的 ``/api/v1/budget/*`` 只读接口，本 app 只消费、不修改它。

接口一览（全部返回 ``{success: true, data: ...}``）：

- ``GET /api/v1/budget/entries``：预算台账分页
- ``GET /api/v1/budget/entries/stats``：四张统计卡
- ``GET /api/v1/budget/entries/{expense-details,production-details,remarks,detail-count}``
- ``GET /api/v1/budget/quota/{options,summary,entries}``
- ``GET /api/v1/budget/expenses/{approved,meta,completed,pending,splits,payments}``
- ``GET /api/v1/budget/departments{,/candidates,/snapshots}``

这样 budget app 不再需要任何 PostgreSQL 连接：SQL 口径集中在 dingtalk-oa 侧，
本模块只做 HTTP 调用与错误归一。
"""

from __future__ import annotations

import os
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any

import requests

DEFAULT_TIMEOUT_SECONDS = 30.0
DEFAULT_MAX_RETRIES = 2
DEFAULT_RETRY_BACKOFF_MS = 2000


class BudgetReadAPIError(Exception):
	"""预算只读 API 调用失败；``code`` 为接口返回的稳定错误码。"""

	def __init__(self, message: str, *, code: str | None = None, status_code: int | None = None):
		super().__init__(message)
		self.code = code
		self.status_code = status_code


class BudgetReadRateLimited(BudgetReadAPIError):
	"""触发来源或全局限流；``retry_after`` 为服务端建议的等待秒数。"""

	def __init__(self, message: str = "预算只读 API 限流", *, retry_after: float = 1.0):
		super().__init__(message, code="RATE_LIMITED", status_code=429)
		self.retry_after = retry_after


@dataclass(frozen=True)
class BudgetReadAPIConfig:
	"""只读 API 连接参数。"""

	base_url: str = ""
	corp_id: str = ""
	timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS
	max_retries: int = DEFAULT_MAX_RETRIES
	retry_backoff_ms: int = DEFAULT_RETRY_BACKOFF_MS

	@classmethod
	def from_site_config(cls) -> BudgetReadAPIConfig:
		"""环境变量优先，其次 frappe.conf；地址键兼容既有的 OA 只读接口配置。"""
		return cls(
			base_url=_runtime_config_value(
				"budget_read_api_url",
				"budget_oa_api_url",
				"budget_oa_api_base",
				"operating_expense_oa_api_url",
			),
			corp_id=_runtime_config_value(
				"budget_corp_id", "budget_oa_corp_id", "dingtalk_oa_corp_id", "dingtalk_corp_id"
			),
			timeout_seconds=float(
				_runtime_config_value(
					"budget_read_api_timeout", "budget_oa_api_timeout", default="30"
				)
				or 30
			),
		)


def _runtime_config_value(*keys: str, default: str = "") -> str:
	for key in keys:
		env_value = os.environ.get(key.upper())
		if env_value:
			return str(env_value).strip()
	for key in keys:
		try:
			from frappe import conf
		except ImportError:
			break
		value = conf.get(key)
		if value:
			return str(value).strip()
	return default


class BudgetReadAPIClient:
	"""``/api/v1/budget/*`` 的薄封装：只管请求、退避与错误归一。"""

	def __init__(
		self,
		config: BudgetReadAPIConfig | None = None,
		*,
		session: Any | None = None,
		sleep: Any | None = None,
	):
		self.config = config or BudgetReadAPIConfig.from_site_config()
		if not self.config.base_url:
			raise BudgetReadAPIError(
				"未配置预算只读 API 地址（budget_read_api_url / budget_oa_api_url）"
			)
		self._session = session or requests.Session()
		self._sleep = sleep or _time_sleep

	def _request(self, path: str, params: dict | None = None) -> Any:
		url = f"{self.config.base_url.rstrip('/')}{path}"
		clean = {key: value for key, value in (params or {}).items() if value not in (None, "")}
		attempts = max(0, self.config.max_retries)
		last_error: BudgetReadAPIError | None = None

		for attempt in range(attempts + 1):
			try:
				response = self._session.request(
					"GET", url, params=clean, timeout=self.config.timeout_seconds
				)
			except requests.RequestException as exc:
				last_error = BudgetReadAPIError(f"预算只读 API 请求失败: {exc}")
				if attempt >= attempts:
					raise last_error from exc
				self._sleep(self._backoff_seconds(attempt))
				continue

			payload = _safe_json(response)
			if response.status_code == 429:
				last_error = BudgetReadRateLimited(
					retry_after=_retry_after_seconds(response, self._backoff_seconds(attempt))
				)
				if attempt >= attempts:
					raise last_error
				self._sleep(last_error.retry_after)
				continue

			if response.status_code >= 400 or not payload.get("success"):
				raise BudgetReadAPIError(
					f"预算只读 API 返回失败: {payload.get('code') or response.status_code}",
					code=payload.get("code"),
					status_code=response.status_code,
				)
			return payload.get("data")

		raise last_error or BudgetReadAPIError("预算只读 API 调用失败且没有可用错误信息")

	def _backoff_seconds(self, attempt: int) -> float:
		return self.config.retry_backoff_ms * (2**attempt) / 1000.0

	def _corp_id(self) -> str:
		return str(self.config.corp_id or "").strip()

	# ------------------------------------------------------------ 台账
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
		return self._request(
			"/api/v1/budget/entries",
			{
				"corpId": self._corp_id() or None,
				"budgetType": budget_type,
				"startMonth": start_month,
				"endMonth": end_month,
				"status": status,
				"keyword": keyword,
				"page": page,
				"pageSize": page_size,
			},
		) or {}

	def get_budget_stats(self) -> dict:
		return self._request("/api/v1/budget/entries/stats") or {}

	def list_expense_details(self, form_nos: Iterable[str]) -> list[dict]:
		return self._request(
			"/api/v1/budget/entries/expense-details",
			{"formNos": _joined(form_nos)},
		) or []

	def list_production_details(self, form_nos: Iterable[str]) -> list[dict]:
		return self._request(
			"/api/v1/budget/entries/production-details",
			{"formNos": _joined(form_nos)},
		) or []

	def load_remarks(self, form_nos: Iterable[str]) -> dict:
		return self._request(
			"/api/v1/budget/entries/remarks",
			{"formNos": _joined(form_nos)},
		) or {}

	def count_detail_rows(
		self, start_month: str | None = None, end_month: str | None = None
	) -> dict:
		return self._request(
			"/api/v1/budget/entries/detail-count",
			{"startMonth": start_month, "endMonth": end_month},
		) or {}

	# ------------------------------------------------------------ 额度
	def get_quota_options(self) -> dict:
		return self._request("/api/v1/budget/quota/options") or {}

	def get_quota_summary(self, **filters) -> dict:
		return self._request("/api/v1/budget/quota/summary", _quota_params(filters)) or {}

	def list_quotas(
		self, *, page: int = 1, page_size: int = 20, **filters
	) -> dict:
		params = _quota_params(filters)
		params.update({"page": page, "pageSize": page_size})
		return self._request("/api/v1/budget/quota/entries", params) or {}

	# ------------------------------------------------------------ 支出
	def list_approved_expenses(self, start_date: Any, end_date: Any) -> list[dict]:
		return self._request(
			"/api/v1/budget/expenses/approved",
			{"startDate": _iso(start_date), "endDate": _iso(end_date)},
		) or []

	def load_expense_detail_meta(self, business_ids: Iterable[str]) -> dict:
		return self._request(
			"/api/v1/budget/expenses/meta",
			{"businessIds": _joined(business_ids)},
		) or {}

	def list_completed_expenses(self) -> list[dict]:
		return self._request("/api/v1/budget/expenses/completed") or []

	def list_pending_expenses(self) -> list[dict]:
		return self._request("/api/v1/budget/expenses/pending") or []

	def load_expense_splits(self) -> dict:
		return self._request("/api/v1/budget/expenses/splits") or {}

	def load_expense_payments(self) -> dict:
		return self._request("/api/v1/budget/expenses/payments") or {}

	# ------------------------------------------------------------ 部门树
	def list_departments(self, *, current_only: bool = True) -> list[dict]:
		return self._request(
			"/api/v1/budget/departments",
			{"corpId": self._corp_id(), "currentOnly": "true" if current_only else "false"},
		) or []

	def list_department_candidates(
		self,
		service_entity: str,
		*,
		corresponding_department: str | None = None,
		dept_code: str | None = None,
	) -> list[dict]:
		return self._request(
			"/api/v1/budget/departments/candidates",
			{
				"corpId": self._corp_id(),
				"serviceEntity": service_entity,
				"correspondingDepartment": corresponding_department,
				"deptCode": dept_code,
			},
		) or []

	def list_department_snapshot_rows(self, dept_ids: Iterable[str]) -> list[dict]:
		return self._request(
			"/api/v1/budget/departments/snapshots",
			{"corpId": self._corp_id(), "deptIds": _joined(dept_ids)},
		) or []


def _quota_params(filters: dict) -> dict:
	return {
		"budgetYear": filters.get("budget_year"),
		"budgetMonth": filters.get("budget_month"),
		"groupDeptId": filters.get("group_dept_id"),
		"companyDeptId": filters.get("company_dept_id"),
		"departmentId": filters.get("department_id"),
		"budgetType": filters.get("budget_type"),
		"subjectName": filters.get("subject_name"),
	}


def _joined(values: Iterable[str]) -> str:
	names = [str(value).strip() for value in values or [] if str(value or "").strip()]
	return ",".join(dict.fromkeys(names))


def _iso(value: Any) -> str:
	return value.isoformat() if hasattr(value, "isoformat") else str(value)


def _time_sleep(seconds: float) -> None:
	import time

	time.sleep(max(0.0, seconds))


def _safe_json(response: Any) -> dict:
	try:
		payload = response.json()
	except ValueError:
		return {"success": False, "code": "INVALID_RESPONSE"}
	return payload if isinstance(payload, dict) else {"success": False, "code": "INVALID_RESPONSE"}


def _retry_after_seconds(response: Any, fallback: float) -> float:
	raw = (getattr(response, "headers", None) or {}).get("Retry-After")
	try:
		return max(0.0, float(raw))
	except (TypeError, ValueError):
		return fallback
