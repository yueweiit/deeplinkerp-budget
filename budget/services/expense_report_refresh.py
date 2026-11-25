"""Build private reporting lines in the background, never in a page request."""
import json
import logging
import time
from decimal import Decimal
from uuid import uuid4

import frappe
from frappe import _

from . import expense_report_store as store

VERSION = "expense-report-v3"
JOB = "expense-report-refresh"
TIMEOUT = 300


def live_revision():
	from ..integrations import cn_approval_source as source, cn_approval_store as cn_store
	from . import cn_approval_sync as cn, cn_approval_settings as settings
	from .expense_sync import cn_revision
	from deeplinkerp_branding.services import mexico_expenses as mx, regional_expense_automatic as automatic
	url, templates = source.configuration()
	# CN templates alone do not cover MX-only or retired corporations. A
	# publication is usable only under the complete current OA source scope.
	_url, corps = source.client().configuration()
	key = cn.identity(url, templates)
	record = cn_store.read(key)
	status = cn.status(record, key=key)
	mx_settings = mx._settings()
	mx_revision = mx.sync_revision(mx_settings)
	# Manual attribution changes must invalidate old company assignments, even
	# while an otherwise reusable reporting publication is retained on failure.
	manual = frappe.db.sql("select max(reviewed_at) from `tabRegional Expense Allocation`")[0][0]
	legacy = frappe.db.sql("select max(reviewed_at) from `tabMexico Expense Line`")[0][0]
	policy = store.digest([VERSION, key, sorted(set(corps)), settings.version(), mx_settings.config_version,
		automatic.configuration(mx_settings)["version"], str(manual or ""), str(legacy or "")])
	line_version = frappe.db.sql("select max(modified), count(*) from `tabMexico Expense Line`")[0]
	source_version = frappe.db.sql("select max(modified), count(*) from `tabMexico Expense Source`")[0]
	versions = {"cn": cn_revision(status), "mx": mx_revision}
	data = store.digest([policy, versions, line_version, source_version])
	selected = mx._templates(mx_settings)
	mx_complete = bool(selected) and all(item.get("enabled") and item.get("last_sync_at")
		and not any(item.get(field) for field in ("last_error", "cursor", "sync_until")) for item in selected)
	return {"policy": policy, "data": data, "versions": versions,
		"complete": bool(status.get("has_snapshot") and status["status"] not in ("queued", "running", "failed") and mx_complete),
		"rebuilding": bool(status.get("allocation_rebuilding"))}


def needs_refresh(state, live):
	if not state or not state.get("publication") or state.get("data_revision") != live["data"]:
		return True
	return bool((state.get("fx_missing") or rates_due(state)) and (
		not state.get("built_at") or (frappe.utils.now_datetime() - frappe.utils.get_datetime(state["built_at"])).total_seconds() >= 300))


def rates_due(state):
	return not state.get("rates_checked_at") or (
		frappe.utils.now_datetime() - frappe.utils.get_datetime(state["rates_checked_at"])).total_seconds() >= 86400


def available():
	return "deeplinkerp_branding" in frappe.get_installed_apps() and store.installed()


def enqueue():
	# A short retry throttle also covers a failed queue and a failed worker.
	# This cache is only coordination; published data remains durable in SQL.
	claimed = False
	try:
		if not available():
			return
		cache = frappe.cache()
		key = cache.make_key("expense-report-request")
		if not cache.set(key, "1", nx=True, ex=60):
			return
		claimed = True
		frappe.enqueue("budget.services.expense_report_refresh.refresh", queue="long", timeout=TIMEOUT,
			job_id=JOB, deduplicate=True)
	except Exception as error:
		if claimed:
			try:
				cache.delete(key)
			except Exception:
				# The throttle expires by itself if Redis also rejects cleanup.
				pass
		# Scheduling is best effort. A queue outage must never turn a committed
		# source synchronization into a reported failure or discard its result.
		logging.getLogger(__name__).error("Expense reporting queue unavailable (%s)", type(error).__name__)
		return False
	return True


def after_source_change(doc=None, method=None):
	"""Coalesce source changes; queue only after their transaction commits."""
	if not available() or frappe.flags.get("expense_report_enqueue_registered"):
		return
	frappe.flags.expense_report_enqueue_registered = True
	def committed():
		frappe.flags.expense_report_enqueue_registered = False
		enqueue()
	def rolled_back():
		frappe.flags.expense_report_enqueue_registered = False
	frappe.db.after_commit.add(committed)
	frappe.db.after_rollback.add(rolled_back)


def scheduled_refresh():
	if not available():
		return
	live = live_revision()
	if not live["rebuilding"] and needs_refresh(store.state(), live):
		enqueue()


def rate_loader(saved, *, renew=False):
	"""Persist verified exact-day legs; only the worker can fetch missing rates.

	A bounded number of external lookups prevents an old archive or an outage
	from monopolizing the worker. Missing legs remain explicit and retry later.
	"""
	from deeplinkerp_branding.services import mexico_expense_fx as fx
	rates = dict(saved)
	# Unresolved legs with fewer attempts get the next round's request budget.
	# Keep this private progress with the publication, independently of Redis
	# expiry. Rebuild the pending set from actual calls to prune obsolete legs.
	attempts = dict(rates.pop("_pending", {}))
	round_limit = min(attempts.values(), default=0)
	pending = {}
	deadline, misses = time.monotonic() + 45, 0
	def load(currency, day):
		nonlocal misses
		if currency == "CNY":
			return Decimal(1)
		key = currency + ":" + day
		previous = fx.positive_rate(rates.get(key))
		if previous is not None and not renew:
			return Decimal(rates[key])
		cached = frappe.cache.get_value(f"mx-month-fx:v2:{day}:{currency}")
		if fx.positive_rate(cached) is not None:
			rates[key] = str(cached)
			return Decimal(str(cached))
		retry_key = "expense-report-fx-retry:" + key
		attempted = False
		try:
			if (misses >= 6 or time.monotonic() >= deadline or attempts.get(key, 0) > round_limit
				or frappe.cache.get_value(retry_key)):
				raise fx.RateUnavailable()
			misses += 1
			attempts[key] = attempts.get(key, 0) + 1
			attempted = True
			value = fx.load_month_rate(currency, day, quiet=True)
		except fx.RateUnavailable:
			load.pending = True
			pending[key] = attempts.get(key, 0)
			rates["_pending"] = pending
			if attempted:
				frappe.cache.set_value(retry_key, 1, expires_in_sec=300)
			if previous is not None:
				return previous
			raise
		rates[key] = str(value)
		pending.pop(key, None)
		return value
	load.pending = False
	return load, rates


def refresh():
	"""Own the transaction; preserve the last publication on any failure."""
	# Also guard already queued jobs after the optional integration is removed.
	if not available():
		return
	from redis.exceptions import LockError
	from . import expense_management as service
	user = frappe.session.user
	try:
		with frappe.cache.lock("expense-report-publish:" + frappe.local.site, timeout=TIMEOUT + 30, blocking_timeout=0):
			# This is an internal job, not whitelisted. It creates a complete site
			# projection; current-user permissions are mandatory at every read.
			frappe.set_user("Administrator")
			frappe.db.rollback()
			before = live_revision()
			previous = store.state()
			if before["rebuilding"] or not needs_refresh(previous, before):
				return {"changed": False}
			rows, _context, rules, metadata = service.load()
			loader, rates = rate_loader(json.loads((previous or {}).get("rates_json") or "{}"), renew=rates_due(previous or {}))
			rows = service.converter(loader)(rows)
			# Release the source snapshot and check a fresh committed revision.
			# No report rows have been written yet, and no source locks are held
			# while waiting for external exchange rates.
			frappe.db.rollback()
			frappe.flags.regional_automatic_configuration = None
			after = live_revision()
			if after["data"] != before["data"] or after["rebuilding"]:
				return {"changed": False, "superseded": True}
			result = store.publish(rows, {"publication": uuid4().hex, "data_revision": before["data"],
				"policy_revision": before["policy"], "metadata_json": store.encode(metadata),
				"rules_json": store.encode(rules), "rates_json": store.encode(rates),
				"rates_checked_at": (previous or {}).get("rates_checked_at") if loader.pending else frappe.utils.now_datetime(),
				"built_at": frappe.utils.now_datetime(), "fx_missing": int(any("exchange_rate" in row["issues"] for row in rows))})
			frappe.db.commit()
			return result
	except LockError:
		return {"changed": False, "busy": True}
	except Exception:
		frappe.db.rollback()
		# RQ retains the traceback. Never log source payloads or connection values.
		raise
	finally:
		frappe.set_user(user)


def read_context():
	from . import cn_approval_workbench as approvals
	approvals.require_reader()
	context = {"companies": approvals.allowed_companies(),
		"manager": frappe.session.user == "Administrator" or "System Manager" in frappe.get_roles()}
	live = live_revision()
	state = store.state(lock="read")
	if needs_refresh(state, live):
		enqueue()
	if not state or not state.get("publication") or state.get("policy_revision") != live["policy"] or live["rebuilding"]:
		frappe.throw(_("Expense reporting data is being prepared. Please retry shortly."))
	metadata = json.loads(state["metadata_json"])
	# Historical MX exceptions retain their original DocType, field and
	# company/mapping permissions. Only names are read here, never raw JSON.
	legacy_sources = frappe.db.sql("select distinct source_key from `tabExpense Report Line` where is_legacy=1")
	context["legacy_keys"] = frappe.get_list("Mexico Expense Line",
		filters={"is_active": 1, "source": ["in", [row[0] for row in legacy_sources]]},
		pluck="name", limit_page_length=store.MAX_LINES + 1) if legacy_sources else []
	visible, params = store.where(context, {"company": "", "category": "", "source_category": ""})
	metadata["has_data"] = bool(metadata.get("has_snapshot") or store.count(visible, params))
	pending = state["data_revision"] != live["data"]
	metadata.update(reporting_pending=pending, reporting_publication=state["publication"],
		reporting_updated_at=str(state["built_at"]),
		complete=bool(metadata["complete"] and live["complete"] and not pending))
	# A changed publication must refresh the screen even when input versions
	# are unchanged (e.g. a previously missing exchange rate was obtained).
	metadata["sync_revision"]["cn"] = [*metadata["sync_revision"]["cn"], state["publication"]]
	return context, json.loads(state["rules_json"]), metadata
