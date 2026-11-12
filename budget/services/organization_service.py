"""部门树的分层、唯一命中与路径快照归一。

纯逻辑层：输入是 `ding_department_tree` 的行，输出是可直接返回给前端/业务的结构。

规则出处：
- `dingtalk-budget/server/services/department-tree.js:93-138`：集团/公司/部门三层选项。
- `dingtalk-budget/server/services/department-tree.js:140-182`：``pickUniqueDepartment`` 必须唯一。
- `dingtalk-expense-sync/src/oa-source.ts:256-331`：部门路径快照的最终筛选条件。
"""

from __future__ import annotations

from typing import Any

# 集团根节点名，对齐 `department-tree.js:81-88`（比对前统一转小写并压缩空白）
GROUP_ROOT_NAMES = frozenset({"yuewei", "悦为集团", "悦为集团yuewei grupo"})


def text_of(value: Any) -> str | None:
	"""去首尾空白；空值返回 None。"""
	if value is None:
		return None
	text = str(value).strip()
	return text or None


def string_list(value: Any) -> list[str]:
	"""把 jsonb 数组转成去空白的字符串列表；非数组返回空列表。"""
	if not isinstance(value, list):
		return []
	return [text for text in (text_of(item) for item in value) if text]


def normalize_organization_name(value: Any) -> str:
	"""组织名归一：转小写 + 压缩空白（对齐 ``normalizeOrganizationName``）。"""
	return " ".join(str(value or "").split()).lower()


def is_group_root_name(value: Any) -> bool:
	"""是否为集团根节点名。"""
	return normalize_organization_name(value) in GROUP_ROOT_NAMES


def _sort_by_name(items: list[dict]) -> list[dict]:
	"""按名称排序。

	JS 侧用 ``localeCompare(name, "zh-CN")``（拼音序）。Python 不擅自改宿主进程的全局 locale，
	这里用码位序；需要拼音序时由调用方在进程内设置 ``LC_COLLATE`` 后再自行排序。
	"""
	return sorted(items, key=lambda item: item["name"])


def build_organization_options(rows: list[dict]) -> dict[str, list[dict]]:
	"""按 ``path_ids`` 切出集团 / 公司 / 部门三层。

	- 只认路径里出现集团根名的行；
	- 集团下一级是公司；
	- 公司下一级及更深节点都是可选部门；
	- 没有出现在路径里的部门（例如外部组织）直接丢弃。
	"""
	groups: dict[str, dict] = {}
	companies: dict[str, dict] = {}
	departments: dict[str, dict] = {}

	for row in rows:
		ids = string_list(row.get("path_ids"))
		names = string_list(row.get("path_names"))
		group_index = next((index for index, name in enumerate(names) if is_group_root_name(name)), -1)
		if group_index < 0 or group_index >= len(ids):
			continue

		group = {"id": ids[group_index], "name": names[group_index]}
		groups[group["id"]] = group

		company_index = group_index + 1
		if company_index >= len(ids):
			continue

		company = {
			"id": ids[company_index],
			"name": names[company_index],
			"group_dept_id": group["id"],
			"group_name": group["name"],
		}
		companies[company["id"]] = company

		department_id = text_of(row.get("dept_id"))
		department_name = text_of(row.get("name")) or (names[-1] if names else None)
		if len(ids) <= company_index + 1 or not department_id or not department_name:
			continue

		departments[department_id] = {
			"id": department_id,
			"name": department_name,
			"company_dept_id": company["id"],
			"company_name": company["name"],
			"group_dept_id": group["id"],
			"group_name": group["name"],
		}

	return {
		"groups": _sort_by_name(list(groups.values())),
		"companies": _sort_by_name(list(companies.values())),
		"departments": _sort_by_name(list(departments.values())),
	}


def pick_unique_department(rows: list[dict]) -> dict | None:
	"""在当前快照里挑唯一部门；零条或多条都返回 None。

	对齐 ``pickUniqueDepartment``：优先按 ``dept_id`` 去重，再要求只剩一条，且名称非空。
	"""
	candidates: dict[str, dict] = {}
	for row in rows:
		if row.get("is_current") is not True:
			continue
		department_id = text_of(row.get("dept_id"))
		department_name = text_of(row.get("name"))
		if department_id and department_name:
			candidates[department_id] = row
	return next(iter(candidates.values())) if len(candidates) == 1 else None


def normalize_department_snapshots(rows: list[dict]) -> dict[str, dict]:
	"""把 Q9 的候选行收敛成 ``dept_id -> 快照``；不完整的行丢弃。

	对齐 `oa-source.ts:318-331`：名称、path_ids、path_names 任一为空都不采用。
	"""
	snapshots: dict[str, dict] = {}
	for row in rows:
		department_id = text_of(row.get("dept_id"))
		department = text_of(row.get("name"))
		path_ids = (
			[str(item) for item in row.get("path_ids")] if isinstance(row.get("path_ids"), list) else []
		)
		path_names = (
			[str(item) for item in row.get("path_names")] if isinstance(row.get("path_names"), list) else []
		)
		if not department_id or not department or not path_ids or not path_names:
			continue
		snapshots[department_id] = {
			"dept_id": department_id,
			"department": department,
			"path_ids": path_ids,
			"path_names": path_names,
		}
	return snapshots
