"""把外部 ``budget_system`` 的预算额度同步到站点库的 ``Budget Quota``。

额度是预算明细按 ``subject_type``（hr / office / operation）的科目聚合，因此落库时
主表存额度单、子表 ``Budget Quota Subject`` 存科目行，两者用 ``form_no`` 关联。

口径与 ``integrations/quota_source.py`` 保持一致：

- ``status`` = effective / deactivated 原样保留，不因审批终止而丢弃；
- ``approval_status`` / ``approval_result`` 单独落库，``TERMINATED + agree``
  这类「流程终止但结果为同意」的单据因此仍可识别、仍计入额度；
- ``total_amount`` 是唯一参与余额计算的额度，``detail_amount`` 与科目汇总
  不一致时由 ``detail_amount_mismatch`` 标记；
- **只同步预算模板**（``BUDGET_PROCESS_CODE``，与台账同步共用同一个常量）。
  额度表由上游按流程同步而来，这里再用同一 process_code 校验一次：混入其它
  审批流程的额度会被忽略并记 Error Log，不会静默写进本地额度。

可靠性设计（全量对账，不做增量）：

1. **互斥**：Redis ``SET NX`` 保证同一站点只有一个同步在跑。刻意**不用**
   ``frappe.utils.synchronization.filelock``——它基于 ``flock``，在 Docker Desktop
   的 macOS 挂载卷上实测不互斥（两个进程同时进入临界区，子表一度插成 950 行）；
   Redis 锁跨进程 / 跨机器都有效，也是生产多 worker 下唯一可靠的方案。
2. **全或无**：写库与删除在同一个事务内，校验不通过则 ``rollback``，本地保持
   上一次的完整快照，不会停在「主表已更新、子表已清空」的中间态。
3. **自校验**：提交前比对本地条数 / 金额合计 / 子表行数与源端，任何一项不符
   就整批回滚并抛错。
4. **可观测**：最近一次结果写缓存，成功失败都能用 ``get_quota_sync_status``
   查到；失败另记 Error Log（含 traceback）。

额度表只有几十条、科目几百条，全量重写比增量更简单也更不容易漏数据；
源端删除的记录也会因此被正确清理。
"""

from __future__ import annotations

import json
from typing import Any

import frappe

from ..integrations.pg import fetch as pg_fetch
from ..integrations.quota_source import QuotaSourceConfig, quota_connection_kwargs
from .oa_budget_parser import BUDGET_PROCESS_CODE
from .sync_lock import is_locked, sync_lock

QUOTA_DOCTYPE = "Budget Quota"
SUBJECT_DOCTYPE = "Budget Quota Subject"

SYNC_LOCK_NAME = "budget-quota-sync"
CACHE_KEY = "budget_quota_sync:last"
STATUS_TTL = 30 * 24 * 3600

QUOTA_SQL = """
SELECT id,
       corp_id,
       process_instance_id,
       source_form_no,
       source_process_code,
       budget_type,
       approval_status,
       approval_result,
       budget_year,
       budget_month,
       group_dept_id,
       group_name,
       company_dept_id,
       company_name,
       department_id,
       department_name,
       department_path_ids,
       department_path_names,
       department_source,
       organization_is_current,
       owner_user_id,
       owner_name,
       total_amount,
       detail_amount,
       detail_amount_mismatch,
       status,
       to_char(approval_completed_at AT TIME ZONE 'UTC', 'YYYY-MM-DD HH24:MI:SS')
           AS approval_completed_at,
       to_char(effective_at AT TIME ZONE 'UTC', 'YYYY-MM-DD HH24:MI:SS') AS effective_at,
       to_char(deactivated_at AT TIME ZONE 'UTC', 'YYYY-MM-DD HH24:MI:SS') AS deactivated_at,
       to_char(created_at AT TIME ZONE 'UTC', 'YYYY-MM-DD HH24:MI:SS') AS source_created_at,
       to_char(updated_at AT TIME ZONE 'UTC', 'YYYY-MM-DD HH24:MI:SS') AS source_updated_at
  FROM budget_effective_quota
 ORDER BY id
"""

SUBJECT_SQL = """
SELECT quota_id, line_no, subject_type, subject_code, subject_name, amount, source_table
  FROM budget_effective_quota_subject
 ORDER BY quota_id, line_no
"""


class QuotaSyncVerificationError(frappe.ValidationError):
	"""同步后的本地数据与源端对不上（条数 / 金额 / 子表行数）。"""


# ---------------------------------------------------------------- 缓存
def _cache_client():
	cache = frappe.cache
	return cache() if callable(cache) else cache


def _remember(payload: dict) -> None:
	"""最近一次同步结果写缓存，供 ``get_quota_sync_status`` 查看。"""
	try:
		_cache_client().set_value(CACHE_KEY, payload, expires_in_sec=STATUS_TTL)
	except Exception:  # 缓存不可用不能影响同步本身
		pass


# ---------------------------------------------------------------- 字段映射
def _json_text(value: Any) -> str | None:
	"""jsonb 列 → DocType 的 Code(JSON) 文本；空值保持 NULL。"""
	if value in (None, "", [], {}):
		return None
	if isinstance(value, str):
		return value
	return json.dumps(value, ensure_ascii=False)


def _month_start(value: Any) -> str | None:
	"""``2026-05`` → ``2026-05-01``：Date 字段需要完整日期才能用原生日历筛选。"""
	text = str(value or "").strip()
	if not text:
		return None
	return f"{text}-01" if len(text) == 7 and text[4] == "-" else text


def _flag(value: Any) -> int:
	return 1 if value else 0


def _amount(value: Any) -> float:
	return float(value or 0)


def _quota_values(row: dict, synced_at: str) -> dict:
	department_id = str(row.get("department_id") or "").strip()
	company_dept_id = str(row.get("company_dept_id") or "").strip()
	return {
		"form_no": row.get("source_form_no"),
		"remote_id": row.get("id"),
		"corp_id": row.get("corp_id"),
		"process_instance_id": row.get("process_instance_id"),
		"oa_process_code": row.get("source_process_code"),
		"budget_type": row.get("budget_type"),
		"budget_year": row.get("budget_year"),
		"budget_month": _month_start(row.get("budget_month")),
		"group_dept_id": row.get("group_dept_id"),
		"group_name": row.get("group_name"),
		"company_dept_id": row.get("company_dept_id"),
		"company_name": row.get("company_name"),
		"department_id": row.get("department_id"),
		"department_name": row.get("department_name"),
		"department_is_company": _flag(
			department_id and department_id == company_dept_id
		),
		"department_source": row.get("department_source"),
		"organization_is_current": _flag(row.get("organization_is_current")),
		"dept_path_ids": _json_text(row.get("department_path_ids")),
		"dept_path_names": _json_text(row.get("department_path_names")),
		"total_amount": _amount(row.get("total_amount")),
		"detail_amount": _amount(row.get("detail_amount")),
		"detail_amount_mismatch": _flag(row.get("detail_amount_mismatch")),
		"owner_user_id": row.get("owner_user_id"),
		"owner_name": row.get("owner_name"),
		"status": row.get("status"),
		"approval_status": row.get("approval_status"),
		"approval_result": row.get("approval_result"),
		"effective_at": row.get("effective_at"),
		"deactivated_at": row.get("deactivated_at"),
		"approval_completed_at": row.get("approval_completed_at"),
		"source_created_at": row.get("source_created_at"),
		"source_updated_at": row.get("source_updated_at"),
		"synced_at": synced_at,
	}


def _connection_kwargs() -> dict:
	return quota_connection_kwargs(QuotaSourceConfig.from_site_config())


def _fetch_quota_rows() -> list[dict]:
	return pg_fetch(_connection_kwargs(), QUOTA_SQL, label="budget-quota-sync")


def _fetch_subject_rows() -> list[dict]:
	return pg_fetch(_connection_kwargs(), SUBJECT_SQL, label="budget-quota-sync")


def _group_subjects(rows: list[dict], quota_id_to_form: dict) -> dict[str, list[dict]]:
	grouped: dict[str, list[dict]] = {}
	for row in rows:
		form_no = quota_id_to_form.get(row.get("quota_id"))
		if not form_no:
			continue
		grouped.setdefault(form_no, []).append(
			{
				"line_no": row.get("line_no"),
				"subject_type": row.get("subject_type"),
				"subject_code": row.get("subject_code"),
				"subject_name": row.get("subject_name"),
				"amount": _amount(row.get("amount")),
				"source_table": row.get("source_table"),
			}
		)
	return grouped


# ---------------------------------------------------------------- 校验
def _local_snapshot() -> dict:
	rows = frappe.db.sql(
		f"SELECT COUNT(*) AS total, COALESCE(SUM(total_amount), 0) AS amount"
		f" FROM `tab{QUOTA_DOCTYPE}`",
		as_dict=True,
	)
	row = rows[0] if rows else {}
	return {
		"quota_count": int(row.get("total") or 0),
		"total_amount": round(float(row.get("amount") or 0), 2),
		"subject_count": frappe.db.count(SUBJECT_DOCTYPE),
	}


def _verify(*, remote_count: int, remote_amount: float, remote_subject_count: int) -> dict:
	"""提交前自校验：条数 / 金额 / 子表行数必须与源端一致，否则整批回滚。"""
	local = _local_snapshot()
	problems = []
	if local["quota_count"] != remote_count:
		problems.append(f"额度条数 本地 {local['quota_count']} ≠ 源端 {remote_count}")
	if local["total_amount"] != round(remote_amount, 2):
		problems.append(
			f"额度金额合计 本地 {local['total_amount']} ≠ 源端 {round(remote_amount, 2)}"
		)
	if local["subject_count"] != remote_subject_count:
		problems.append(
			f"科目行数 本地 {local['subject_count']} ≠ 源端 {remote_subject_count}"
		)
	if problems:
		raise QuotaSyncVerificationError("；".join(problems))
	return local


# ---------------------------------------------------------------- 主流程
def _alert_other_process(rows: list[dict]) -> None:
	"""额度表混入非预算流程时留痕：不静默丢弃，便于发现上游口径变化。"""
	others = [
		str(row.get("source_form_no") or "")
		for row in rows
		if str(row.get("source_process_code") or "").strip() != BUDGET_PROCESS_CODE
	]
	frappe.log_error(
		title="预算额度同步忽略了非预算流程的单据",
		message=(
			f"只同步 process_code = {BUDGET_PROCESS_CODE} 的额度，"
			f"本次忽略 {len(others)} 条其它流程的单据。\n"
			f"form_no 示例：{', '.join(others[:20])}"
		),
	)


def _run(*, quota_rows=None, subject_rows=None) -> dict:
	source_rows = _fetch_quota_rows() if quota_rows is None else list(quota_rows)
	# 额度表由上游按流程同步，这里用台账同一个 process_code 再校验一次
	rows = [
		row
		for row in source_rows
		if str(row.get("source_process_code") or "").strip() == BUDGET_PROCESS_CODE
	]
	ignored = len(source_rows) - len(rows)
	if ignored:
		_alert_other_process(source_rows)
	if not rows:
		# 源端暂时读不到数据时不清理本地，避免一次空结果把本地清空
		return {
			"skipped": True,
			"reason": "外部额度表没有预算模板的额度",
			"remote_total": 0,
			"ignored_other_process": ignored,
		}

	subjects = _fetch_subject_rows() if subject_rows is None else list(subject_rows)
	synced_at = frappe.utils.now()
	created = 0
	updated = 0
	names: list[str] = []
	quota_id_to_form: dict = {}

	for row in rows:
		values = _quota_values(row, synced_at)
		form_no = (values.get("form_no") or "").strip()
		if not form_no:
			continue
		values["form_no"] = form_no
		quota_id_to_form[row.get("id")] = form_no
		if frappe.db.exists(QUOTA_DOCTYPE, form_no):
			frappe.db.set_value(QUOTA_DOCTYPE, form_no, values, update_modified=False)
			updated += 1
		else:
			frappe.get_doc({"doctype": QUOTA_DOCTYPE, **values}).insert(
				ignore_permissions=True, ignore_mandatory=True
			)
			created += 1
		names.append(form_no)

	# 子表全量重建：额度单数量小，整表重写比逐单比对更简单，也能带走源端已删的科目行
	grouped = _group_subjects(subjects, quota_id_to_form)
	frappe.db.delete(SUBJECT_DOCTYPE)
	subject_count = 0
	for form_no in names:
		for line in grouped.get(form_no, []):
			doc = {key: value for key, value in line.items() if value not in (None, "")}
			doc.update(
				{
					"doctype": SUBJECT_DOCTYPE,
					"quota_form_no": form_no,
					"synced_at": synced_at,
				}
			)
			frappe.get_doc(doc).insert(ignore_permissions=True, ignore_mandatory=True)
			subject_count += 1

	removed = 0
	stale = (
		frappe.get_all(QUOTA_DOCTYPE, filters={"form_no": ["not in", names]}, pluck="name")
		if names
		else []
	)
	for name in stale:
		frappe.delete_doc(QUOTA_DOCTYPE, name, ignore_permissions=True, force=True)
		removed += 1

	verified = _verify(
		remote_count=len(names),
		remote_amount=sum(_amount(row.get("total_amount")) for row in rows),
		remote_subject_count=subject_count,
	)

	return {
		"remote_total": len(rows),
		"source_total": len(source_rows),
		"ignored_other_process": ignored,
		"created": created,
		"updated": updated,
		"removed": removed,
		"subject_rows": subject_count,
		"local_total": verified["quota_count"],
		"local_amount": verified["total_amount"],
		"verified": True,
	}


def sync_quotas(
	*,
	quota_rows: list[dict] | None = None,
	subject_rows: list[dict] | None = None,
) -> dict:
	"""同步入口：加锁 → 全量对账 → 自校验 → 提交；失败整批回滚。

	``quota_rows`` / ``subject_rows`` 仅供单测注入替身，省略时直连 PG。
	"""
	started_at = frappe.utils.now()
	with sync_lock(SYNC_LOCK_NAME) as acquired:
		if not acquired:
			result = {"skipped": True, "reason": "另一个额度同步正在运行，本次跳过"}
			_remember(
				{
					"status": "skipped",
					"started_at": started_at,
					"finished_at": frappe.utils.now(),
					**result,
				}
			)
			return result

		try:
			result = _run(quota_rows=quota_rows, subject_rows=subject_rows)
		except Exception as error:
			frappe.db.rollback()
			_remember(
				{
					"status": "failed",
					"started_at": started_at,
					"finished_at": frappe.utils.now(),
					"error": f"{type(error).__name__}: {error}",
				}
			)
			frappe.log_error(
				title="预算额度同步失败",
				message=frappe.get_traceback(with_context=True),
			)
			raise

		if result.get("skipped"):
			_remember(
				{
					"status": "skipped",
					"started_at": started_at,
					"finished_at": frappe.utils.now(),
					**result,
				}
			)
			return result

		frappe.db.commit()
		_remember(
			{
				"status": "success",
				"started_at": started_at,
				"finished_at": frappe.utils.now(),
				**result,
			}
		)
		return result


@frappe.whitelist()
def run_quota_sync() -> dict:
	"""手动同步（同步执行，适合排查）：``bench --site <site> execute ...run_quota_sync``。"""
	frappe.only_for(("System Manager",))
	return sync_quotas()


@frappe.whitelist()
def enqueue_quota_sync() -> dict:
	"""异步同步：入队 long 队列，``job_id`` + ``deduplicate`` 保证不会重复排队。

	定时任务是进程级的，站点关闭 scheduler 时（``enable_scheduler: 0``）不会自动跑；
	此时由外部 cron / 页面按钮调这个接口即可，行为和每日任务完全一致。
	"""
	frappe.only_for(("System Manager",))
	frappe.enqueue(
		"budget.services.quota_sync.sync_quotas",
		queue="long",
		timeout=900,
		job_id="budget-quota-sync",
		deduplicate=True,
	)
	return {"queued": True, "job_id": "budget-quota-sync"}


@frappe.whitelist()
def get_quota_sync_status() -> dict:
	"""最近一次同步结果 + 本地实际规模，用于确认同步是否在正常跑。"""
	frappe.only_for(("System Manager",))
	try:
		last = _cache_client().get_value(CACHE_KEY) or {}
	except Exception:
		last = {}
	return {
		"last": last,
		"local": _local_snapshot(),
		"lock_held": is_locked(SYNC_LOCK_NAME),
	}
