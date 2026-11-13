"""预算额度配置只读数据源（PostgreSQL ``budget_system.budget_effective_quota``）。

对齐 ``dingtalk-budget`` 的原实现（``server/routes/quota-configurations.js`` 与
``server/services/quota-configuration.js``）：

- 只统计 ``status = 'effective'`` 的额度，失效记录保留但不出现在配置页；
- 组织层级直接取 ``group_dept_id`` / ``company_dept_id`` / ``department_id``，
  不按部门名称猜；``organization_is_current`` 用于筛选选项；
- 金额口径：``total_amount`` 是唯一参与余额计算的额度，科目明细
  ``budget_effective_quota_subject.amount`` 只用于科目统计，两者不一致时由
  ``detail_amount_mismatch`` 标记。

连接参数复用站点唯一的 PG 入口（``shared_db_config()``），库名走
``budget_ledger_db_name``——额度和台账在同一个库。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .oa_approval_source import shared_database_name, shared_db_config
from .pg import fetch as pg_fetch

DEFAULT_DATABASE = "budget_system"
EFFECTIVE = "effective"


@dataclass(frozen=True)
class QuotaSourceConfig:
	host: str = "localhost"
	port: int = 5432
	database: str = DEFAULT_DATABASE
	user: str = "postgres"
	password: str = ""
	connect_timeout: int = 3
	statement_timeout_ms: int = 15000
	application_name: str = "budget-quota"

	@classmethod
	def from_site_config(cls) -> QuotaSourceConfig:
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


def quota_connection_kwargs(config: QuotaSourceConfig) -> dict:
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
	import psycopg
	from psycopg.rows import dict_row

	return psycopg.connect(**kwargs, row_factory=dict_row)


class QuotaSource:
	"""``budget_effective_quota`` 的只读查询集合。"""

	def __init__(self, config: QuotaSourceConfig | None = None, *, connect=None):
		self._config = config or QuotaSourceConfig.from_site_config()
		self._connect = connect or _default_connect

	def _fetch(self, sql: str, params: list[Any] | None = None) -> list[dict]:
		# 连接按请求复用（见 integrations/pg.py），失败自动重试一次
		return pg_fetch(
			quota_connection_kwargs(self._config),
			sql,
			params,
			connect=self._connect,
			label="budget-quota",
		)

	def _one(self, sql: str, params: list[Any] | None = None) -> dict:
		rows = self._fetch(sql, params)
		return rows[0] if rows else {}

	# ------------------------------------------------------------ 筛选
	@staticmethod
	def _where(
		*,
		budget_year=None,
		budget_month=None,
		group_dept_id=None,
		company_dept_id=None,
		department_id=None,
		budget_type=None,
		subject_name=None,
	) -> tuple[str, list[Any]]:
		clauses = ["q.status = %s"]
		params: list[Any] = [EFFECTIVE]

		if str(budget_year or "").strip():
			clauses.append("q.budget_year = %s")
			params.append(int(budget_year))
		for column, value in (
			("budget_month", budget_month),
			("group_dept_id", group_dept_id),
			("company_dept_id", company_dept_id),
			("department_id", department_id),
			("budget_type", budget_type),
		):
			text = str(value or "").strip()
			if text:
				clauses.append(f"q.{column} = %s")
				params.append(text)
		if str(subject_name or "").strip():
			clauses.append(
				"EXISTS (SELECT 1 FROM budget_effective_quota_subject s "
				"WHERE s.quota_id = q.id AND s.subject_name = %s)"
			)
			params.append(str(subject_name).strip())
		return " AND ".join(clauses), params

	# ------------------------------------------------------------ 选项
	def get_options(self) -> dict:
		"""筛选下拉候选值；组织类选项只取 ``organization_is_current = true``。"""
		base = "FROM budget_effective_quota q WHERE q.status = %s"
		years = self._fetch(
			f"SELECT DISTINCT q.budget_year AS value {base} AND q.budget_year IS NOT NULL "
			"ORDER BY 1 DESC",
			[EFFECTIVE],
		)
		months = self._fetch(
			f"SELECT DISTINCT q.budget_month AS value {base} "
			"AND NULLIF(BTRIM(q.budget_month), '') IS NOT NULL ORDER BY 1",
			[EFFECTIVE],
		)
		budget_types = self._fetch(
			f"SELECT DISTINCT q.budget_type AS value {base} "
			"AND NULLIF(BTRIM(q.budget_type), '') IS NOT NULL ORDER BY 1",
			[EFFECTIVE],
		)

		def org_options(id_column: str, name_column: str, extra: str = "") -> list[dict]:
			return self._fetch(
				f"""
				SELECT DISTINCT NULLIF(BTRIM(q.{id_column}), '') AS id,
				       COALESCE(NULLIF(BTRIM(q.{name_column}), ''), '待确认') AS name
				{base}
				  AND q.organization_is_current = true
				  AND NULLIF(BTRIM(q.{id_column}), '') IS NOT NULL
				  {extra}
				ORDER BY name
				""",
				[EFFECTIVE],
			)

		subjects = self._fetch(
			"""
			SELECT DISTINCT s.subject_name AS value
			FROM budget_effective_quota q
			JOIN budget_effective_quota_subject s ON s.quota_id = q.id
			WHERE q.status = %s AND NULLIF(BTRIM(s.subject_name), '') IS NOT NULL
			ORDER BY 1
			""",
			[EFFECTIVE],
		)

		return {
			"years": [row["value"] for row in years],
			"months": [row["value"] for row in months],
			"budget_types": [row["value"] for row in budget_types],
			"groups": org_options("group_dept_id", "group_name"),
			"companies": org_options("company_dept_id", "company_name"),
			"departments": org_options(
				"department_id",
				"department_name",
				"AND q.department_id IS DISTINCT FROM q.company_dept_id",
			),
			"subjects": [row["value"] for row in subjects],
		}

	# ------------------------------------------------------------ 汇总
	def get_summary(self, **filters) -> dict:
		"""4 张统计卡 + 组织额度汇总（集团 / 子公司 / 部门 / 科目）。"""
		where, params = self._where(**filters)
		totals = self._one(
			f"""
			SELECT COUNT(*)::int AS quota_count,
			       COALESCE(SUM(q.total_amount), 0) AS total_amount,
			       COUNT(*) FILTER (WHERE q.detail_amount_mismatch)::int
			           AS detail_amount_mismatch_count,
			       COUNT(DISTINCT NULLIF(BTRIM(q.department_id), ''))::int
			           AS department_count
			FROM budget_effective_quota q
			WHERE {where}
			""",
			params,
		)

		def rollup(id_column: str, name_column: str, extra: str = "") -> list[dict]:
			return self._fetch(
				f"""
				SELECT NULLIF(BTRIM(q.{id_column}), '') AS id,
				       COALESCE(NULLIF(BTRIM(q.{name_column}), ''), '待确认') AS name,
				       COUNT(*)::int AS quota_count,
				       COALESCE(SUM(q.total_amount), 0) AS total_amount
				FROM budget_effective_quota q
				WHERE {where} {extra}
				GROUP BY 1, 2
				ORDER BY total_amount DESC, name ASC
				""",
				params,
			)

		subjects = self._fetch(
			f"""
			SELECT s.subject_type,
			       s.subject_code,
			       s.subject_name AS name,
			       COUNT(DISTINCT q.id)::int AS quota_count,
			       COALESCE(SUM(s.amount), 0) AS total_amount
			FROM budget_effective_quota q
			JOIN budget_effective_quota_subject s ON s.quota_id = q.id
			WHERE {where}
			GROUP BY 1, 2, 3
			ORDER BY total_amount DESC, name ASC
			""",
			params,
		)

		return {
			"totals": {
				"quota_count": int(totals.get("quota_count") or 0),
				"total_amount": float(totals.get("total_amount") or 0),
				"detail_amount_mismatch_count": int(
					totals.get("detail_amount_mismatch_count") or 0
				),
				"department_count": int(totals.get("department_count") or 0),
			},
			"rollups": {
				"group": rollup("group_dept_id", "group_name"),
				"company": rollup("company_dept_id", "company_name"),
				"department": rollup(
					"department_id",
					"department_name",
					"AND q.department_id IS DISTINCT FROM q.company_dept_id",
				),
				"subject": subjects,
			},
		}

	# ------------------------------------------------------------ 明细
	def list_quotas(self, *, page: int = 1, page_size: int = 20, **filters) -> dict:
		where, params = self._where(**filters)
		page = max(int(page or 1), 1)
		page_size = min(max(int(page_size or 20), 1), 200)

		total = self._one(
			f"SELECT COUNT(*)::int AS total FROM budget_effective_quota q WHERE {where}",
			params,
		)
		rows = self._fetch(
			f"""
			SELECT q.id,
			       q.budget_year,
			       q.budget_month,
			       q.group_name,
			       q.company_name,
			       q.department_name,
			       q.department_id,
			       q.company_dept_id,
			       (q.department_id IS NOT DISTINCT FROM q.company_dept_id)
			           AS department_is_company,
			       q.total_amount,
			       q.detail_amount,
			       q.detail_amount_mismatch,
			       q.owner_name,
			       q.budget_type,
			       q.source_form_no AS form_no,
			       COALESCE(SUM(s.amount), 0) AS subject_amount,
			       COALESCE(
			           jsonb_agg(
			               jsonb_build_object('name', s.subject_name, 'amount', s.amount)
			               ORDER BY s.line_no
			           ) FILTER (WHERE s.line_no IS NOT NULL),
			           '[]'::jsonb
			       ) AS subjects
			FROM budget_effective_quota q
			LEFT JOIN budget_effective_quota_subject s ON s.quota_id = q.id
			WHERE {where}
			GROUP BY q.id
			ORDER BY q.budget_month DESC NULLS LAST,
			         q.approval_completed_at DESC NULLS LAST,
			         q.id DESC
			LIMIT %s OFFSET %s
			""",
			[*params, page_size, (page - 1) * page_size],
		)
		return {
			"rows": rows,
			"total": int(total.get("total") or 0),
			"page": page,
			"page_size": page_size,
		}
