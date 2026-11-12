"""预算台账只读数据源（PostgreSQL ``budget_system`` 库）。

对标 ``dingtalk-budget/server/routes/list.js``：

- 主表：``production_budget`` / ``non_production_budget``
- 明细存在性：生产必须命中 ``budget_material`` / ``budget_production`` / ``budget_labor``；
  非生产必须命中 ``budget_hr`` / ``budget_office`` / ``budget_operation``
- 去重：按 ``(dept_key, budget_month_key)`` 取 ``create_time`` 最新的一单
- 状态：排除取消 / 驳回 / 撤回 等无效状态
- 地区：金额大于 0 的单必须是 China / 中国 / CN
- 统计卡片：今日条数走原始表，总数走去重后的 CTE

只读连接，调用方可注入 ``connect`` 做脱库单测。
"""

from __future__ import annotations

import logging
import os
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any

from .oa_approval_source import OAApprovalSourceConfig, shared_database_name, shared_db_config

LOGGER = logging.getLogger(__name__)

DEFAULT_DATABASE = "budget_system"
PRODUCTION = "production"
NON_PRODUCTION = "non_production"

# 命中任一关键字即视为无效预算单（对齐 list.js 的 excludedBudgetStatusKeywords）
_EXCLUDED_STATUS_REGEX = (
	"cancel|reject|refuse|terminate|invalid|withdraw|撤回|撤销|取消|驳回|拒绝|终止|作废|不通过"
)


class BudgetLedgerScopeError(RuntimeError):
	"""连接或参数不完整时抛出。"""


@dataclass(frozen=True)
class BudgetLedgerSourceConfig:
	"""预算库连接参数。"""

	host: str = "localhost"
	port: int = 5432
	database: str = DEFAULT_DATABASE
	user: str = "postgres"
	password: str = ""
	connect_timeout: int = 5
	statement_timeout_ms: int = 15000
	application_name: str = "budget-ledger"

	@classmethod
	def from_site_config(cls) -> BudgetLedgerSourceConfig:
		"""连接信息复用站点唯一的 PG 入口（``shared_db_config()``）。

		台账表与审批表合并到同一个库后，库名也一起共用，无需再配；
		合并之前才用 ``budget_ledger_db_name``（或旧键 ``budget_system_db_name``）
		单独指明台账所在的库。
		"""
		shared = shared_db_config()
		return cls(
			host=shared["host"],
			port=int(shared["port"]),
			database=shared_database_name(
				"budget_ledger_db_name", "budget_system_db_name", default=DEFAULT_DATABASE
			),
			user=shared["user"],
			password=shared["password"],
		)


def ledger_connection_kwargs(config: BudgetLedgerSourceConfig) -> dict:
	"""预算库同样强制只读会话 + 语句超时。"""
	options = [
		f"-c statement_timeout={int(config.statement_timeout_ms)}",
		"-c default_transaction_read_only=on",
	]
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
		raise RuntimeError("查询预算库需要 psycopg3（pip install 'psycopg[binary]'）") from exc
	return psycopg.connect(**kwargs, row_factory=dict_row)


def _month_expr(alias: str) -> str:
	return f"COALESCE(NULLIF({alias}.budget_month, ''), NULLIF({alias}.declaration_month, ''))"


def _dept_key_expr(alias: str) -> str:
	"""部门分区键：有 dept_id 用 id，否则退回名称归一化（对齐 deptPartitionExprFor）。"""
	normalized = (
		f"LOWER(REGEXP_REPLACE(COALESCE({alias}.dept_name, ''), "
		"'[\\\\s()（）\\\\-_/\\\\\\\\,.;:，。；：&]+', '', 'g'))"
	)
	legacy = f"""CASE
		WHEN {normalized} LIKE '%%悦为智能%%'
			OR {normalized} LIKE '%%ywtechai%%'
			OR ({normalized} LIKE '%%it%%sc%%' AND {normalized} LIKE '%%信息技术%%')
		THEN 'dept_yw_tech_ai'
		ELSE {normalized}
	END"""
	return f"""CASE
		WHEN NULLIF({alias}.dept_id, '') IS NOT NULL THEN 'id:' || {alias}.dept_id
		ELSE 'legacy:' || {alias}.form_no || ':' || ({legacy})
	END"""


def _valid_status_sql(alias: str) -> str:
	return f"COALESCE({alias}.status, '') !~* '{_EXCLUDED_STATUS_REGEX}'"


def _china_region_sql(alias: str) -> str:
	region = f"COALESCE({alias}.execution_region, '')"
	return (
		f"(STRPOS(LOWER({region}), 'china') > 0"
		f" OR STRPOS({region}, '中国') > 0"
		f" OR LOWER(BTRIM({region})) = 'cn')"
	)


def _submitted_scope_sql(alias: str) -> str:
	amount = (
		f"GREATEST(COALESCE({alias}.total_amount, 0), COALESCE({alias}.monthly_budget_amount, 0))"
		if alias == "p"
		else f"GREATEST(COALESCE({alias}.total_amount, 0), COALESCE({alias}.budget_amount, 0))"
	)
	return f"({amount} <= 0 OR {_china_region_sql(alias)})"


def _detail_exists_sql(alias: str) -> str:
	if alias == "p":
		return (
			"(EXISTS (SELECT 1 FROM budget_material m WHERE m.form_no = p.form_no)"
			" OR EXISTS (SELECT 1 FROM budget_production bp WHERE bp.form_no = p.form_no)"
			" OR EXISTS (SELECT 1 FROM budget_labor bl WHERE bl.form_no = p.form_no))"
		)
	return (
		"(EXISTS (SELECT 1 FROM budget_hr h WHERE h.form_no = n.form_no)"
		" OR EXISTS (SELECT 1 FROM budget_office o WHERE o.form_no = n.form_no)"
		" OR EXISTS (SELECT 1 FROM budget_operation op WHERE op.form_no = n.form_no))"
	)


# OA 部门树层级：ROOT(0) → 集团(1) → 子公司/中心(2) → … → 部门(N)
_COMPANY_LEVEL_INDEX = 2


def oa_connection_kwargs(config: OAApprovalSourceConfig) -> dict:
	"""部门树查询同样走只读会话 + 语句超时。"""
	options = [
		f"-c statement_timeout={int(config.statement_timeout_ms)}",
		"-c default_transaction_read_only=on",
	]
	return {
		"host": config.host,
		"port": int(config.port),
		"dbname": config.database,
		"user": config.user,
		"password": config.password,
		"connect_timeout": int(config.connect_timeout),
		"application_name": "budget-ledger-department",
		"options": " ".join(options),
	}


def load_department_paths(
	config: OAApprovalSourceConfig | None = None,
	*,
	connect: Callable[[dict], Any] | None = None,
) -> dict[str, list[str]]:
	"""从 OA 库读部门树权威路径（``dept_id -> path_names``）。

	预算单里的 ``dept_path_names`` 是历史快照，同一个 dept_id 可能有多种写法，
	所以展示口径以 ``dingtalk_oa.ding_department_tree`` 为准。
	"""
	cfg = config or OAApprovalSourceConfig.from_site_config()
	if not str(cfg.corp_id or "").strip():
		return {}
	connect_fn = connect or _default_connect
	with connect_fn(oa_connection_kwargs(cfg)) as connection, connection.cursor() as cursor:
		cursor.execute(
			"SELECT dept_id, path_names FROM public.ding_department_tree"
			" WHERE corp_id = %s AND is_current ORDER BY dept_id",
			(cfg.corp_id,),
		)
		return {
			str(row.get("dept_id")): [str(item) for item in (row.get("path_names") or [])]
			for row in cursor.fetchall()
		}


def department_presentation(
	row: dict, path_names: list[str] | None = None
) -> tuple[str, str]:
	"""返回 ``(子公司, 部门)``：子公司取 OA 部门树第 3 层，部门取叶子节点。

	层级：``ROOT → 悦为集团YUEWEI Grupo / YUEWEI → 子公司或中心 → … → 部门``。
	``path_names`` 不传时退回预算单里的快照；层级不足 3 层时取最后一级。
	"""
	names = path_names if path_names is not None else row.get("dept_path_names")
	levels = [str(item).strip() for item in (names or []) if str(item or "").strip()]
	if not levels:
		return ("待确认", "")

	company = (
		levels[_COMPANY_LEVEL_INDEX] if len(levels) > _COMPANY_LEVEL_INDEX else levels[-1]
	)

	terminal = levels[-1]
	sub_department = "" if terminal == company else terminal
	return (company or "待确认", sub_department)


class BudgetLedgerSource:
	"""预算库查询集合，全部只读。"""

	def __init__(
		self,
		config: BudgetLedgerSourceConfig | None = None,
		*,
		connect: Callable[[dict], Any] | None = None,
		department_paths: Callable[[], dict[str, list[str]]] | None = None,
	):
		self._config = config or BudgetLedgerSourceConfig.from_site_config()
		self._connect = connect or _default_connect
		self._department_paths = department_paths or load_department_paths
		self._department_cache: dict[str, list[str]] | None = None

	def _department_map(self) -> dict[str, list[str]]:
		"""部门树每个实例只读一次；读不到就退回预算单自带的历史路径。"""
		if self._department_cache is None:
			try:
				self._department_cache = self._department_paths() or {}
			except Exception:
				LOGGER.warning("读取 OA 部门树失败，退回预算单里的部门路径", exc_info=True)
				self._department_cache = {}
		return self._department_cache

	@contextmanager
	def _connection(self) -> Iterator[Any]:
		connection = self._connect(ledger_connection_kwargs(self._config))
		try:
			yield connection
		finally:
			connection.close()

	def _fetch(self, sql: str, params: list[Any] | None = None) -> list[dict]:
		with self._connection() as connection, connection.cursor() as cursor:
			cursor.execute(sql, tuple(params or ()))
			return list(cursor.fetchall())

	# ------------------------------------------------------------ 列表
	def _segment_sql(
		self,
		kind: str,
		params: list[Any],
		*,
		start_month: str | None,
		end_month: str | None,
		status: str | None,
		keyword: str | None = None,
	) -> str:
		alias = "p" if kind == PRODUCTION else "n"
		table = "production_budget" if kind == PRODUCTION else "non_production_budget"
		amount_column = "monthly_budget_amount" if kind == PRODUCTION else "budget_amount"

		conditions = ["1=1"]
		month_expr = _month_expr(alias)
		if start_month:
			conditions.append(f"{month_expr} >= %s")
			params.append(start_month)
		if end_month:
			conditions.append(f"{month_expr} <= %s")
			params.append(end_month)
		conditions.append(_valid_status_sql(alias))
		conditions.append(_submitted_scope_sql(alias))
		if status:
			conditions.append(f"{alias}.status = %s")
			params.append(status)
		if keyword:
			conditions.append(
				f"({alias}.form_no ILIKE %s OR {alias}.dept_name ILIKE %s"
				f" OR {alias}.creator_name ILIKE %s OR {alias}.execution_region ILIKE %s)"
			)
			like = f"%{keyword}%"
			params.extend([like, like, like, like])
		conditions.append(_detail_exists_sql(alias))

		return f"""
		{kind}_filtered AS (
			SELECT
			  {alias}.id,
			  {alias}.form_no,
			  {alias}.process_instance_id,
			  {alias}.dept_name,
			  {alias}.dept_id,
			  {alias}.dept_path_ids,
			  {alias}.dept_path_names,
			  {alias}.budget_type,
			  {alias}.declaration_month,
			  {alias}.budget_month,
			  {alias}.application_date,
			  {alias}.execution_region,
			  {alias}.creator_name,
			  {alias}.create_time,
			  {alias}.status,
			  '{kind}'::text AS budget_kind,
			  {alias}.{amount_column} AS budget_amount,
			  {alias}.total_amount,
			  {month_expr} AS budget_month_key,
			  {_dept_key_expr(alias)} AS dept_key
			FROM {table} {alias}
			WHERE {" AND ".join(conditions)}
		),
		{kind}_picked AS (
			SELECT *,
			       ROW_NUMBER() OVER (
			         PARTITION BY dept_key, budget_month_key
			         ORDER BY create_time DESC NULLS LAST, id DESC
			       ) AS rn
			FROM {kind}_filtered
		)
		"""

	def _build_query(
		self,
		*,
		budget_type: str | None,
		start_month: str | None,
		end_month: str | None,
		status: str | None,
		keyword: str | None = None,
	) -> tuple[str, list[Any], str]:
		"""返回 ``(CTE 前缀, 参数, unioned 子查询)``。"""
		kinds = [PRODUCTION, NON_PRODUCTION]
		if budget_type in kinds:
			kinds = [budget_type]

		params: list[Any] = []
		segments = [
			self._segment_sql(
				kind,
				params,
				start_month=start_month,
				end_month=end_month,
				status=status,
				keyword=keyword,
			)
			for kind in kinds
		]
		unioned = " UNION ALL ".join(
			f"SELECT * FROM {kind}_picked WHERE rn = 1" for kind in kinds
		)
		return ("WITH " + ",\n".join(segments), params, unioned)

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
		"""分页查询预算列表；返回 ``{rows, total, counts, page, page_size}``。

		``counts`` 始终是「全部 / 生产 / 非生产」三种口径的条数，不受当前 tab 影响，
		供前端 tab 角标使用。
		"""
		page = max(int(page or 1), 1)
		page_size = min(max(int(page_size or 10), 1), 100)
		offset = (page - 1) * page_size
		keyword = (keyword or "").strip() or None

		count_cte, count_params, count_unioned = self._build_query(
			budget_type=None,
			start_month=start_month,
			end_month=end_month,
			status=status,
			keyword=keyword,
		)
		count_rows = self._fetch(
			f"""
			{count_cte}
			SELECT budget_kind,
			       COUNT(*) AS total,
			       COALESCE(SUM(COALESCE(total_amount, budget_amount, 0)), 0) AS amount
			FROM ({count_unioned}) AS merged
			GROUP BY budget_kind
			""",
			list(count_params),
		)
		counts = {PRODUCTION: 0, NON_PRODUCTION: 0}
		amounts = {PRODUCTION: 0.0, NON_PRODUCTION: 0.0}
		for row in count_rows:
			kind = row.get("budget_kind")
			counts[kind] = int(row.get("total") or 0)
			amounts[kind] = float(row.get("amount") or 0)
		counts["all"] = counts[PRODUCTION] + counts[NON_PRODUCTION]
		amounts["all"] = amounts[PRODUCTION] + amounts[NON_PRODUCTION]

		cte, params, unioned = self._build_query(
			budget_type=budget_type,
			start_month=start_month,
			end_month=end_month,
			status=status,
			keyword=keyword,
		)

		rows = self._fetch(
			f"""
			{cte}
			SELECT * FROM ({unioned}) AS merged
			ORDER BY create_time DESC NULLS LAST, id DESC
			LIMIT %s OFFSET %s
			""",
			[*params, page_size, offset],
		)
		department_map = self._department_map()
		for row in rows:
			company, department = department_presentation(
				row, department_map.get(str(row.get("dept_id") or ""))
			)
			row["company_display"] = company
			row["department_display"] = department
			row["budget_kind_label"] = "生产预算" if row.get("budget_kind") == PRODUCTION else "非生产预算"
			row["amount"] = float(row.get("total_amount") or row.get("budget_amount") or 0)

		active = budget_type if budget_type in counts else "all"
		return {
			"rows": rows,
			"total": counts[active],
			"counts": counts,
			"total_amount": round(amounts[active], 2),
			"page": page,
			"page_size": page_size,
		}

	# ------------------------------------------------------------ 统计
	def get_stats(self) -> dict:
		"""四个卡片：今日生产 / 今日非生产 / 生产总数 / 非生产总数。"""
		production_today = self._fetch(
			f"""
			SELECT COUNT(*) AS count
			FROM production_budget p
			WHERE p.create_time::date = CURRENT_DATE
			  AND {_valid_status_sql("p")}
			  AND {_submitted_scope_sql("p")}
			  AND {_detail_exists_sql("p")}
			"""
		)
		non_production_today = self._fetch(
			f"""
			SELECT COUNT(*) AS count
			FROM non_production_budget n
			WHERE n.create_time::date = CURRENT_DATE
			  AND {_valid_status_sql("n")}
			  AND {_submitted_scope_sql("n")}
			  AND {_detail_exists_sql("n")}
			"""
		)

		counts: dict[str, int] = {}
		for kind in (PRODUCTION, NON_PRODUCTION):
			cte, params, unioned = self._build_query(
				budget_type=kind,
				start_month=None,
				end_month=None,
				status=None,
			)
			rows = self._fetch(
				f"{cte} SELECT COUNT(*) AS total FROM ({unioned}) AS merged",
				params,
			)
			counts[kind] = int(rows[0].get("total") or 0) if rows else 0

		return {
			"production_today": int(production_today[0].get("count") or 0) if production_today else 0,
			"non_production_today": int(non_production_today[0].get("count") or 0)
			if non_production_today
			else 0,
			"production_total": counts[PRODUCTION],
			"non_production_total": counts[NON_PRODUCTION],
		}

	# ------------------------------------------------------------ 明细（报表导出用）
	_EXPENSE_DETAIL_TABLES = (
		("hr", "budget_hr"),
		("office", "budget_office"),
		("operation", "budget_operation"),
	)
	_PRODUCTION_DETAIL_TABLES = (
		("material", "budget_material"),
		("production", "budget_production"),
		("labor", "budget_labor"),
	)

	def _detail_rows(
		self, tables: tuple[tuple[str, str], ...], form_nos: list[str]
	) -> list[dict]:
		names = list(dict.fromkeys(str(name) for name in form_nos if name))
		if not names:
			return []
		rows: list[dict] = []
		for group, table in tables:
			for row in self._fetch(
				f"SELECT * FROM {table} WHERE form_no = ANY(%s) ORDER BY id", [names]
			):
				row["detail_group"] = group
				rows.append(row)
		return rows

	def list_expense_details(self, form_nos: list[str]) -> list[dict]:
		"""非生产预算明细：人资 / 办公场地 / 管理支出。"""
		return self._detail_rows(self._EXPENSE_DETAIL_TABLES, form_nos)

	def list_production_details(self, form_nos: list[str]) -> list[dict]:
		"""生产预算明细：物料 / 生产 / 人工。"""
		return self._detail_rows(self._PRODUCTION_DETAIL_TABLES, form_nos)
