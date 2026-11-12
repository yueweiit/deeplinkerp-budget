"""钉钉开放平台客户端：仅保留 token 与机器人消息。

审批数据的拉取由上游 `yueweiit/dingtalk-oa` 负责，本 app 不再直连钉钉查询实例/详情；
这里只剩两种能力：
- access_token（企业应用与机器人两套凭据，各自缓存）；
- 机器人 Markdown 批量发送（预警用）。

对标源码：`dingtalk-budget/server/services/dingtalk.js`（token 缓存、三组熔断）、
`dingtalk-expense-sync/src/dingtalk-robot.ts`（批量发送）。

本模块只负责 HTTP 与重试/熔断，不做字段归因（归因在 ``services/``）。
token 缓存默认走进程内字典；存在 `frappe` 时自动改用 `frappe.cache()`，
与 `draft_notifications/dingtalk_robot.py` 的缓存行为一致。
"""

from __future__ import annotations

import json
import os
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

import requests

QPS_ERROR_CODE = "Forbidden.AccessDenied.QpsLimitForApi"
DEFAULT_OAPI_URL = "https://oapi.dingtalk.com"
DEFAULT_API_URL = "https://api.dingtalk.com"


class DingTalkAPIError(Exception):
	"""钉钉接口调用失败（含业务错误码与网络异常）。"""


class CircuitOpenError(DingTalkAPIError):
	"""熔断打开，请求被直接拦截。"""


@dataclass(frozen=True)
class RobotSendResult:
	"""机器人批量发送结果；两个失败列表都为空才算全量送达。"""

	success: bool
	process_query_key: str | None = None
	invalid_staff_ids: list[str] = field(default_factory=list)
	flow_controlled_staff_ids: list[str] = field(default_factory=list)


class ProcessTokenCache:
	"""进程内 token 缓存（无 frappe 环境时使用）。"""

	def __init__(self, clock: Callable[[], float] = time.time):
		self._clock = clock
		self._values: dict[str, tuple[str, float]] = {}
		self._lock = threading.Lock()

	def get(self, key: str) -> str | None:
		with self._lock:
			cached = self._values.get(key)
			if not cached or self._clock() >= cached[1]:
				return None
			return cached[0]

	def set(self, key: str, value: str, ttl_seconds: int) -> None:
		with self._lock:
			self._values[key] = (value, self._clock() + max(60, int(ttl_seconds)))


class FrappeTokenCache:
	"""frappe.cache() 适配：跨 worker 共享 token，避免每个进程各取一次。"""

	def __init__(self, cache: Any):
		self._cache = cache

	def get(self, key: str) -> str | None:
		value = self._cache.get_value(key)
		return str(value) if value else None

	def set(self, key: str, value: str, ttl_seconds: int) -> None:
		self._cache.set_value(key, value, expires_in_sec=max(60, int(ttl_seconds)))


def build_token_cache() -> ProcessTokenCache | FrappeTokenCache:
	"""有 frappe 时用站点缓存，否则退回进程内缓存。"""
	try:
		import frappe
	except ImportError:
		return ProcessTokenCache()
	return FrappeTokenCache(frappe.cache())


@dataclass(frozen=True)
class DingTalkConfig:
	"""钉钉应用配置；``prefix`` 用于同一站点并存多套凭据（如预算、支出同步）。"""

	app_key: str = ""
	app_secret: str = ""
	robot_app_key: str = ""
	robot_app_secret: str = ""
	robot_code: str = ""
	oapi_url: str = DEFAULT_OAPI_URL
	api_url: str = DEFAULT_API_URL
	timeout_seconds: float = 15.0
	request_delay_ms: int = 300
	max_retries: int = 3
	base_retry_delay_ms: int = 2000
	circuit_enabled: bool = True

	@classmethod
	def from_site_config(cls, prefix: str = "") -> DingTalkConfig:
		"""从 frappe.conf 读取（环境变量优先），键名与现有模块保持一致。"""

		def conf(name: str, default: str = "") -> str:
			env_name = f"{prefix}{name}".upper()
			value = os.environ.get(env_name) or _site_conf(f"{prefix}{name}")
			return str(value).strip() if value else default

		return cls(
			app_key=conf("dingtalk_app_key"),
			app_secret=conf("dingtalk_app_secret"),
			robot_app_key=conf("dingtalk_robot_app_key"),
			robot_app_secret=conf("dingtalk_robot_app_secret"),
			robot_code=conf("dingtalk_robot_code"),
			oapi_url=conf("dingtalk_oapi_url", DEFAULT_OAPI_URL),
			api_url=conf("dingtalk_api_url", DEFAULT_API_URL),
		)


def _site_conf(key: str) -> Any:
	try:
		from frappe import conf
	except ImportError:
		return None
	return conf.get(key)


class CircuitBreaker:
	"""CLOSED → OPEN → HALF_OPEN → CLOSED，对齐 `budget/server/utils/resilience.js:58-124`。"""

	def __init__(self, label: str, failure_threshold: int = 5, reset_timeout_ms: int = 60000):
		self.label = label
		self.failure_threshold = max(1, int(failure_threshold))
		self.reset_timeout_ms = max(0, int(reset_timeout_ms))
		self._state = "CLOSED"
		self._failure_count = 0
		self._last_failure_at = 0.0
		self._lock = threading.Lock()

	@property
	def state(self) -> str:
		with self._lock:
			return self._state_locked()

	@property
	def failure_count(self) -> int:
		with self._lock:
			return self._failure_count

	def _state_locked(self) -> str:
		if self._state == "OPEN" and (time.time() - self._last_failure_at) * 1000 >= self.reset_timeout_ms:
			self._state = "HALF_OPEN"
		return self._state

	def execute(self, fn: Callable[[], Any]) -> Any:
		with self._lock:
			if self._state_locked() == "OPEN":
				raise CircuitOpenError(f"[CIRCUIT] {self.label}: circuit is OPEN, request blocked")

		try:
			result = fn()
		except Exception:
			with self._lock:
				self._failure_count += 1
				self._last_failure_at = time.time()
				if self._failure_count >= self.failure_threshold:
					self._state = "OPEN"
			raise

		with self._lock:
			self._state = "CLOSED"
			self._failure_count = 0
		return result

	def reset(self) -> None:
		with self._lock:
			self._state = "CLOSED"
			self._failure_count = 0
			self._last_failure_at = 0.0


class DingTalkClient:
	"""钉钉开放平台 6 个接口的封装。"""

	def __init__(
		self,
		config: DingTalkConfig,
		session: Any | None = None,
		token_cache: Any | None = None,
	):
		self.config = config
		self._session = session or requests.Session()
		self._token_cache = token_cache or build_token_cache()
		self._circuits: dict[str, CircuitBreaker] = {
			"token": CircuitBreaker("dingtalk-token", 3, 30000),
			"list": CircuitBreaker("dingtalk-list", 5, 60000),
			"detail": CircuitBreaker("dingtalk-detail", 5, 60000),
		}

	def _run(self, group: str, fn: Callable[[], Any]) -> Any:
		breaker = self._circuits.get(group)
		if not self.config.circuit_enabled or breaker is None:
			return fn()
		return breaker.execute(fn)

	def _throttle(self) -> None:
		if self.config.request_delay_ms > 0:
			time.sleep(self.config.request_delay_ms / 1000.0)

	@staticmethod
	def _is_qps_limited(response: Any) -> bool:
		try:
			body = response.json()
		except ValueError:
			return "QpsLimitForApi" in (response.text or "")
		if isinstance(body, str):
			return "QpsLimitForApi" in body
		if isinstance(body, dict):
			return body.get("code") == QPS_ERROR_CODE
		return False

	def _request(
		self,
		method: str,
		url: str,
		*,
		params: dict | None = None,
		json_body: dict | None = None,
		headers: dict | None = None,
	) -> Any:
		"""带 QPS 退避重试的请求；返回解析后的 JSON。"""
		attempts = max(0, self.config.max_retries)
		last_error: Exception | None = None

		for attempt in range(attempts + 1):
			self._throttle()
			try:
				response = self._session.request(
					method,
					url,
					params=params,
					json=json_body,
					headers=headers,
					timeout=self.config.timeout_seconds,
				)
			except requests.RequestException as exc:
				last_error = exc
				if attempt >= attempts:
					raise DingTalkAPIError(f"请求失败: {exc}") from exc
				time.sleep(self.config.base_retry_delay_ms * (2**attempt) / 1000.0)
				continue

			if response.status_code == 429 or self._is_qps_limited(response):
				last_error = DingTalkAPIError(f"QPS 限流: {response.text[:200]}")
				if attempt >= attempts:
					break
				time.sleep(self.config.base_retry_delay_ms * (2**attempt) / 1000.0)
				continue

			try:
				return response.json()
			except ValueError as exc:
				raise DingTalkAPIError(
					f"响应不是 JSON (HTTP {response.status_code}): {response.text[:200]}"
				) from exc

		raise last_error or DingTalkAPIError("请求失败且没有可用错误信息")

	# ------------------------------------------------------------------ token
	def get_access_token(self, app_key: str | None = None, app_secret: str | None = None) -> str:
		"""获取并缓存企业应用 access_token；TTL 取 ``expires_in - 300`` 秒。"""
		key = app_key or self.config.app_key
		secret = app_secret or self.config.app_secret
		if not key or not secret:
			raise DingTalkAPIError("缺少 app_key / app_secret，无法获取 access_token")

		cache_key = f"budget:access_token:{key}"
		cached = self._token_cache.get(cache_key)
		if cached:
			return cached

		data = self._run(
			"token",
			lambda: self._request(
				"GET",
				f"{self.config.oapi_url}/gettoken",
				params={"appkey": key, "appsecret": secret},
			),
		)
		if not data.get("access_token") or data.get("errcode") not in (0, None):
			raise DingTalkAPIError(f"获取 access_token 失败: {data}")

		expires_in = int(data.get("expires_in") or 7200)
		self._token_cache.set(cache_key, data["access_token"], max(60, expires_in - 300))
		return data["access_token"]

	def get_robot_token(self) -> str:
		"""机器人应用凭据独立缓存。"""
		if not self.config.robot_app_key or not self.config.robot_app_secret:
			raise DingTalkAPIError("缺少机器人凭据 (robot_app_key / robot_app_secret)")
		return self.get_access_token(self.config.robot_app_key, self.config.robot_app_secret)

	# ------------------------------------------------------------- 机器人
	def send_markdown(
		self,
		user_ids: list[str],
		title: str,
		text: str,
		*,
		robot_code: str | None = None,
	) -> RobotSendResult:
		"""机器人批量发送 Markdown；单次上限 20 人由调用方分批。"""
		code = robot_code or self.config.robot_code
		if not code:
			raise DingTalkAPIError("缺少 robot_code，无法发送机器人消息")

		data = self._request(
			"POST",
			f"{self.config.api_url}/v1.0/robot/oToMessages/batchSend",
			json_body={
				"robotCode": code,
				"userIds": list(user_ids),
				"msgKey": "sampleMarkdown",
				"msgParam": json.dumps({"title": title, "text": text}, ensure_ascii=False),
			},
			headers={
				"Content-Type": "application/json",
				"x-acs-dingtalk-access-token": self.get_robot_token(),
			},
		)
		invalid = list(data.get("invalidStaffIdList") or [])
		controlled = list(data.get("flowControlledStaffIdList") or [])
		return RobotSendResult(
			success=not invalid and not controlled,
			process_query_key=data.get("processQueryKey"),
			invalid_staff_ids=invalid,
			flow_controlled_staff_ids=controlled,
		)
