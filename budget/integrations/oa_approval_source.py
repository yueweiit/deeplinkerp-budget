"""钉钉 OA 只读库（PostgreSQL ``dingtalk_oa``）数据源。

对标源码：
- `dingtalk-budget/server/services/dingtalk.js`：事件时间取 ID、实例详情、部门树、审批效率。
- `dingtalk-expense-sync/src/oa-source.ts`：create_time / updated_at 分页、历史部门路径回退。

本模块只负责「拿行」，返回原始 dict；字段归因、分页游标、分层判定都在 ``services/``。
连接默认用 psycopg3 且强制只读事务，调用方可注入 ``connect`` 做脱库单测。
"""

from __future__ import annotations

import os
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from typing import Any

from .pg import fetch as pg_fetch

DEFAULT_DATABASE = "dingtalk_oa"


class OAApprovalSourceScopeError(RuntimeError):
	"""缺少企业范围（``corp_id``）时抛出：宁可报错，也不跨企业混读。"""


# 站点只有一个 PostgreSQL 连接入口：``budget_db_*`` 为当前键名，
# 其余是历年沿用下来的别名（``budget_oa_db_*`` / ``dingtalk_oa_db_*`` / ``oa_mapping_pg_*``），
# 按顺序取第一个非空值，老站点不用改配置即可继续运行。
_SHARED_DB_KEYS: dict[str, tuple[str, ...]] = {
	"host": ("budget_db_host", "budget_oa_db_host", "dingtalk_oa_db_host", "oa_mapping_pg_host"),
	"port": ("budget_db_port", "budget_oa_db_port", "dingtalk_oa_db_port", "oa_mapping_pg_port"),
	"database": (
		"budget_db_name",
		"budget_oa_db_name",
		"dingtalk_oa_db_name",
		"oa_mapping_pg_database",
	),
	"user": ("budget_db_user", "budget_oa_db_user", "dingtalk_oa_db_user", "oa_mapping_pg_user"),
	"password": (
		"budget_db_password",
		"budget_oa_db_password",
		"dingtalk_oa_db_password",
		"oa_mapping_pg_password",
	),
}


def shared_db_config() -> dict[str, str]:
	"""站点唯一的 PG 连接参数：预算台账、审批、费用三个数据源共用这一份。

	返回 ``host / port / database / user / password``（均为字符串）。
	需要连另一个库时，只在该数据源侧配 ``*_db_name`` 覆盖库名，连接信息不再重复配置。
	"""
	return {
		"host": _runtime_config_value(*_SHARED_DB_KEYS["host"], default="localhost"),
		"port": _runtime_config_value(*_SHARED_DB_KEYS["port"], default="5432"),
		"database": _runtime_config_value(*_SHARED_DB_KEYS["database"], default=DEFAULT_DATABASE),
		"user": _runtime_config_value(*_SHARED_DB_KEYS["user"], default="postgres"),
		"password": _runtime_config_value(*_SHARED_DB_KEYS["password"]),
	}


def shared_database_name(*specific_keys: str, default: str) -> str:
	"""库名解析顺序：数据源自己的键 → 统一键 ``budget_db_name`` → 该数据源的默认库名。

	合并到同一个库之前，各数据源仍用 ``budget_ledger_db_name`` / ``budget_approval_db_name``
	指明自己的库；合并之后这两个键可以删掉，只留 ``budget_db_name``。
	"""
	return _runtime_config_value(*specific_keys, "budget_db_name", default=default)


@dataclass(frozen=True)
class OAApprovalSourceConfig:
	"""OA 库连接参数；``application_name`` 便于在库里定位调用方。

	``corp_id`` 是本实例负责的企业。OA 库是多企业共库，``ding_department_tree``
	的唯一键是 ``(corp_id, dept_id)``，``ding_approval_instance`` 的 ``businessId``
	也可能跨企业重复，因此所有查询都必须带这个范围。
	"""

	host: str = "localhost"
	port: int = 5432
	database: str = DEFAULT_DATABASE
	user: str = "postgres"
	password: str = ""
	corp_id: str = ""
	connect_timeout: int = 3
	statement_timeout_ms: int = 15000
	application_name: str = "budget"

	@classmethod
	def from_site_config(cls) -> OAApprovalSourceConfig:
		"""环境变量优先，其次 frappe.conf；连接信息取 ``shared_db_config()``。"""
		shared = shared_db_config()
		return cls(
			host=shared["host"],
			port=int(shared["port"]),
			database=shared["database"],
			user=shared["user"],
			password=shared["password"],
			corp_id=_runtime_config_value(
				"budget_corp_id", "budget_oa_corp_id", "dingtalk_oa_corp_id", "dingtalk_corp_id"
			),
		)


def _runtime_config_value(*keys: str, default: str = "") -> str:
	"""按顺序取配置：环境变量（大写）优先，其后 frappe.conf，最后默认值。"""
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


def postgres_connection_kwargs(config: OAApprovalSourceConfig, *, read_only: bool = True) -> dict:
	"""构造 psycopg3 连接参数；只读时在会话层禁写并设置语句超时。"""
	options = [f"-c statement_timeout={int(config.statement_timeout_ms)}"]
	if read_only:
		options.append("-c default_transaction_read_only=on")
	return {
		"host": config.host,
		"port": int(config.port),
		"dbname": config.database,
		"user": config.user,
		"password": config.password,
		"connect_timeout": int(config.connect_timeout),
		"application_name": config.application_name,
		"options": " ".join(options),
	}


def _default_connect(kwargs: dict) -> Any:
	try:
		import psycopg
		from psycopg.rows import dict_row
	except ImportError as exc:  # pragma: no cover - 依赖缺失时给出明确提示
		raise RuntimeError("查询 OA 库需要 psycopg3（pip install 'psycopg[binary]'）") from exc
	return psycopg.connect(**kwargs, row_factory=dict_row)


class OAApprovalSource:
	"""OA 库查询集合，全部只读。"""

	def __init__(
		self,
		config: OAApprovalSourceConfig | None = None,
		*,
		corp_id: str | None = None,
		connect: Callable[[dict], Any] | None = None,
	):
		self._config = config or OAApprovalSourceConfig.from_site_config()
		self._corp_id = str(corp_id or self._config.corp_id or "").strip()
		self._connect = connect or _default_connect

	def _corp_scope(self) -> str:
		"""企业范围必须显式配置：缺了就报错，避免退化成「读全库」。"""
		if not self._corp_id:
			raise OAApprovalSourceScopeError(
				"未配置 OA 企业范围 corp_id（budget_oa_corp_id / dingtalk_oa_corp_id），拒绝执行跨企业查询"
			)
		return self._corp_id

	def _fetch(self, sql: str, params: Iterable[Any] = ()) -> list[dict]:
		# 连接按请求复用（见 integrations/pg.py），失败自动重试一次
		return pg_fetch(
			postgres_connection_kwargs(self._config),
			sql,
			params,
			connect=self._connect,
			label="budget-oa",
		)

	# ---------------------------------------------------------- 实例详情
	def get_instance_row(self, instance_id: str, *, with_snapshot: bool = True) -> dict | None:
		"""Q4/Q5 合并：按 ``process_instance_id`` 或 ``businessId`` 取一行。

		``with_snapshot=False`` 即 budget 口径（Q4，不做用户快照 join）；
		默认 True 走 expense-sync 口径（Q5，lateral join 补 ``snapshot_user_name``）。
		"""
		corp_id = self._corp_scope()
		snapshot_join = (
			"""
			LEFT JOIN LATERAL (
				SELECT snapshot.name
				FROM ding_user_snapshot AS snapshot
				WHERE snapshot.corp_id = source.corp_id
				  AND snapshot.user_id = COALESCE(
						NULLIF(TRIM(source.originator_user_id), ''),
						NULLIF(TRIM(source.raw_payload->>'originatorUserId'), ''),
						NULLIF(TRIM(source.raw_payload->>'originator_user_id'), '')
					  )
				  AND snapshot.is_current = true
				  AND snapshot.fetch_status = 'success'
				  AND COALESCE(TRIM(snapshot.name), '') <> ''
				ORDER BY snapshot.valid_from DESC, snapshot.id DESC
				LIMIT 1
			) AS snapshot_row ON TRUE
			"""
			if with_snapshot
			else ""
		)
		snapshot_column = "snapshot_row.name AS snapshot_user_name," if with_snapshot else ""
		rows = self._fetch(
			f"""
			SELECT
			  {snapshot_column}
			  source.corp_id,
			  source.process_instance_id,
			  source.process_code,
			  source.title,
			  source.status,
			  source.result,
			  source.originator_user_id,
			  source.originator_user_name,
			  source.originator_dept_id,
			  source.originator_dept_name,
			  source.create_time,
			  source.finish_time,
			  source.updated_at,
			  source.form_component_values,
			  source.raw_payload
			FROM ding_approval_instance AS source
			{snapshot_join}
			WHERE source.deleted_at IS NULL
			  AND source.corp_id = %s
			  AND (
				source.process_instance_id = %s
				OR COALESCE(source.raw_payload->>'businessId', '') = %s
			  )
			ORDER BY CASE WHEN source.process_instance_id = %s THEN 0 ELSE 1 END, source.create_time DESC
			LIMIT 1
			""",
			(corp_id, instance_id, instance_id, instance_id),
		)
		return rows[0] if rows else None

	# ------------------------------------------------------------ 部门树
	def list_departments(self, *, current_only: bool = True) -> list[dict]:
		"""Q6：部门树原始行（是否分层由 service 决定）。"""
		corp_id = self._corp_scope()
		where = "AND is_current = true" if current_only else ""
		return self._fetch(
			f"""
			SELECT corp_id, dept_id, name, path_ids, path_names, is_current
			FROM ding_department_tree
			WHERE corp_id = %s
			{where}
			""",
			(corp_id,),
		)

	def list_department_candidates(
		self,
		service_entity: str,
		*,
		corresponding_department: str | None = None,
		dept_code: str | None = None,
	) -> list[dict]:
		"""Q7：按 业务主体 / 对应部门 / 部门编码 取候选行（唯一性判定在 service）。"""
		corp_id = self._corp_scope()
		return self._fetch(
			"""
			SELECT corp_id, dept_id, name, path_ids, path_names, is_current
			FROM ding_department_tree
			WHERE corp_id = %s
			  AND NULLIF(BTRIM(dept_id), '') IS NOT NULL
			  AND (
				(NULLIF(%s::text, '') IS NOT NULL AND BTRIM(dept_id) = %s::text)
				OR (
				  NULLIF(%s::text, '') IS NULL
				  AND (
					(NULLIF(%s::text, '') IS NOT NULL AND BTRIM(name) = %s::text
					  AND jsonb_typeof(path_names) = 'array'
					  AND path_names @> jsonb_build_array(%s::text))
					OR (NULLIF(%s::text, '') IS NULL AND BTRIM(name) = %s::text)
				  )
				)
			  )
			""",
			(
				corp_id,
				dept_code or "",
				dept_code or "",
				dept_code or "",
				corresponding_department or "",
				corresponding_department or "",
				service_entity,
				corresponding_department or "",
				service_entity,
			),
		)

	# -------------------------------------------------- 部门路径快照（Q9）
	def list_department_snapshot_rows(self, dept_ids: Iterable[str]) -> list[dict]:
		"""Q9：当前快照唯一优先、历史快照回退（计数与筛选都在 SQL 里完成）。"""
		ids = [str(item).strip() for item in dept_ids or [] if str(item or "").strip()]
		if not ids:
			return []
		corp_id = self._corp_scope()
		return self._fetch(
			"""
			WITH candidates AS (
			  SELECT dept_id, name, path_ids, path_names, is_current
				FROM ding_department_tree
			   WHERE corp_id = %s
				 AND dept_id = ANY(%s::varchar[])
			),
			current_counts AS (
			  SELECT dept_id, COUNT(*) AS match_count
				FROM candidates
			   WHERE is_current = true
			   GROUP BY dept_id
			),
			current_matches AS (
			  SELECT c.dept_id, c.name, c.path_ids, c.path_names
				FROM candidates c
				JOIN current_counts counts ON counts.dept_id = c.dept_id
			   WHERE c.is_current = true
				 AND counts.match_count = 1
				 AND NULLIF(BTRIM(c.name), '') IS NOT NULL
				 AND jsonb_typeof(c.path_ids) = 'array'
				 AND jsonb_array_length(c.path_ids) > 0
				 AND jsonb_typeof(c.path_names) = 'array'
				 AND jsonb_array_length(c.path_names) > 0
			),
			historical_counts AS (
			  SELECT c.dept_id, COUNT(*) AS match_count
				FROM candidates c
			   WHERE c.is_current IS NOT TRUE
				 AND NOT EXISTS (SELECT 1 FROM current_counts current WHERE current.dept_id = c.dept_id)
			   GROUP BY c.dept_id
			),
			historical_matches AS (
			  SELECT c.dept_id, c.name, c.path_ids, c.path_names
				FROM candidates c
				JOIN historical_counts counts ON counts.dept_id = c.dept_id
			   WHERE c.is_current IS NOT TRUE
				 AND counts.match_count = 1
				 AND NULLIF(BTRIM(c.name), '') IS NOT NULL
				 AND jsonb_typeof(c.path_ids) = 'array'
				 AND jsonb_array_length(c.path_ids) > 0
				 AND jsonb_typeof(c.path_names) = 'array'
				 AND jsonb_array_length(c.path_names) > 0
			)
			SELECT dept_id, name, path_ids, path_names FROM current_matches
			UNION ALL
			SELECT dept_id, name, path_ids, path_names FROM historical_matches
			""",
			(corp_id, ids),
		)

	# ------------------------------------------------------ 审批效率报表
	def list_approval_efficiency_rows(
		self, *, start_date: str | None = None, end_date: str | None = None
	) -> tuple[list[dict], list[dict]]:
		"""Q8：返回 ``(instances, tasks)`` 两张明细，聚合在业务层做。"""
		corp_id = self._corp_scope()
		params = (corp_id, start_date, start_date, end_date, end_date)
		instances = self._fetch(
			"""
			SELECT i.process_instance_id, i.process_code, p.name AS process_name,
				   i.title, i.status, i.result, i.create_time, i.finish_time
			  FROM ding_approval_instance i
			  JOIN ding_process_template p
				ON p.corp_id = i.corp_id AND p.process_code = i.process_code
			 WHERE i.deleted_at IS NULL
			   AND i.corp_id = %s
			   AND p.enabled = true
			   AND p.is_deleted = false
			   AND (%s::date IS NULL OR i.create_time >= %s::date)
			   AND (%s::date IS NULL OR i.create_time < %s::date + INTERVAL '1 day')
			 ORDER BY i.create_time DESC
			""",
			params,
		)
		tasks = self._fetch(
			"""
			SELECT t.process_instance_id, t.task_id,
				   COALESCE(NULLIF(t.node_name, ''), NULLIF(t.raw_payload->>'taskGroupName', '')) AS node_name,
				   t.status, t.approver_user_id,
				   CASE
					 WHEN t.approver_user_id = 'bpms_system' THEN '系统自动处理'
					 ELSE COALESCE(NULLIF(t.approver_user_name, ''), NULLIF(t.raw_payload->>'userName', ''), u.name)
				   END AS approver_user_name,
				   t.start_time, t.end_time
			  FROM ding_approval_task t
			  JOIN ding_approval_instance i
				ON i.corp_id = t.corp_id AND i.process_instance_id = t.process_instance_id
			  JOIN ding_process_template p
				ON p.corp_id = i.corp_id AND p.process_code = i.process_code
			  LEFT JOIN LATERAL (
				SELECT snapshot.name
				  FROM ding_user_snapshot snapshot
				 WHERE snapshot.corp_id = t.corp_id
				   AND snapshot.user_id = t.approver_user_id
				   AND NULLIF(snapshot.name, '') IS NOT NULL
				 ORDER BY snapshot.is_current DESC, snapshot.valid_from DESC NULLS LAST, snapshot.id DESC
				 LIMIT 1
			  ) u ON true
			 WHERE i.deleted_at IS NULL
			   AND i.corp_id = %s
			   AND p.enabled = true
			   AND p.is_deleted = false
			   AND (%s::date IS NULL OR i.create_time >= %s::date)
			   AND (%s::date IS NULL OR i.create_time < %s::date + INTERVAL '1 day')
			 ORDER BY i.create_time DESC, t.task_order NULLS LAST, t.start_time
			""",
			params,
		)
		return instances, tasks

