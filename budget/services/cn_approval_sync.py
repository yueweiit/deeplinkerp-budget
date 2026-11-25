"""Durable CN snapshots, refreshed outside the page request."""
import json
from functools import partial
from traceback import walk_tb
from uuid import uuid4

import frappe
from frappe import _
from frappe.utils import get_datetime, now_datetime
from rq.timeouts import JobTimeoutException

from ..integrations import cn_approval_source as source
from ..integrations import cn_approval_store as store
from . import cn_approval_settings as settings

SYNC_INTERVAL = 300
QUEUE_LEASE = 900
# Leave time after the scan for ownership projection and atomic publication.
JOB_TIMEOUT = source.SCAN_TIMEOUT + 300
# Publication holds locks and must not commit a heartbeat halfway through.
# A valid worker must never lose ownership before its RQ time budget ends.
# Cap the lease from started_at too: later heartbeats cannot extend a dead job.
RUN_LEASE = JOB_TIMEOUT + 60
ACTIVE = {"queued", "running"}


def identity(url, templates):
	return source.cache_key(url, templates).rsplit(":", 1)[-1]


def enabled():
	return settings.enabled()


def age(value, now):
	return (now - get_datetime(value)).total_seconds() if value else float("inf")


def status(record=None, *, key=None):
	record = record or {}
	now = now_datetime()
	state = record.get("sync_status") or "idle"
	if state == "queued" and age(record.get("queued_at"), now) >= QUEUE_LEASE:
		state = "failed"
	elif state == "running" and (
		age(record.get("heartbeat_at") or record.get("started_at"), now) >= RUN_LEASE
		or (record.get("started_at") and age(record["started_at"], now) >= RUN_LEASE)
	):
		state = "failed"
	stale = not record.get("snapshot_id") or age(record.get("last_success_at"), now) >= SYNC_INTERVAL
	active = state in ACTIVE
	options = settings.sync_options()
	from deeplinkerp_branding.services.regional_expenses import revision
	from deeplinkerp_branding.services.regional_expense_automatic import ensure_rebuild
	return {"source_id": key or record.get("name"), "status": state, "active": active, **options,
		"allocation_rebuilding": ensure_rebuild(),
		"allocation_revision": revision(),
		"snapshot_id": record.get("snapshot_id"), "has_snapshot": bool(record.get("snapshot_id")),
		"last_success_at": str(record["last_success_at"]) if record.get("last_success_at") else None,
		"completed_templates": record.get("completed_templates") or 0,
		"template_count": record.get("template_count") or 0, "page_count": record.get("page_count") or 0,
		"error_code": "sync_timeout" if state == "failed" and record.get("sync_status") in ACTIVE else record.get("error_code"),
		"stale": stale, "auto_sync_due": bool(options["enabled"] and stale and not active
			and age(record.get("queued_at"), now) >= SYNC_INTERVAL)}


def get_status():
	url, templates = source.configuration()
	key = identity(url, templates)
	return {**status(store.read(key), key=key), "configured": bool(templates)}


def read_snapshot():
	url, templates = source.configuration()
	key = identity(url, templates)
	record = store.read(key, payload=True)
	return {**store.snapshot(record), "sync": status(record, key=key)}


def request_sync(*, if_stale=False):
	"""Dedicated command transaction; callers must not mix in business writes."""
	for attempt in range(3):
		try:
			return claim_sync(if_stale=if_stale)
		except frappe.QueryDeadlockError:
			# MariaDB snapshot isolation can reject even a locking read after
			# another request creates/updates the row. Retry with a new database
			# snapshot; a savepoint cannot recover an aborted transaction.
			frappe.db.rollback()
			if attempt == 2:
				raise


def claim_sync(*, if_stale=False):
	if not enabled():
		frappe.throw(_("CN approval background synchronization is disabled."))
	url, templates = source.configuration()
	key = identity(url, templates)
	record = store.ensure(key)
	current = status(record, key=key)
	if current["active"] or (if_stale and not current["auto_sync_due"]):
		return current
	token = uuid4().hex
	values = {"sync_status": "queued", "sync_token": token, "queued_at": now_datetime(),
		"started_at": None, "heartbeat_at": None, "finished_at": None, "completed_templates": 0,
		"template_count": len(templates), "page_count": 0, "error_code": None}
	store.update(key, values)
	# Publish the claim before starting a worker. Catch an actual enqueue failure
	# in the callback too, instead of leaving a permanent queued status.
	frappe.db.after_commit.add(partial(enqueue_committed, key, token))
	return status({**record, **values}, key=key)


def enqueue_committed(key, token):
	try:
		frappe.enqueue("budget.services.cn_approval_sync.run_sync", queue="long", timeout=JOB_TIMEOUT,
			job_id=f"cn-approval-sync-{token}", key=key, token=token)
	except Exception as error:
		log_failure(key, "enqueue", "queue_failed", error)
		frappe.db.rollback()
		fail(key, token, "queue_failed")


def failure_code(error, stage):
	if isinstance(error, source.ApprovalScanTimeout):
		return "scan_timeout"
	if isinstance(error, source.ApprovalScanLimitExceeded):
		return "scan_limit"
	if isinstance(error, JobTimeoutException):
		return "job_timeout"
	return {"configuration": "configuration_failed", "scan": "scan_failed", "allocation": "allocation_failed",
		"publish": "publish_failed"}.get(stage, "source_failed")


def log_failure(key, stage, code, error):
	"""Keep actionable diagnostics even if a dead connection prevents rollback.

	Never log exception messages, SQL, HTTP bodies, local variables or tokens.
	"""
	try:
		frames = [{"file": frame.f_code.co_filename.rsplit("/", 1)[-1],
			"function": frame.f_code.co_name, "line": line} for frame, line in walk_tb(error.__traceback__)][-8:]
		detail = {"source_id": key, "stage": stage, "error_code": code,
			"exception_type": type(error).__name__, "frames": frames}
		if error.args and type(error.args[0]) is int:
			detail["numeric_code"] = error.args[0]
		frappe.logger("cn_approval_sync").error("CN approval failure detail: %s", json.dumps(detail))
	except Exception:
		# Diagnostics must never replace the original failure.
		pass


def fail(key, token, code):
	record = store.read(key, lock=True)
	if record and record.get("sync_token") == token and record.get("sync_status") in ACTIVE:
		store.update(key, {"sync_status": "failed", "finished_at": now_datetime(), "error_code": code})
	frappe.db.commit()
	# Do not include upstream responses, configuration or approval contents.
	frappe.logger("cn_approval_sync").error("CN approval synchronization failed: %s [%s]", code, key)


class SupersededSync(Exception):
	pass


def owned(key, token):
	record = store.read(key, lock=True)
	if not record or record.get("sync_token") != token or not status(record)["active"]:
		raise SupersededSync
	return record


def progress(key, token, *, completed_templates, page_count):
	owned(key, token)
	store.update(key, {"heartbeat_at": now_datetime(), "completed_templates": completed_templates,
		"page_count": page_count})
	frappe.db.commit()


def run_sync(key, token):
	stage = "configuration"
	try:
		record = owned(key, token)
		# Duplicate queue delivery must not start another scan with the same token.
		if record["sync_status"] != "queued":
			frappe.db.rollback()
			return
		url, templates = source.configuration()
		if not enabled() or identity(url, templates) != key:
			fail(key, token, "configuration_changed")
			return
		store.update(key, {"sync_status": "running", "started_at": now_datetime(), "heartbeat_at": now_datetime()})
		frappe.db.commit()
		stage = "scan"
		previous = store.snapshot(store.read(key, payload=True))
		payload = source.load_snapshot(url, templates, previous=previous, progress=partial(progress, key, token))
		refresh = payload.pop("_refresh", None)
		stage = "publish"
		# Compress/validate before acquiring business locks or changing ownership.
		encoded = store.prepare_snapshot(payload)
		for attempt in range(3):
			try:
				# Recheck configuration and token after every transaction restart.
				stage = "configuration"
				current_url, current_templates = source.configuration()
				if not enabled() or identity(current_url, current_templates) != key:
					fail(key, token, "configuration_changed")
					return
				owned(key, token)
				stage = "allocation"
				from deeplinkerp_branding.services.regional_expenses import refresh_cn
				if refresh is None:
					refresh_cn(payload["rows"], templates)
				else:
					if refresh["full_templates"]:
						full = {(t["corp_id"], t["process_code"]) for t in refresh["full_templates"]}
						refresh_cn([r for r in payload["rows"] if (r["corp_id"], r["process_code"]) in full], refresh["full_templates"])
					if refresh["changed_rows"]:
						refresh_cn(refresh["changed_rows"], templates, incremental=True)
				stage = "publish"
				store.update(key, {"snapshot_json": encoded, "snapshot_id": uuid4().hex,
					"last_success_at": payload["fetched_at"], "sync_status": "success", "finished_at": now_datetime(),
					"heartbeat_at": now_datetime(), "completed_templates": len(templates), "error_code": None})
				# Ownership, complete snapshot and watermarks publish atomically.
				from .expense_report_refresh import after_source_change
				after_source_change()
				frappe.db.commit()
				break
			except frappe.QueryDeadlockError:
				frappe.db.rollback()
				frappe.flags.regional_automatic_configuration = None
				if attempt == 2:
					raise
				# Reuse the validated in-memory scan; never read OA again here.
	except SupersededSync:
		frappe.db.rollback()
	except Exception as error:
		code = failure_code(error, stage)
		log_failure(key, stage, code, error)
		try:
			frappe.db.rollback()
			fail(key, token, code)
		except Exception as cleanup_error:
			log_failure(key, "failure_recording", code, cleanup_error)
			# Do not bypass connection protection or continue an invalid transaction.
			raise error from cleanup_error


def scheduled_sync():
	# Budget can be installed without the optional CN/MX integration App.
	if not enabled() or "deeplinkerp_branding" not in frappe.get_installed_apps() or not frappe.db.table_exists(store.DOCTYPE):
		return
	try:
		source.configuration()
	except (frappe.ValidationError, ImportError):
		return
	# Cron already provides the five-minute cadence. Measuring freshness from
	# scan completion as well would skip the next tick after a nonzero-duration
	# scan. claim_sync still reuses queued/running jobs under its database lock.
	request_sync()
