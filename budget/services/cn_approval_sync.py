"""Durable CN snapshots, refreshed outside the page request."""
import json
from functools import partial
from uuid import uuid4

import frappe
from frappe import _
from frappe.utils import get_datetime, now_datetime

from ..integrations import cn_approval_source as source
from ..integrations import cn_approval_store as store
from . import cn_approval_settings as settings

SYNC_INTERVAL = 900
QUEUE_LEASE = 900
RUN_LEASE = 300
JOB_TIMEOUT = 180
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
	elif state == "running" and age(record.get("heartbeat_at") or record.get("started_at"), now) >= RUN_LEASE:
		state = "failed"
	stale = not record.get("snapshot_id") or age(record.get("last_success_at"), now) >= SYNC_INTERVAL
	active = state in ACTIVE
	options = settings.sync_options()
	return {"source_id": key or record.get("name"), "status": state, "active": active, **options,
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
	return status(store.read(key), key=key)


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
	except Exception:
		frappe.db.rollback()
		fail(key, token, "queue_failed")


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
		payload = source.load_snapshot(url, templates, progress=partial(progress, key, token))
		# Source settings can change while HTTP pages are being read.
		current_url, current_templates = source.configuration()
		if not enabled() or identity(current_url, current_templates) != key:
			fail(key, token, "configuration_changed")
			return
		owned(key, token)
		store.update(key, {"snapshot_json": json.dumps(payload, ensure_ascii=False), "snapshot_id": uuid4().hex,
			"last_success_at": payload["fetched_at"], "sync_status": "success", "finished_at": now_datetime(),
			"heartbeat_at": now_datetime(), "completed_templates": len(templates), "error_code": None})
		# One atomic publication, after every template has completed successfully.
		frappe.db.commit()
	except SupersededSync:
		frappe.db.rollback()
	except Exception:
		frappe.db.rollback()
		fail(key, token, "source_failed")


def scheduled_sync():
	# Budget can be installed without the optional CN/MX integration App.
	if not enabled() or "deeplinkerp_branding" not in frappe.get_installed_apps() or not frappe.db.table_exists(store.DOCTYPE):
		return
	try:
		source.configuration()
	except (frappe.ValidationError, ImportError):
		return
	request_sync(if_stale=True)
