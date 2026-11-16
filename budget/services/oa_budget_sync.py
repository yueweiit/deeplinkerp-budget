"""从钉钉 OA 归档同步预算台账到站点库（MariaDB）。

流程：
    ding_approval_instance (PostgreSQL)
        → parse_budget_instance 解析表单
        → 按 (dept_key, budget_month) 去重取最新
        → Budget Ledger Entry / Budget Ledger Detail (MariaDB)

页面此后只读本地表，外部 PG 只在同步时使用。
"""

from __future__ import annotations

import hashlib
import json
import time
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Any

import frappe

from ..integrations.oa_approval_source import _runtime_config_value, shared_db_config
from ..integrations.pg import fetch as pg_fetch
from .oa_budget_parser import BUDGET_PROCESS_CODE, dedupe_latest, parse_budget_instance
from .sync_lock import is_locked, sync_lock

ENTRY_DOCTYPE = "Budget Ledger Entry"
DETAIL_DOCTYPE = "Budget Ledger Detail"
SYNC_LOG_DOCTYPE = "Budget Ledger Sync Log"
COMPANY_LEVEL_INDEX = 2
LEDGER_LOCK_NAME = "budget-ledger-sync"
LEDGER_JOB_ID = "budget-ledger-sync"
CACHE_KEY = "budget_ledger_sync:last"
STATUS_TTL = 30 * 24 * 3600
# 单次同步的时间预算：全量拉取 + 全表重建，正常几秒内完成，超过就说明上游或库有问题，
# 与其让 worker 一直占着锁和事务，不如中断并回滚，等下一轮
SYNC_TIMEOUT_SECONDS = 300
SYNC_LOG_RETENTION_DAYS = 30
# 增量同步靠每单指纹跳过未变的单据；但增量不会纠正「本地被改坏 / 历史 bug 留下的脏数据」，
# 所以每天强制整表对账一次，兜住自愈能力
FORCE_FULL_INTERVAL_HOURS = 24
# 与 api/budget_ledger.py 的 READ_ROLES 一致：能看台账的人就能触发「打开即刷新」
READ_ROLES = ("System Manager", "Accounts Manager")

INSTANCE_SQL = """
SELECT process_instance_id,
       process_code,
       status,
       result,
       form_component_values,
       raw_payload,
       originator_dept_id,
       originator_dept_name,
       originator_user_name,
       to_char(create_time AT TIME ZONE 'UTC', 'YYYY-MM-DD"T"HH24:MI:SS') AS create_time
  FROM ding_approval_instance
 WHERE process_code = %s
   AND status = 'COMPLETED'
   AND result = 'agree'
   AND deleted_at IS NULL
 ORDER BY create_time
"""

# 不能只取 is_current：台账里的历史部门（已撤销）照样要显示层级路径，
# 同一 dept_id 可能有多条快照，优先取当前版本、其次取最近同步的一条
DEPARTMENT_SQL = """
SELECT DISTINCT ON (dept_id) dept_id, path_ids, path_names
  FROM ding_department_tree
 WHERE corp_id = %s
 ORDER BY dept_id, is_current DESC, last_sync_at DESC NULLS LAST
"""

ENTRY_FIELDS = (
	"form_no",
	"process_instance_id",
	"company",
	"department",
	"department_id",
	"creator_name",
	"dept_name",
	"dept_key",
	"budget_kind",
	"application_date",
	"budget_month",
	"declaration_month",
	"execution_region",
	"status",
	"amount",
	"detail_amount",
	"total_amount",
	"remark",
	"dept_path_ids",
	"dept_path_names",
	"oa_process_code",
	"source_created_at",
	"synced_at",
)


def _corp_id() -> str:
	return _runtime_config_value(
		"budget_corp_id", "budget_oa_corp_id", "dingtalk_oa_corp_id", "dingtalk_corp_id"
	)


def _oa_connection() -> dict:
	"""``shared_db_config()`` 用的是 ``database`` 键，psycopg 需要 ``dbname``。"""
	shared = shared_db_config()
	return {
		"host": shared.get("host") or "localhost",
		"port": int(shared.get("port") or 5432),
		"dbname": shared.get("database") or "dingtalk_oa",
		"user": shared.get("user") or "postgres",
		"password": shared.get("password") or "",
		"connect_timeout": 8,
		"application_name": "budget-oa-sync",
		"options": "-c default_transaction_read_only=on -c statement_timeout=30000",
	}


def _fetch_instances() -> list[dict]:
	return pg_fetch(
		_oa_connection(),
		INSTANCE_SQL,
		[BUDGET_PROCESS_CODE],
		label="oa-budget-sync",
	)


def _json_list(value) -> list[str]:
	if isinstance(value, str):
		try:
			value = json.loads(value)
		except ValueError:
			return []
	return [str(item) for item in (value or [])] if isinstance(value, list) else []


def _department_paths() -> dict[str, dict[str, list[str]]]:
	"""``dept_id -> {"ids": [...], "names": [...]}``；读不到就退回 OA 的部门全名。"""
	corp_id = _corp_id()
	if not corp_id:
		return {}
	rows = pg_fetch(_oa_connection(), DEPARTMENT_SQL, [corp_id], label="oa-budget-sync")
	paths: dict[str, dict[str, list[str]]] = {}
	for row in rows:
		paths[str(row.get("dept_id") or "")] = {
			"ids": _json_list(row.get("path_ids")),
			"names": _json_list(row.get("path_names")),
		}
	return paths


def _presentation(dept_name: str, path_names: list[str] | None) -> tuple[str, str]:
	levels = [item.strip() for item in (path_names or []) if str(item).strip()]
	if not levels:
		return "待确认", dept_name
	company = levels[COMPANY_LEVEL_INDEX] if len(levels) > COMPANY_LEVEL_INDEX else levels[-1]
	terminal = levels[-1]
	return company or "待确认", "" if terminal == company else terminal


def _entry_values(
	item: dict, paths: dict[str, dict[str, list[str]]], synced_at: str
) -> dict:
	entry = dict(item["entry"])
	dept_id = str(entry.get("dept_id") or "")
	path = paths.get(dept_id) or {}
	path_names = path.get("names") or []
	path_ids = path.get("ids") or []
	company, department = _presentation(entry.get("dept_name") or "", path_names)

	entry.update(
		{
			"company": company,
			"department": department,
			# 解析器产出 dept_id，DocType 字段叫 department_id，必须显式映射；
			# 少了这一步台账的 department_id 会整列为空，部门维度筛选与关联全部失效
			"department_id": dept_id or None,
			"dept_path_ids": json.dumps(path_ids, ensure_ascii=False) if path_ids else None,
			"dept_path_names": json.dumps(path_names, ensure_ascii=False) if path_names else None,
			"total_amount": entry.get("amount") or 0,
			"synced_at": synced_at,
		}
	)
	return {field: entry.get(field) for field in ENTRY_FIELDS}


def _entry_digest(values: dict, details: list[dict]) -> str:
	"""单据指纹：主表最终入库值 + 明细，任何一处变化都会变。

	必须基于 ``_entry_values()`` 之后的值而不是原始 OA 行——``company`` / ``department``
	是部门路径推导出来的，部门改名也要能触发更新；``synced_at`` 每轮都变，要排除，
	否则指纹永远不相等，增量等于没做。
	"""
	stable = {key: value for key, value in values.items() if key != "synced_at"}
	payload = json.dumps(
		{"entry": stable, "details": details},
		sort_keys=True,
		default=str,
		ensure_ascii=False,
	)
	return hashlib.sha256(payload.encode("utf-8")).hexdigest()


class LedgerSyncVerificationError(frappe.ValidationError):
	"""同步后的台账与源端对不上（条数 / 金额 / 明细行数）。"""


class LedgerSyncTimeout(frappe.ValidationError):
	"""单次同步超过时间预算，已中断并回滚。"""


# 用 ContextVar 而不是 signal.alarm：同步可能跑在 worker 线程里，信号只对主线程有效，
# 而且 ERPNext 自己的逻辑也会消费 SIGALRM。检查点式超时是唯一跨进程可靠的做法。
_deadline: ContextVar[float | None] = ContextVar("budget_ledger_sync_deadline", default=None)


def _timeout_seconds() -> float:
	"""默认 300s，可用站点配置 ``budget_ledger_sync_timeout`` 覆盖。"""
	try:
		return float(frappe.conf.get("budget_ledger_sync_timeout") or SYNC_TIMEOUT_SECONDS)
	except (TypeError, ValueError):
		return float(SYNC_TIMEOUT_SECONDS)


@contextmanager
def _time_budget():
	limit = _timeout_seconds()
	token = _deadline.set(time.monotonic() + limit)
	try:
		yield limit
	finally:
		_deadline.reset(token)


def _check_deadline() -> None:
	"""在循环里埋检查点：超时就中止，交给外层 rollback 整批撤销。"""
	deadline = _deadline.get()
	if deadline is not None and time.monotonic() >= deadline:
		raise LedgerSyncTimeout(f"同步超过单次时间预算 {_timeout_seconds():.0f}s，已中止")


def _log_start(trigger: str, started_at: str) -> str | None:
	"""先落一条 ``Running`` 并立即提交。

	提交是刻意的：后续写库若失败要 ``rollback()`` 整批撤销，日志行必须先于事务边界
	存在，否则会跟着台账一起被回滚掉，「正在同步」也就永远看不见。
	日志写失败不影响同步本身，最多丢掉这一轮的运行记录。
	"""
	try:
		doc = frappe.get_doc(
			{
				"doctype": SYNC_LOG_DOCTYPE,
				"status": "Running",
				"trigger": trigger,
				"started_at": started_at,
			}
		).insert(ignore_permissions=True)
		frappe.db.commit()
		return doc.name
	except Exception:
		frappe.db.rollback()
		frappe.log_error(
			title="预算台账同步：写运行记录失败", message=frappe.get_traceback()
		)
		return None


LOG_COUNT_FIELDS = (
	"remote_total",
	"deduped",
	"created",
	"updated",
	"unchanged",
	"removed",
	"detail_rows",
	"local_total",
)


def _log_values(
	status: str,
	payload: dict | None,
	*,
	finished_at: str,
	message: str | None = None,
	duration_sec: float | None = None,
	full_sync: bool = False,
) -> dict[str, Any]:
	"""构造运行记录要写的字段（纯函数，便于单测）。

	只写 payload 里真实存在的计数：跳过 / 失败轮次没有 ``updated`` 之类的值，
	写成 0 会让人误以为「跑过且没有更新」。
	"""
	values: dict[str, Any] = {
		"status": status,
		"finished_at": finished_at,
		"full_sync": 1 if full_sync else 0,
	}
	if duration_sec is not None:
		values["duration_sec"] = round(duration_sec, 2)
	if message:
		values["message"] = message[:1000]
	payload = payload or {}
	for field in LOG_COUNT_FIELDS:
		if payload.get(field) is not None:
			values[field] = int(payload[field] or 0)
	if payload.get("local_amount") is not None:
		values["local_amount"] = float(payload["local_amount"])
	return values


def _log_finish(
	name: str | None,
	status: str,
	*,
	payload: dict | None = None,
	message: str | None = None,
	started_mono: float | None = None,
	full_sync: bool = False,
	commit: bool = True,
) -> None:
	"""收尾写结果。成功路径传 ``commit=False``，跟台账写入同一个事务提交。"""
	if not name:
		return
	values = _log_values(
		status,
		payload,
		finished_at=frappe.utils.now(),
		message=message,
		duration_sec=(
			None if started_mono is None else time.monotonic() - started_mono
		),
		full_sync=full_sync,
	)
	try:
		frappe.db.set_value(SYNC_LOG_DOCTYPE, name, values, update_modified=False)
		if commit:
			frappe.db.commit()
	except Exception:
		frappe.log_error(
			title="预算台账同步：更新运行记录失败", message=frappe.get_traceback()
		)


def _prune_sync_logs() -> None:
	"""每轮都会新增一条运行记录，不清理会一直涨；留最近 30 天足够排查。"""
	try:
		frappe.db.sql(
			f"DELETE FROM `tab{SYNC_LOG_DOCTYPE}` WHERE started_at < %s",
			[frappe.utils.add_days(frappe.utils.now(), -SYNC_LOG_RETENTION_DAYS)],
		)
		frappe.db.commit()
	except Exception:
		frappe.db.rollback()


def _cache_client():
	cache = frappe.cache
	return cache() if callable(cache) else cache


def _remember(payload: dict) -> None:
	"""最近一次同步结果写缓存，供 ``get_ledger_sync_status`` 查看。"""
	try:
		_cache_client().set_value(CACHE_KEY, payload, expires_in_sec=STATUS_TTL)
	except Exception:  # 缓存不可用不能影响同步本身
		pass


def _local_snapshot() -> dict:
	rows = frappe.db.sql(
		f"SELECT COUNT(*) AS total, COALESCE(SUM(total_amount), 0) AS amount"
		f" FROM `tab{ENTRY_DOCTYPE}`",
		as_dict=True,
	)
	row = rows[0] if rows else {}
	return {
		"entry_count": int(row.get("total") or 0),
		"total_amount": round(float(row.get("amount") or 0), 2),
		"detail_count": frappe.db.count(DETAIL_DOCTYPE),
	}


def _verify(*, expected_entries: int, expected_amount: float, expected_details: int) -> dict:
	"""提交前自校验：条数 / 金额 / 明细行数必须与源端一致，否则整批回滚。"""
	local = _local_snapshot()
	problems = []
	if local["entry_count"] != expected_entries:
		problems.append(f"台账条数 本地 {local['entry_count']} ≠ 源端 {expected_entries}")
	if local["total_amount"] != round(expected_amount, 2):
		problems.append(
			f"台账金额合计 本地 {local['total_amount']} ≠ 源端 {round(expected_amount, 2)}"
		)
	if local["detail_count"] != expected_details:
		problems.append(f"明细行数 本地 {local['detail_count']} ≠ 源端 {expected_details}")
	if problems:
		raise LedgerSyncVerificationError("；".join(problems))
	return local


def _write_details(form_no: str, details: list[dict], synced_at: str) -> int:
	frappe.db.delete(DETAIL_DOCTYPE, {"form_no": form_no})
	written = 0
	for row in details:
		doc = {key: value for key, value in row.items() if value not in (None, "")}
		doc.update({"doctype": DETAIL_DOCTYPE, "synced_at": synced_at})
		frappe.get_doc(doc).insert(ignore_permissions=True, ignore_mandatory=True)
		written += 1
	return written


def sync_from_oa(*, trigger: str = "Manual", force: bool = False) -> dict:
	"""拉取 OA 归档 → 解析 → 去重 → 增量写本地表 → 自校验 → 提交。

	默认增量：每单算指纹，与本地 ``source_digest`` 相同就整单跳过（不 upsert、不碰明细），
	只有新增 / 改动 / 部门路径变的单据才写库。``force=True`` 忽略指纹整表重建，
	用于每日对账，对冲「本地被手工改坏」这类增量发现不了的问题。

	多 worker / 手动触发重叠时靠 Redis 锁串行化：本任务会「先删明细再重建」，
	并发执行会把明细插成两份。校验不通过或超出时间预算时整批回滚，本地保持上一次
	的完整快照。

	每一轮都落一条 ``Budget Ledger Sync Log``：插入时提交（运行中可见），成功时
	和台账写入同事务提交，失败时先回滚再单独标记，所以日志和台账不会打架。
	"""
	started_at = frappe.utils.now()
	started_mono = time.monotonic()
	with sync_lock(LEDGER_LOCK_NAME) as acquired:
		if not acquired:
			result = {"skipped": True, "reason": "另一个预算台账同步正在运行，本次跳过"}
			log_name = _log_start(trigger, started_at)
			_log_finish(
				log_name,
				"Skipped",
				payload=result,
				message=result["reason"],
				started_mono=started_mono,
				full_sync=force,
			)
			_remember(
				{
					"status": "skipped",
					"started_at": started_at,
					"finished_at": frappe.utils.now(),
					**result,
				}
			)
			return result

		log_name = _log_start(trigger, started_at)
		_prune_sync_logs()
		try:
			with _time_budget():
				result = _sync_from_oa(force=force)
		except Exception as error:
			frappe.db.rollback()
			_log_finish(
				log_name,
				"Failed",
				message=f"{type(error).__name__}: {error}",
				started_mono=started_mono,
				full_sync=force,
			)
			_remember(
				{
					"status": "failed",
					"started_at": started_at,
					"finished_at": frappe.utils.now(),
					"error": f"{type(error).__name__}: {error}",
				}
			)
			frappe.log_error(
				title="预算台账同步失败",
				message=frappe.get_traceback(with_context=True),
			)
			raise

		if result.get("skipped"):
			_log_finish(
				log_name,
				"Skipped",
				payload=result,
				message=result.get("reason"),
				started_mono=started_mono,
				full_sync=force,
				)
			_remember(
				{
					"status": "skipped",
					"started_at": started_at,
					"finished_at": frappe.utils.now(),
					**result,
				}
			)
			return result

		# 台账写入与日志收尾同一事务提交：要么都成功，要么日志停在 Running
		_log_finish(
			log_name,
			"Success",
			payload=result,
			message=(
				"增量校验不一致，已自动升级为整表重建"
				if result.get("escalated")
				else None
			),
			started_mono=started_mono,
			# 自动升级的那一轮实际跑的是全量，对账计时也应按全量算
			full_sync=force or bool(result.get("escalated")),
			commit=False,
		)
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


def _sync_from_oa(*, force: bool = False) -> dict:
	rows = _fetch_instances()
	_check_deadline()
	if not rows:
		return {"skipped": True, "reason": "OA 归档没有符合条件的预算单", "remote_total": 0}

	paths = _department_paths()
	_check_deadline()
	synced_at = frappe.utils.now()

	parsed = []
	for index, row in enumerate(rows):
		if index % 50 == 0:
			_check_deadline()
		parsed.append(parse_budget_instance(row))
	kept = dedupe_latest([item["entry"] for item in parsed])
	kept_forms = {entry["form_no"] for entry in kept}
	selected = [item for item in parsed if item["entry"]["form_no"] in kept_forms]

	created = 0
	updated = 0
	unchanged = 0
	detail_rows = 0
	expected_amount = 0.0
	expected_details = 0
	names: list[str] = []

	for item in selected:
		_check_deadline()
		values = _entry_values(item, paths, synced_at)
		form_no = values["form_no"]
		expected_amount += float(values.get("total_amount") or 0)
		expected_details += len(item["details"])
		digest = _entry_digest(values, item["details"])
		values["source_digest"] = digest

		# 一次查询同时判断「是否已存在」和「指纹是否相同」：不存在时返回 None，
		# 存在但历史数据没指纹时返回 (name, None)，两种情况都要重写
		existing = frappe.db.get_value(
			ENTRY_DOCTYPE, form_no, ["name", "source_digest"], as_dict=True
		)
		if existing and not force and existing.get("source_digest") == digest:
			unchanged += 1
			# 必须收进 names：否则会被后面「删陈旧」当成源端已删除的单据删掉
			names.append(form_no)
			continue

		if existing:
			frappe.db.set_value(ENTRY_DOCTYPE, form_no, values, update_modified=False)
			updated += 1
		else:
			frappe.get_doc({"doctype": ENTRY_DOCTYPE, **values}).insert(
				ignore_permissions=True, ignore_mandatory=True
			)
			created += 1
		detail_rows += _write_details(form_no, item["details"], synced_at)
		names.append(form_no)

	removed = 0
	if names:
		stale = frappe.get_all(ENTRY_DOCTYPE, filters={"form_no": ["not in", names]}, pluck="name")
		for name in stale:
			frappe.db.delete(DETAIL_DOCTYPE, {"form_no": name})
			frappe.delete_doc(ENTRY_DOCTYPE, name, ignore_permissions=True, force=True)
			removed += 1

	try:
		verified = _verify(
			expected_entries=len(names),
			expected_amount=expected_amount,
			expected_details=expected_details,
		)
	except LedgerSyncVerificationError:
		# 增量轮里没碰过的单据，本地却和源端对不上（被手改过 / 历史 bug 留下的脏数据）。
		# 单纯回滚修不好，下一轮还是同样的结果，会一直失败到每日全量。所以这里直接升级
		# 成整表重建重跑一次；全量仍对不上才是真的失败，照常抛给外层回滚。
		if force or not unchanged:
			raise
		result = _sync_from_oa(force=True)
		result["escalated"] = True
		return result

	return {
		"remote_total": len(rows),
		"deduped": len(kept),
		"created": created,
		"updated": updated,
		"unchanged": unchanged,
		"removed": removed,
		"detail_rows": detail_rows,
		"local_total": verified["entry_count"],
		"local_amount": verified["total_amount"],
		"verified": True,
	}


@frappe.whitelist()
def run_oa_budget_sync(force: bool = False) -> dict:
	"""手动触发：``bench --site <site> execute budget.services.oa_budget_sync.run_oa_budget_sync``。

	``force=True`` 忽略指纹整表重建，用于怀疑本地数据不一致时的强制校准。
	"""
	frappe.only_for(("System Manager",))
	return sync_from_oa(trigger="Manual", force=bool(force))


def _force_full_due() -> bool:
	"""距离上一次整表对账是否已满 24h（首次运行为 True）。"""
	last_full = frappe.db.get_value(
		SYNC_LOG_DOCTYPE,
		{"status": "Success", "full_sync": 1},
		"started_at",
		order_by="started_at desc",
	)
	if not last_full:
		return True
	age = frappe.utils.now_datetime() - frappe.utils.get_datetime(last_full)
	return age.total_seconds() >= FORCE_FULL_INTERVAL_HOURS * 3600


def scheduled_sync() -> dict:
	"""外部调度器入口（cron / launchd / ofelia）：不做用户权限校验。

	与手动入口的区别：``trigger`` 记为 ``Scheduled``；失败照常向上抛，让调用进程
	拿到非零退出码，调度器那边能直接看到这一轮失败；并自动承担每日一次整表对账
	（平时增量，距上次全量满 24h 的那一轮改为全量），不需要额外配一条 cron。
	"""
	return sync_from_oa(trigger="Scheduled", force=_force_full_due())


def _enqueue_sync() -> bool:
	"""入队一次后台同步，返回是否真的排上了（被去重挡掉就是 False）。"""
	timeout = int(_timeout_seconds()) + 120
	job = frappe.enqueue(
		"budget.services.oa_budget_sync.sync_from_oa",
		queue="long",
		timeout=timeout,
		job_id=LEDGER_JOB_ID,
		deduplicate=True,
		trigger="Background",
	)
	return bool(job)


@frappe.whitelist()
def enqueue_oa_budget_sync() -> dict:
	"""异步触发：``job_id`` + ``deduplicate`` 保证同一时刻只有一个任务在队列里。

	页面按钮和高频 cron 都可以调它，重复调用不会堆出多个排队任务；
	真正写库那一步仍由 Redis 锁兜底。
	"""
	frappe.only_for(("System Manager",))
	return {"queued": _enqueue_sync(), "job_id": LEDGER_JOB_ID}


# 列表页认为「够新」的阈值：超过就在后台补一轮，用户不必等下一次定时任务
FRESHNESS_SECONDS = 120
# 上一轮（不论成败）启动后多久内不再因打开页面而重试。没有这个退避，OA 库长时间故障时
# 每个用户每次打开列表都会排一轮注定失败（连接超时 8s）的同步
FRESHNESS_RETRY_SECONDS = 60


def _seconds_since(value) -> float:
	return (frappe.utils.now_datetime() - frappe.utils.get_datetime(value)).total_seconds()


@frappe.whitelist()
def ensure_ledger_fresh(max_age_seconds: int | None = None) -> dict:
	"""列表页打开时调用：本地数据太旧就入队一次同步（读权限即可，不要求管理员）。

	先渲染本地表、后台补齐，这样「随时能看到」不依赖定时任务的频率：
	多人同时打开也只会排一个任务（``job_id`` 去重），定时任务只作兜底。
	``queued=False`` 且 ``blocked_by == "recent_attempt"`` 表示上一轮刚试过、这是退避。
	"""
	frappe.only_for(READ_ROLES)

	# 不能用 ``or``：传 0 是合法用法（强制认为过期），会被 falsy 吃掉
	threshold = FRESHNESS_SECONDS if max_age_seconds is None else int(max_age_seconds)
	last_finished = frappe.db.get_value(
		SYNC_LOG_DOCTYPE,
		{"status": "Success"},
		"finished_at",
		order_by="started_at desc",
	)
	age_seconds: float | None = None
	if last_finished:
		age_seconds = _seconds_since(last_finished)

	stale = age_seconds is None or age_seconds > threshold
	queued = False
	blocked_by = None
	if stale:
		# 用「最近一次尝试」而不是「最近一次成功」做退避：失败轮也算试过了，
		# 否则失败后每个打开页面的人都会再触发一轮
		last_attempt = frappe.db.get_value(
			SYNC_LOG_DOCTYPE, {}, "started_at", order_by="started_at desc"
		)
		if last_attempt and _seconds_since(last_attempt) < FRESHNESS_RETRY_SECONDS:
			blocked_by = "recent_attempt"
		else:
			queued = _enqueue_sync()

	return {
		"stale": stale,
		"queued": queued,
		"blocked_by": blocked_by,
		"age_seconds": None if age_seconds is None else round(age_seconds, 1),
		"threshold": threshold,
		"job_id": LEDGER_JOB_ID if stale else None,
	}


@frappe.whitelist()
def get_ledger_sync_status() -> dict:
	"""最近一次同步结果 + 本地实际规模，用于确认同步是否在正常跑。

	``last`` 优先取运行记录（重启不丢），Redis 缓存只作为历史数据的兜底。
	"""
	frappe.only_for(("System Manager",))
	recent = frappe.get_all(
		SYNC_LOG_DOCTYPE,
		fields=[
			"name",
			"status",
			"trigger",
			"started_at",
			"finished_at",
			"duration_sec",
			"message",
			"remote_total",
			"deduped",
			"created",
			"updated",
			"unchanged",
			"full_sync",
			"removed",
			"detail_rows",
			"local_total",
			"local_amount",
		],
		order_by="started_at desc",
		limit=5,
	)
	if recent:
		last = recent[0]
	else:
		try:
			last = _cache_client().get_value(CACHE_KEY) or {}
		except Exception:
			last = {}
	return {
		"last": last,
		"recent": recent,
		"local": _local_snapshot(),
		"lock_held": is_locked(LEDGER_LOCK_NAME),
		"timeout_seconds": _timeout_seconds(),
	}
