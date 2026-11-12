"""审批实例的字段归因与组装。

纯逻辑层：不读数据库、不发 HTTP、不 import frappe，可脱库单测。

规则出处：
- `dingtalk-budget/server/services/dingtalk.js:118-151`：`buildOaDbDetail` 的字段优先级
  （优先 `raw_payload`，缺失回落行字段）。
- `dingtalk-expense-sync/src/oa-source.ts:92-103`：`resolveOriginatorUserName`
  （姓名字段被写成工号时改用用户快照名）。
"""

from __future__ import annotations

from typing import Any


def text_of(value: Any) -> str | None:
	"""去首尾空白；空值返回 None（对齐 Node 侧 ``text()``）。"""
	if value is None:
		return None
	text = str(value).strip()
	return text or None


def as_list(value: Any) -> list:
	"""仅接受 list，其它一概视为空（对齐 Node 侧 ``asArray``）。"""
	return value if isinstance(value, list) else []


def to_iso_list(value: Any) -> Any:
	"""datetime 转 ISO 字符串，其它原样返回。"""
	return value.isoformat() if hasattr(value, "isoformat") else value


def resolve_originator_user_name(source_name: Any, user_id: Any, snapshot_name: Any = None) -> str | None:
	"""发起人姓名归因。

	优先级来自 `oa-source.ts:100-102`：
	- ``source`` 为空、等于 userId、或是纯数字（工号被误存成姓名）→ 先快照名，后源名；
	- 否则源名优先，其次快照名；
	- 两者都没有时兜底 userId。

	``\\d+`` 在 JS 里只匹配 ASCII 数字，这里用 ``isascii() and isdigit()`` 保持同一语义，
	避免把全角数字也当成工号。
	"""
	source = text_of(source_name)
	user = text_of(user_id)
	snapshot = text_of(snapshot_name)
	source_looks_like_user_id = not source or source == user or (source.isascii() and source.isdigit())
	primary = (snapshot or source) if source_looks_like_user_id else (source or snapshot)
	return primary or user or None


def build_instance_detail(row: dict) -> dict:
	"""把 ``ding_approval_instance`` 行组装成与钉钉详情一致的 camelCase 结构。

	条目与 ``buildOaDbDetail`` 一一对应；``originator`` 在 `raw_payload` 里已有对象时直接沿用。
	"""
	raw = row.get("raw_payload") if isinstance(row.get("raw_payload"), dict) else {}
	originator = (
		raw.get("originator")
		if isinstance(raw.get("originator"), dict)
		else {
			"userId": raw.get("originatorUserId") or row.get("originator_user_id"),
			"name": raw.get("originatorUserName") or row.get("originator_user_name"),
			"deptId": raw.get("originatorDeptId") or row.get("originator_dept_id"),
			"deptName": raw.get("originatorDeptName") or row.get("originator_dept_name"),
		}
	)

	detail = dict(raw)
	detail.update(
		{
			"corpId": raw.get("corpId") or raw.get("corp_id") or row.get("corp_id") or "",
			"processInstanceId": raw.get("processInstanceId") or row.get("process_instance_id"),
			"processCode": raw.get("processCode") or row.get("process_code"),
			"title": raw.get("title") or row.get("title") or "",
			"status": raw.get("status") or row.get("status") or "",
			"result": raw.get("result") or row.get("result") or "",
			"businessId": raw.get("businessId") or raw.get("business_id") or "",
			"createTime": raw.get("createTime") or to_iso_list(row.get("create_time")),
			"finishTime": raw.get("finishTime") or to_iso_list(row.get("finish_time")),
			"formComponentValues": as_list(raw.get("formComponentValues"))
			or as_list(row.get("form_component_values")),
			"tasks": as_list(raw.get("tasks")),
			"operationRecords": as_list(raw.get("operationRecords")),
			"originatorUserId": raw.get("originatorUserId") or row.get("originator_user_id") or "",
			"originatorUserName": raw.get("originatorUserName") or row.get("originator_user_name") or "",
			"originatorDeptId": raw.get("originatorDeptId") or row.get("originator_dept_id") or "",
			"originatorDeptName": raw.get("originatorDeptName") or row.get("originator_dept_name") or "",
			"originator": originator,
		}
	)
	return detail


def apply_originator_snapshot(detail: dict, snapshot_name: Any) -> dict:
	"""用用户快照名收敛 ``originatorUserName``，并同步 ``originator.name``。

	只有走过 Q5（带 lateral 快照 join）时才应调用；Q4 口径（budget）不做这层归因。
	"""
	resolved = resolve_originator_user_name(
		detail.get("originatorUserName"),
		detail.get("originatorUserId"),
		snapshot_name,
	)
	detail["originatorUserName"] = resolved or ""
	originator = detail.get("originator")
	if isinstance(originator, dict):
		originator["name"] = resolved
	return detail
