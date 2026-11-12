"""钉钉 OA 只读 HTTP API 客户端（消费方）。

上游服务：`yueweiit/dingtalk-oa`，本 app 只消费、不修改它。

接口契约（`docs/modules/approval-read-api/README.md`）：
- ``GET /api/v1/approvals``：审批快照，按 ``(updated_at, process_instance_id)`` 排序，
  支持 ``corpId``（必填）、``processCode``、``processInstanceId``、``approvalNo``、
  ``updatedAfter``（必带时区）、``limit``（1–200）、``cursor``。
- 无鉴权，靠网络隔离；限流按来源 IP 120 req/min，超限返回 429 + ``Retry-After``。
- 游标不透明、24 小时有效；``INVALID_CURSOR`` 必须从上次完成水位重开一轮。

附件与正文附件走 ``/api/v1/attachments``、``/api/v1/attachment-content``，
不在本模块范围内（由既有 `operating_oa_http.py` 承担，避免第三份实现）。
"""

from __future__ import annotations

import os
from collections.abc import Iterator
from dataclasses import dataclass, field
from typing import Any

import requests

DEFAULT_TIMEOUT_SECONDS = 30.0
MAX_PAGE_LIMIT = 200


class OAReadAPIError(Exception):
	"""只读 API 调用失败；``code`` 为接口返回的稳定错误码。"""

	def __init__(self, message: str, *, code: str | None = None, status_code: int | None = None):
		super().__init__(message)
		self.code = code
		self.status_code = status_code


class OAInvalidCursor(OAReadAPIError):
	"""游标失效/被篡改：调用方应丢弃游标，从上次完成水位重开。"""


class OARateLimited(OAReadAPIError):
	"""触发来源或全局限流；``retry_after`` 为服务端建议的等待秒数。"""

	def __init__(self, message: str, *, retry_after: float = 1.0):
		super().__init__(message, code="RATE_LIMITED", status_code=429)
		self.retry_after = retry_after


@dataclass(frozen=True)
class OAReadAPIConfig:
	"""只读 API 连接参数。"""

	base_url: str = ""
	timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS
	max_retries: int = 2
	retry_backoff_ms: int = 2000

	@classmethod
	def from_site_config(cls) -> OAReadAPIConfig:
		"""环境变量优先，其次 frappe.conf；键名兼容既有的经营费用配置。"""
		return cls(
			base_url=_runtime_config_value("budget_oa_api_url", "operating_expense_oa_api_url"),
			timeout_seconds=float(_runtime_config_value("budget_oa_api_timeout", default="30") or 30),
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


@dataclass(frozen=True)
class ApprovalPage:
	"""一页审批快照；``sync_until`` 是本轮读取的数据库时间上界。"""

	rows: list[dict] = field(default_factory=list)
	next_cursor: str | None = None
	sync_until: str | None = None
	has_more: bool = False

	def __bool__(self) -> bool:
		return bool(self.rows)


class OAReadAPIClient:
	"""``/api/v1/approvals`` 的薄封装：只管分页、限流与错误归一。"""

	def __init__(
		self,
		config: OAReadAPIConfig,
		*,
		session: Any | None = None,
		sleep: Any | None = None,
	):
		if not config.base_url:
			raise OAReadAPIError(
				"未配置 OA 只读 API 地址（budget_oa_api_url / operating_expense_oa_api_url）"
			)
		self.config = config
		self._session = session or requests.Session()
		self._sleep = sleep or time_sleep

	def _request(self, path: str, params: dict) -> dict:
		url = f"{self.config.base_url.rstrip('/')}{path}"
		attempts = max(0, self.config.max_retries)
		last_error: OAReadAPIError | None = None

		for attempt in range(attempts + 1):
			try:
				response = self._session.request(
					"GET", url, params=params, timeout=self.config.timeout_seconds
				)
			except requests.RequestException as exc:
				last_error = OAReadAPIError(f"只读 API 请求失败: {exc}")
				if attempt >= attempts:
					raise last_error from exc
				self._sleep(self._backoff_seconds(attempt))
				continue

			payload = _safe_json(response)
			if response.status_code == 429:
				last_error = OARateLimited(
					"只读 API 限流",
					retry_after=_retry_after_seconds(response, self._backoff_seconds(attempt)),
				)
				if attempt >= attempts:
					raise last_error
				self._sleep(last_error.retry_after)
				continue

			if response.status_code == 400 and payload.get("code") == "INVALID_CURSOR":
				raise OAInvalidCursor(
					"游标失效，请从上次完成水位重开", code="INVALID_CURSOR", status_code=400
				)

			if response.status_code >= 400 or not payload.get("success"):
				raise OAReadAPIError(
					f"只读 API 返回失败: {payload.get('code') or response.status_code}",
					code=payload.get("code"),
					status_code=response.status_code,
				)
			return payload

		raise last_error or OAReadAPIError("只读 API 调用失败且没有可用错误信息")

	def _backoff_seconds(self, attempt: int) -> float:
		return self.config.retry_backoff_ms * (2**attempt) / 1000.0

	def list_approvals(
		self,
		corp_id: str,
		*,
		process_code: str | None = None,
		process_instance_id: str | None = None,
		approval_no: str | None = None,
		updated_after: str | None = None,
		limit: int = 50,
		cursor: str | None = None,
	) -> ApprovalPage:
		"""取一页审批快照；调用方负责保存 ``sync_until`` 作为完成水位。"""
		params: dict[str, Any] = {"corpId": corp_id, "limit": _safe_limit(limit)}
		for key, value in (
			("processCode", process_code),
			("processInstanceId", process_instance_id),
			("approvalNo", approval_no),
			("updatedAfter", updated_after),
			("cursor", cursor),
		):
			if value:
				params[key] = value

		payload = self._request("/api/v1/approvals", params)
		pagination = payload.get("pagination") or {}
		return ApprovalPage(
			rows=list(payload.get("data") or []),
			next_cursor=pagination.get("nextCursor"),
			sync_until=pagination.get("syncUntil"),
			has_more=bool(pagination.get("hasMore")),
		)

	def iter_approval_pages(
		self,
		corp_id: str,
		*,
		process_code: str | None = None,
		updated_after: str | None = None,
		limit: int = 50,
	) -> Iterator[ApprovalPage]:
		"""按游标翻页；最后一页的 ``sync_until`` 即本轮水位。"""
		cursor: str | None = None
		while True:
			page = self.list_approvals(
				corp_id,
				process_code=process_code,
				updated_after=updated_after,
				limit=limit,
				cursor=cursor,
			)
			yield page
			if not page.has_more or not page.next_cursor:
				return
			cursor = page.next_cursor

	def get_approval(
		self, corp_id: str, process_instance_id: str, *, by_approval_no: str | None = None
	) -> dict | None:
		"""按实例 ID 或审批编号取单条（接口无详情路由，用精确过滤 + limit 1）。"""
		page = self.list_approvals(
			corp_id,
			process_instance_id=None if by_approval_no else process_instance_id,
			approval_no=by_approval_no,
			limit=1,
		)
		return page.rows[0] if page.rows else None


def time_sleep(seconds: float) -> None:
	import time

	time.sleep(max(0.0, seconds))


def _safe_limit(limit: int) -> int:
	return min(MAX_PAGE_LIMIT, max(1, int(limit or 50)))


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
