"""实际支出只读数据源（PostgreSQL ``dingtalk_approval`` 库）。

对齐 ``dingtalk-budget/server/routes/list.js: fetchApprovalExpenseDetails`` 的口径：

- 只认「审批完成且结果为同意」的费用单：
  ``approval_completed_at IS NOT NULL`` + ``approval_status = 'COMPLETED'`` +
  ``raw_data.result ∈ agree/approved/pass/success/同意/已通过/通过``；
- 有部门拆分的单据按 ``approval_expense_dept_split`` 分摊，没有拆分才按申请部门整单计；
- 付款事件只取 ``status = 'confirmed'``，金额优先 ``base_currency_amount``；
- 日期一律按 UTC 折算：``(approval_completed_at AT TIME ZONE 'UTC')::date``，
  付款事件用 ``paid_at``。

分支与 ``list.js`` 一致：已通过（有拆分）/ 已通过（无拆分）/ 运营付款事件 /
采购付款事件 / 采购完成（无付款事件）/ 月结付款。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import Any

from .oa_approval_source import _runtime_config_value, shared_db_config
from .pg import fetch as pg_fetch

DEFAULT_DATABASE = "dingtalk_approval"

AGREED_RESULTS = ("agree", "approved", "pass", "success", "同意", "已通过", "通过")

SPLIT_JSON_COLUMNS = (
	"salary_by_department",
	"bonus_by_department",
	"office_equipment_by_department",
	"administrative_by_department",
	"social_insurance_by_department",
	"office_space_by_department",
	"individual_income_tax_by_department",
	"it_operation_by_department",
)


def _completed_where(alias: str) -> str:
	"""``completed-expense-policy.js`` 的 SQL 版本。"""
	results = ", ".join(f"'{value}'" for value in AGREED_RESULTS)
	return f"""
		AND {alias}.approval_completed_at IS NOT NULL
		AND UPPER(COALESCE(NULLIF(TRIM({alias}.approval_status), ''),
		                   NULLIF(TRIM({alias}.raw_data->>'status'), ''), 'NONE')) = 'COMPLETED'
		AND LOWER(COALESCE(NULLIF(TRIM({alias}.raw_data->>'result'), ''),
		                   NULLIF(TRIM({alias}.raw_data->>'flowResult'), ''),
		                   NULLIF(TRIM({alias}.raw_data->>'flow_result'), ''), ''))
		    IN ({results})
	"""


def _split_evidence(alias: str) -> str:
	"""拆分证据：规范表有行，或历史 JSONB 列非空。"""
	json_checks = " OR ".join(
		f"""COALESCE(jsonb_array_length(
			CASE WHEN jsonb_typeof({alias}.{column}) = 'array'
			     THEN {alias}.{column} ELSE '[]'::jsonb END), 0) > 0"""
		for column in SPLIT_JSON_COLUMNS
	)
	return f"""(
		EXISTS (SELECT 1 FROM approval_expense_dept_split split
		        WHERE split.business_id = {alias}.business_id)
		OR {json_checks}
	)"""


@dataclass(frozen=True)
class ExpenseSourceConfig:
	host: str = "localhost"
	port: int = 5432
	database: str = DEFAULT_DATABASE
	user: str = "postgres"
	password: str = ""
	connect_timeout: int = 3
	statement_timeout_ms: int = 15000
	application_name: str = "budget-expense"

	@classmethod
	def from_site_config(cls) -> ExpenseSourceConfig:
		"""连接信息复用站点 PG 入口，库名单独配 ``budget_expense_db_name``。"""
		shared = shared_db_config()
		return cls(
			host=shared["host"],
			port=int(shared["port"]),
			database=_runtime_config_value(
				"budget_expense_db_name", default=DEFAULT_DATABASE
			),
			user=shared["user"],
			password=shared["password"],
		)


def expense_connection_kwargs(config: ExpenseSourceConfig) -> dict:
	return {
		"host": config.host,
		"port": int(config.port),
		"dbname": config.database,
		"user": config.user,
		"password": config.password,
		"connect_timeout": int(config.connect_timeout),
		"application_name": config.application_name,
		"options": " ".join(
			[
				f"-c statement_timeout={int(config.statement_timeout_ms)}",
				"-c default_transaction_read_only=on",
			]
		),
	}


def _default_connect(kwargs: dict) -> Any:
	import psycopg
	from psycopg.rows import dict_row

	return psycopg.connect(**kwargs, row_factory=dict_row)


class ExpenseSource:
	"""已通过支出的明细拉取（只读）。"""

	def __init__(self, config: ExpenseSourceConfig | None = None, *, connect=None):
		self._config = config or ExpenseSourceConfig.from_site_config()
		self._connect = connect or _default_connect

	def _fetch(self, sql: str, params: list[Any]) -> list[dict]:
		# 连接按请求复用（见 integrations/pg.py），失败自动重试一次
		return pg_fetch(
			expense_connection_kwargs(self._config),
			sql,
			params,
			connect=self._connect,
			label="budget-expense",
		)

	def list_approved_expenses(self, start_date: date | str, end_date: date | str) -> list[dict]:
		"""返回 ``accounting_source / department / department_id / expense_type /
		category / amount / accounting_date / business_id / split_type`` 明细行。

		``split_type`` 只在部门拆分行上有值（``office_space`` / ``administrative``
		等），前端按它把支出拆到「运营/采购」「办公场地」「管理费」等独立分区。"""
		start = start_date.isoformat() if isinstance(start_date, date) else str(start_date)
		end = end_date.isoformat() if isinstance(end_date, date) else str(end_date)

		completed_op = _completed_where("o")
		split_op = _split_evidence("o")
		confirmed_event = (
			"event.status = 'confirmed' AND event.rule_version = 'authorized-comment-v1'"
		)

		sql = f"""
		WITH op AS (
			SELECT o.*,
			       COALESCE(NULLIF(o.base_currency_amount, 0), o.amount) AS effective_amount,
			       (o.approval_completed_at AT TIME ZONE 'UTC')::date AS accounting_date,
			       ({split_op}) AS has_split
			FROM approval_expense_operation o
			WHERE TRUE {completed_op}
			  AND (o.approval_completed_at AT TIME ZONE 'UTC')::date BETWEEN %s::date AND %s::date
		),
		purchase AS (
			SELECT p.*,
			       COALESCE(NULLIF(p.base_currency_amount, 0),
			                NULLIF(p.detail_summary_amount, 0), 0) AS effective_amount,
			       (p.approval_completed_at AT TIME ZONE 'UTC')::date AS accounting_date
			FROM approval_expense_purchase p
			WHERE TRUE {_completed_where('p')}
			  AND (p.approval_completed_at AT TIME ZONE 'UTC')::date BETWEEN %s::date AND %s::date
		)
		SELECT * FROM (
			-- 1) 已通过 + 有部门拆分：按拆分表分摊
			SELECT 'completed_department_split' AS accounting_source,
			       op.business_id, op.process_instance_id,
			       COALESCE(NULLIF(BTRIM(split.department), ''), op.applicant_department) AS department,
			       COALESCE(NULLIF(BTRIM(split.department_id), ''), op.applicant_department_id) AS department_id,
			       op.expense_type,
			       split.category_name AS category,
			       SUM(split.amount) AS amount,
			       op.accounting_date,
			       split.split_type AS split_type
			FROM op
			JOIN approval_expense_dept_split split ON split.business_id = op.business_id
			WHERE op.has_split
			GROUP BY 1, 2, 3, 4, 5, 6, 7, 9, 10

			UNION ALL

			-- 2) 已通过但无拆分、也没有确认付款事件：整单按申请部门
			SELECT 'completed_approval_fallback', op.business_id, op.process_instance_id,
			       op.applicant_department, op.applicant_department_id,
			       op.expense_type, NULL, op.effective_amount, op.accounting_date,
			       NULL AS split_type
			FROM op
			WHERE NOT op.has_split
			  AND NOT EXISTS (
			      SELECT 1 FROM approval_expense_payment_events event
			      WHERE event.business_id = op.business_id AND {confirmed_event}
			  )

			UNION ALL

			-- 3) 运营付款事件：按实际付款日期计入
			SELECT 'payment_event_operation', op.business_id, op.process_instance_id,
			       op.applicant_department, op.applicant_department_id,
			       op.expense_type, NULL,
			       COALESCE(NULLIF(event.base_currency_amount, 0), event.amount),
			       (event.paid_at AT TIME ZONE 'UTC')::date,
			       NULL AS split_type
			FROM approval_expense_payment_events event
			JOIN approval_expense_operation op ON op.business_id = event.business_id
			WHERE event.expense_kind = 'operation' AND {confirmed_event}
			  AND (event.paid_at AT TIME ZONE 'UTC')::date BETWEEN %s::date AND %s::date

			UNION ALL

			-- 4) 采购付款事件
			SELECT 'payment_event_purchase', purchase.business_id, purchase.process_instance_id,
			       purchase.applicant_department, purchase.applicant_department_id,
			       purchase.expense_classification, NULL,
			       COALESCE(NULLIF(event.base_currency_amount, 0), event.amount),
			       (event.paid_at AT TIME ZONE 'UTC')::date,
			       NULL AS split_type
			FROM approval_expense_payment_events event
			JOIN purchase ON purchase.business_id = event.business_id
			WHERE event.expense_kind = 'purchase' AND {confirmed_event}
			  AND (event.paid_at AT TIME ZONE 'UTC')::date BETWEEN %s::date AND %s::date

			UNION ALL

			-- 5) 采购已通过、无付款事件
			SELECT 'purchase_completed', purchase.business_id, purchase.process_instance_id,
			       purchase.applicant_department, purchase.applicant_department_id,
			       purchase.expense_classification, NULL,
			       purchase.effective_amount, purchase.accounting_date,
			       NULL AS split_type
			FROM purchase
			WHERE NOT EXISTS (
			      SELECT 1 FROM approval_expense_payment_events event
			      WHERE event.business_id = purchase.business_id AND {confirmed_event}
			  )

			UNION ALL

			-- 6) 月结付款：硬编码为「月结付款」科目
			SELECT 'monthly_settlement', monthly.business_id, monthly.process_instance_id,
			       COALESCE(NULLIF(BTRIM(monthly.resolved_department), ''),
			                monthly.applicant_department),
			       COALESCE(NULLIF(BTRIM(monthly.resolved_department_id), ''),
			                monthly.applicant_department_id),
			       '月结付款', '月结付款',
			       COALESCE(NULLIF(monthly.base_currency_amount, 0), monthly.total_amount, 0),
			       (event.paid_at AT TIME ZONE 'UTC')::date,
			       NULL AS split_type
			FROM approval_expense_monthly_settlement monthly
			JOIN approval_expense_payment_events event
			  ON event.business_id = monthly.business_id
			 AND event.expense_kind = 'monthly_settlement'
			 AND event.status = 'confirmed'
			WHERE (event.paid_at AT TIME ZONE 'UTC')::date BETWEEN %s::date AND %s::date
		) AS rows
		WHERE amount IS NOT NULL AND amount <> 0
		ORDER BY accounting_date, department
		"""
		params = [start, end, start, end, start, end, start, end, start, end]
		return self._fetch(sql, params)

	def load_detail_meta(self, business_ids: list[str]) -> dict[str, dict]:
		"""按业务编号补详情列：事项说明、完成/付款时间、付款证据（备注）。"""
		ids = [str(value).strip() for value in business_ids or [] if str(value or "").strip()]
		if not ids:
			return {}
		meta: dict[str, dict] = {}
		for row in self._fetch(
			"""
			SELECT business_id, form_name AS title, matter_description AS description,
			       approval_completed_at AS completed_at
			FROM approval_expense_operation
			WHERE business_id = ANY(%s)
			""",
			[ids],
		):
			meta[str(row.get("business_id") or "").strip()] = {
				"title": str(row.get("title") or "").strip(),
				"description": str(row.get("description") or "").strip(),
				"completed_at": row.get("completed_at"),
				"evidence": "",
				"paid_at": None,
			}
		for row in self._fetch(
			"""
			SELECT business_id, form_name AS title, approval_completed_at AS completed_at
			FROM approval_expense_purchase
			WHERE business_id = ANY(%s)
			""",
			[ids],
		):
			meta.setdefault(
				str(row.get("business_id") or "").strip(),
				{
					"title": str(row.get("title") or "").strip(),
					"description": "",
					"completed_at": row.get("completed_at"),
					"evidence": "",
					"paid_at": None,
				},
			)
		for row in self._fetch(
			"""
			SELECT business_id, evidence_text, paid_at
			FROM approval_expense_payment_events
			WHERE business_id = ANY(%s) AND status = 'confirmed'
			ORDER BY paid_at NULLS LAST, id
			""",
			[ids],
		):
			entry = meta.setdefault(
				str(row.get("business_id") or "").strip(),
				{
					"title": "",
					"description": "",
					"completed_at": None,
					"evidence": "",
					"paid_at": None,
				},
			)
			entry["evidence"] = entry.get("evidence") or str(row.get("evidence_text") or "").strip()
			entry["paid_at"] = entry.get("paid_at") or row.get("paid_at")
		return meta
