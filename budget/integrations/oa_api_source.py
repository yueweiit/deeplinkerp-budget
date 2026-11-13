"""通过 ``dingtalk-oa`` 的只读 HTTP API 读取审批数据。

上游接口：``GET /api/v1/approvals``（无需 Bearer，``corpId`` 必填，游标分页），
返回 ``{success, data:[...], pagination:{hasMore,nextCursor,syncUntil}}``，
任务节点在 ``data[].rawPayload.tasks`` 里。契约见 dingtalk-oa
``docs/modules/approval-read-api/README.md``。

与 ``OAApprovalSource``（PostgreSQL 直连）的关系：

- 本类是它的 HTTP 版本，对外暴露同一组方法、同一套行字段（snake_case），
  业务层与报表层无需区分数据来源；
- 接口只覆盖审批（实例 + 任务），没有部门树接口，因此 ``list_departments`` /
  ``list_department_candidates`` / ``list_department_snapshot_rows`` 回退到 PostgreSQL；
- 支出库（``dingtalk_approval``）不在该 API 范围内，仍走 ``ExpenseSource``。

取舍说明：接口不支持按 ``createTime`` 过滤，时间窗只能在本地过滤；可以先用
``updatedAfter`` 收窄（``updated_at`` 恒不早于 ``create_time``，不会漏掉窗内实例）。
实例行缺少「模板已启用」这个 join 条件，失效模板的历史审批会一并返回。
"""

from __future__ import annotations

import logging
from collections.abc import Iterable, Iterator
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timezone
from typing import Any

from .oa_approval_source import OAApprovalSource, _runtime_config_value
from .oa_read_api_client import (
	OAInvalidCursor,
	OAReadAPIClient,
	OAReadAPIConfig,
	OAReadAPIError,
)

LOGGER = logging.getLogger(__name__)

DEFAULT_PAGE_SIZE = 200


class OAAPISourceError(RuntimeError):
	"""OA 读取接口不可用或返回异常。"""


@dataclass(frozen=True)
class OAAPIConfig:
	"""HTTP 数据源参数；连接信息复用 ``OAReadAPIConfig``（``budget_oa_api_url`` 等）。"""

	base_url: str = ""
	corp_id: str = ""
	page_size: int = DEFAULT_PAGE_SIZE
	timeout_seconds: float = 30.0

	@classmethod
	def from_site_config(cls) -> OAAPIConfig:
		read_config = OAReadAPIConfig.from_site_config()
		return cls(
			base_url=read_config.base_url,
			corp_id=_runtime_config_value(
				"budget_corp_id", "budget_oa_corp_id", "dingtalk_oa_corp_id", "dingtalk_corp_id"
			),
			page_size=int(_runtime_config_value("budget_oa_api_page_size", default="200") or 200),
			timeout_seconds=read_config.timeout_seconds,
		)


def _parse_date(value: Any) -> date | None:
	if not value:
		return None
	try:
		return date.fromisoformat(str(value).strip()[:10])
	except ValueError:
		return None


def _parse_datetime(value: Any) -> datetime | None:
	if not value:
		return None
	text = str(value).strip().replace(" ", "T")
	if text.endswith("Z"):
		text = text[:-1] + "+00:00"
	try:
		parsed = datetime.fromisoformat(text)
	except ValueError:
		return None
	return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def _time_window(
	start_date: str | None, end_date: str | None
) -> tuple[datetime | None, datetime | None]:
	start = _parse_date(start_date)
	end = _parse_date(end_date)
	start_at = datetime.combine(start, time.min, tzinfo=UTC) if start else None
	end_at = datetime.combine(end, time.max, tzinfo=UTC) if end else None
	return start_at, end_at


def _text(value: Any) -> str:
	return str(value or "").strip()


def _looks_like_user_id(name: Any, user_id: Any) -> bool:
	"""与 ``approval_attribution_service`` 同口径：空、等于工号或纯 ASCII 数字都算工号。"""
	text = _text(name)
	if not text:
		return True
	if text == _text(user_id):
		return True
	return text.isascii() and text.isdigit()


class OAAPISource:
	"""``OAApprovalSource`` 的 HTTP 版本（审批走接口，组织树回退数据库）。"""

	def __init__(
		self,
		config: OAAPIConfig | None = None,
		*,
		client: OAReadAPIClient | None = None,
		fallback: OAApprovalSource | None = None,
	):
		self._config = config or OAAPIConfig.from_site_config()
		self._client = client
		self._fallback = fallback

	# ------------------------------------------------------------ 基础设施
	def _corp_scope(self) -> str:
		"""企业范围必须显式配置：缺了就报错，避免退化成「跨企业混读」。"""
		if not self._config.corp_id:
			raise OAAPISourceError(
				"未配置 OA 企业范围 corp_id（budget_oa_corp_id / dingtalk_oa_corp_id），拒绝执行跨企业查询"
			)
		return self._config.corp_id

	def _api(self) -> OAReadAPIClient:
		if self._client is None:
			try:
				self._client = OAReadAPIClient(
					OAReadAPIConfig(
						base_url=self._config.base_url,
						timeout_seconds=self._config.timeout_seconds,
					)
				)
			except OAReadAPIError as exc:
				raise OAAPISourceError(str(exc)) from exc
		return self._client

	def _fallback_source(self) -> OAApprovalSource:
		"""接口不覆盖的查询（部门树、用户快照）仍回落到 PostgreSQL。"""
		if self._fallback is None:
			self._fallback = OAApprovalSource(corp_id=self._config.corp_id or None)
		return self._fallback

	def _iter_approvals(self, *, updated_after: str | None = None) -> Iterator[dict]:
		"""逐条翻页；游标中途失效时重开一轮，已产出的实例不重复。"""
		corp_id = self._corp_scope()
		seen: set[str] = set()
		restarted = False
		cursor: str | None = None
		while True:
			try:
				page = self._api().list_approvals(
					corp_id,
					updated_after=updated_after,
					limit=self._config.page_size,
					cursor=cursor,
				)
			except OAInvalidCursor:
				if restarted:
					raise OAAPISourceError("OA 读取接口游标连续失效，请稍后重试") from None
				restarted = True
				cursor = None
				continue
			except OAReadAPIError as exc:
				raise OAAPISourceError(f"OA 读取接口不可用：{exc}") from exc

			for item in page.rows:
				instance_id = _text(item.get("processInstanceId"))
				# 重开一轮时同一实例可能再次出现：按实例去重，避免明细翻倍。
				if instance_id:
					if instance_id in seen:
						continue
					seen.add(instance_id)
				yield item

			if not page.has_more or not page.next_cursor:
				return
			cursor = page.next_cursor

	# ---------------------------------------------------------- 审批效率
	def list_approval_efficiency_rows(
		self, *, start_date: str | None = None, end_date: str | None = None
	) -> tuple[list[dict], list[dict]]:
		"""Q8：返回 ``(instances, tasks)``，字段与 ``OAApprovalSource`` 对齐。"""
		start_at, end_at = _time_window(start_date, end_date)
		instances: list[dict] = []
		tasks: list[dict] = []
		for item in self._iter_approvals(
			updated_after=start_at.isoformat() if start_at else None
		):
			created = _parse_datetime(item.get("createTime"))
			if start_at and created and created < start_at:
				continue
			if end_at and created and created > end_at:
				continue

			instance_id = _text(item.get("processInstanceId"))
			process_code = _text(item.get("processCode"))
			instances.append(
				{
					"process_instance_id": instance_id,
					"process_code": process_code,
					"process_name": _text(item.get("templateName")) or process_code,
					"title": _text(item.get("title")),
					"status": _text(item.get("status")),
					"result": _text(item.get("result")),
					"create_time": item.get("createTime"),
					"finish_time": item.get("finishTime"),
				}
			)
			for task in _tasks_of(item):
				tasks.append(
					{
						"process_instance_id": instance_id,
						"process_code": process_code,
						"task_id": _text(task.get("taskId")),
						"node_name": _text(task.get("activityId")) or _text(task.get("taskGroupName")),
						"status": _text(task.get("status")),
						"result": _text(task.get("result")),
						"approver_user_id": _text(task.get("userId")),
						"approver_user_name": _text(task.get("userName")),
						"start_time": task.get("createTime"),
						"end_time": task.get("finishTime"),
					}
				)
		return instances, tasks

	# ---------------------------------------------------------- 审批详情
	def get_instance_row(self, instance_id: str, *, with_snapshot: bool = True) -> dict | None:
		"""按实例 ID 或审批编号取一行；``with_snapshot`` 时补用户快照名（需库可达）。"""
		corp_id = self._corp_scope()
		client = self._api()
		try:
			item = client.get_approval(corp_id, instance_id)
			if not item:
				item = client.get_approval(corp_id, instance_id, by_approval_no=instance_id)
		except OAReadAPIError as exc:
			raise OAAPISourceError(f"OA 读取接口不可用：{exc}") from exc
		if not item:
			return None

		row = {
			"corp_id": _text(item.get("corpId")) or corp_id,
			"process_instance_id": _text(item.get("processInstanceId")),
			"process_code": _text(item.get("processCode")),
			"title": _text(item.get("title")),
			"status": _text(item.get("status")),
			"result": _text(item.get("result")),
			"originator_user_id": _text(item.get("originatorUserId")),
			"originator_user_name": _text(item.get("originatorUserName")),
			"originator_dept_id": _text(item.get("originatorDeptId")),
			"originator_dept_name": _text(item.get("originatorDeptName")),
			"create_time": item.get("createTime"),
			"finish_time": item.get("finishTime"),
			"updated_at": item.get("updatedAt"),
			"form_component_values": item.get("formComponentValues"),
			"raw_payload": item.get("rawPayload"),
			"snapshot_user_name": None,
		}
		if with_snapshot and _looks_like_user_id(
			row["originator_user_name"], row["originator_user_id"]
		):
			row["snapshot_user_name"] = self._snapshot_user_name(row["process_instance_id"])
		return row

	def _snapshot_user_name(self, instance_id: str) -> str | None:
		"""接口不带用户快照，尽力从库里补；不可达时返回 None，不阻断查询。"""
		if not instance_id:
			return None
		try:
			db_row = self._fallback_source().get_instance_row(instance_id, with_snapshot=True)
		except Exception as exc:  # 快照只是补名，失败即降级
			LOGGER.debug("用户快照补齐失败，降级为接口原始名称：%s", exc)
			return None
		return _text((db_row or {}).get("snapshot_user_name")) or None

	# ------------------------------------------------------------ 部门树
	def list_departments(self, *, current_only: bool = True) -> list[dict]:
		"""接口没有部门树，回退到 PostgreSQL（组织级联不受影响）。"""
		return self._fallback_source().list_departments(current_only=current_only)

	def list_department_candidates(
		self,
		service_entity: str,
		*,
		corresponding_department: str | None = None,
		dept_code: str | None = None,
	) -> list[dict]:
		"""Q7：接口没有部门树，回退到 PostgreSQL。"""
		return self._fallback_source().list_department_candidates(
			service_entity,
			corresponding_department=corresponding_department,
			dept_code=dept_code,
		)

	def list_department_snapshot_rows(self, dept_ids: Iterable[str]) -> list[dict]:
		"""Q9：接口没有部门树，回退到 PostgreSQL。"""
		return self._fallback_source().list_department_snapshot_rows(dept_ids)


def _tasks_of(item: dict) -> list[dict]:
	raw = item.get("rawPayload")
	if not isinstance(raw, dict):
		return []
	tasks = raw.get("tasks")
	return [task for task in tasks if isinstance(task, dict)] if isinstance(tasks, list) else []
