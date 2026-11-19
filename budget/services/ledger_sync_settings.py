"""Budget ledger configuration and readiness; never discovers or imports remote data on save."""

from __future__ import annotations

import hashlib
import json
import re
from contextlib import contextmanager
from contextvars import ContextVar

import frappe
from frappe import _

from ..integrations.oa_approval_source import (
	_SHARED_DB_KEYS,
	OAApprovalSource,
	OAApprovalSourceConfig,
	_runtime_config_value,
	shared_db_config,
)
from .sync_lock import is_locked, sync_lock

SETTINGS = "Budget Settings"
LOCK_NAME = "budget-ledger-sync"
CORP_KEYS = ("budget_corp_id", "budget_oa_corp_id", "dingtalk_oa_corp_id", "dingtalk_corp_id")
_active_source = ContextVar("budget_ledger_source", default=None)
_saving = ContextVar("budget_ledger_settings_save", default=False)


def resolve_source(doc) -> dict:
	profile = doc.get("ledger_connection_profile") or "budget"
	from ..integrations import shared_operating_oa
	# Unconfigured CN installations reuse the existing CN/MX source, without
	# enabling synchronization or changing an already bound ledger automatically.
	if (profile == "budget" and not doc.get("ledger_sync_enabled")
		and not doc.get("ledger_corp_id") and not doc.get("ledger_source_fingerprint")
		and not doc.get("organization_mappings")
		and not _runtime_config_value(*CORP_KEYS) and not _runtime_config_value(*_SHARED_DB_KEYS["host"])
		and shared_operating_oa.available() and not frappe.db.count("Budget Ledger Entry")):
		profile = "operating_oa"
	if profile == "operating_oa":
		url, corp, valid_corp = shared_operating_oa.configuration(doc.get("ledger_corp_id"))
		connection = {"url": url}
		identity = {"url": url, "corp_id": corp, "region": "CN"}
		configured = valid_corp
		return _source_values(doc, profile, connection, corp, configured, valid_corp, identity)
	if profile == "shared_oa":
		connection = {key: _runtime_config_value(f"overseas_costing_oa_db_{suffix}") for key, suffix in (
			("host", "host"), ("database", "name"), ("port", "port"), ("user", "user"),
			("password", "password"), ("sslmode", "sslmode"),
		)}
		connection["port"] = connection["port"] or "5432"
		connection["database"] = connection["database"] or "dingtalk_oa"
		configured = all(connection.get(key) for key in ("host", "user", "password"))
	else:
		connection = shared_db_config()
		configured = profile == "budget" and all(
			_runtime_config_value(*_SHARED_DB_KEYS[key]) for key in ("host", "user", "password")
		)
	corp = str(doc.get("ledger_corp_id") or _runtime_config_value(*CORP_KEYS)).strip()
	valid_corp = bool(re.fullmatch(r"[A-Za-z0-9_-]{1,200}", corp))
	try:
		port = int(connection["port"])
		configured = configured and 1 <= port <= 65535
	except (TypeError, ValueError):
		configured = False
	identity = {key: connection.get(key) for key in ("host", "port", "database")}
	identity["corp_id"] = corp
	return _source_values(doc, profile, connection, corp, configured, valid_corp, identity)


def _source_values(doc, profile, connection, corp, configured, valid_corp, identity):
	from .organization_mapping import values
	mappings = values(doc.get("organization_mappings"))
	fingerprint = hashlib.sha256(json.dumps(identity, sort_keys=True).encode()).hexdigest()
	version = hashlib.sha256(json.dumps({
		"identity": fingerprint, "profile": profile, "modified": str(doc.get("modified")),
		"enabled": bool(doc.get("ledger_sync_enabled")),
		"organization_mappings": mappings,
	}, sort_keys=True).encode()).hexdigest()
	return {"corp_id": corp, "profile": profile, "connection": connection,
		"connection_configured": bool(configured), "scope_configured": valid_corp,
		"configured": bool(configured and valid_corp), "enabled": bool(doc.get("ledger_sync_enabled")),
		"fingerprint": fingerprint, "version": version, "organization_mappings": mappings,
		"bound_fingerprint": doc.get("ledger_source_fingerprint") or ""}


def current_source() -> dict:
	if _active_source.get() is not None:
		return _active_source.get()
	from .organization_mapping import schema_state
	return resolve_source(frappe.get_doc(SETTINGS)) | {"organization_review_ready": schema_state()["ready"]}


@contextmanager
def source_context(source):
	token = _active_source.set(source)
	try:
		yield
	finally:
		_active_source.reset(token)


def blocked_reason(source) -> str | None:
	if not source["configured"]:
		return "not_configured"
	if not source["enabled"]:
		return "disabled"
	if source.get("organization_review_ready") is False:
		return "schema_update_required"
	if source["bound_fingerprint"] and source["bound_fingerprint"] != source["fingerprint"]:
		return "source_changed"
	return None


def public_settings(source):
	from ..integrations.shared_operating_oa import available
	from .organization_mapping import schema_state
	# Saved documents are resolved without runtime schema state. Match the read
	# response instead of treating an absent readiness flag as a migrated site.
	if "organization_review_ready" not in source:
		source = source | {"organization_review_ready": schema_state()["ready"]}
	return {key: source[key] for key in ("corp_id", "profile", "connection_configured",
		"scope_configured", "configured", "enabled", "version")} | {
		"blocked_by": blocked_reason(source), "shared_oa_available": available(),
		"organization_review_ready": source["organization_review_ready"]}


def connection_kwargs(source=None):
	source = source or current_source()
	if not source["configured"]:
		frappe.throw(_("Budget synchronization is not configured. Open Sync Settings."))
	if source["profile"] == "operating_oa":
		raise LedgerReadSourceError(_("The shared OA source uses the approval API, not a separate budget database."))
	values = source["connection"]
	kwargs = {"host": values["host"], "port": int(values["port"]), "dbname": values["database"],
		"user": values["user"], "password": values["password"], "connect_timeout": 8,
		"application_name": "budget-oa-sync", "options": "-c default_transaction_read_only=on -c statement_timeout=30000"}
	if values.get("sslmode"):
		kwargs["sslmode"] = values["sslmode"]
	return kwargs


class LedgerReadSourceError(RuntimeError):
	"""A remote read cannot safely be combined with this local ledger."""


def checked_read_source():
	"""Stopping synchronization does not disable safe reads of its existing source."""
	source = current_source()
	if not source["configured"]:
		raise LedgerReadSourceError(_("Budget synchronization is not configured. Open Sync Settings."))
	if source["bound_fingerprint"] and source["bound_fingerprint"] != source["fingerprint"]:
		raise LedgerReadSourceError(_("The configured source differs from the ledger source. Ask an administrator to review it."))
	return source


def get_ledger_oa_source():
	"""Organization and approval reads share the exact source used by synchronization."""
	source = checked_read_source()
	if source["profile"] == "operating_oa":
		raise LedgerReadSourceError(_("Organization and approval metrics are not available from the shared OA API."))
	from ..integrations.pg import fetch

	class LedgerOAReadSource(OAApprovalSource):
		def _fetch(self, sql, params=()):
			return fetch(connection_kwargs(source), sql, params, label="budget-oa-read")

	return LedgerOAReadSource(OAApprovalSourceConfig(corp_id=source["corp_id"]))


def validate_expense_source():
	"""Legacy expense aggregates have a separate database, without per-row corporation scope.

	Only combine them with a ledger when the legacy server and explicit corporation binding
	still match. Do not point aggregate-table queries at a different OA database by guessing.
	"""
	source = checked_read_source()
	if source["profile"] == "operating_oa":
		raise LedgerReadSourceError(_("Legacy expense aggregates are not linked to the shared OA source."))
	legacy = shared_db_config()
	connection = source["connection"]
	matching_server = all(str(legacy.get(key) or "").strip() == str(connection.get(key) or "").strip()
		for key in ("host", "port"))
	if not matching_server or _runtime_config_value(*CORP_KEYS) != source["corp_id"]:
		raise LedgerReadSourceError(_("The expense source does not match the budget source. Expense amounts and balances are unavailable until the source binding is reviewed."))


def acquire_settings_transaction_lock():
	"""Take the worker mutex before native Document saves acquire database row locks.

	A native save commits after Document.save returns, so release on transaction completion,
	not on returning from validate/save. The dedicated API already owns the same mutex.
	"""
	if _saving.get() or getattr(frappe.local, "budget_settings_lock_release", None):
		return
	guard = sync_lock(LOCK_NAME)
	if not guard.__enter__():
		guard.__exit__(None, None, None)
		frappe.throw(_("Budget synchronization is running. Try again after it finishes."))
	released = False

	def release():
		nonlocal released
		if not released:
			released = True
			frappe.local.budget_settings_lock_release = None
			guard.__exit__(None, None, None)

	try:
		frappe.db.after_commit.add(release)
		frappe.db.after_rollback.add(release)
		frappe.local.budget_settings_lock_release = release
	except Exception:
		release()
		raise


def validate_settings(doc):
	old = doc.get_doc_before_save()
	from . import organization_mapping
	from .ledger_source_transition import transition_authorized
	source = resolve_source(doc)
	previous = resolve_source(old) if old else None
	verified_switch = transition_authorized(previous, source)
	organization_mapping.validate(doc, source["corp_id"])
	if old and doc.get("ledger_source_fingerprint") != old.get("ledger_source_fingerprint") and not verified_switch:
		frappe.throw(_("The ledger source fingerprint is managed by synchronization."))
	fields = ("ledger_corp_id", "ledger_connection_profile", "ledger_sync_enabled")
	if (old and all(doc.get(key) == old.get(key) for key in fields)
		and organization_mapping.values(doc.get("organization_mappings")) == organization_mapping.values(old.get("organization_mappings"))):
		return
	if doc.get("ledger_connection_profile") not in (None, "", "budget", "shared_oa", "operating_oa"):
		frappe.throw(_("Invalid budget connection profile."))
	if source["corp_id"] and not source["scope_configured"]:
		frappe.throw(_("Enter a valid OA corporation ID."))
	if source["enabled"] and not source["configured"]:
		frappe.throw(_("Complete the corporation scope and server connection before enabling synchronization."))
	bound = (previous["bound_fingerprint"] or previous["fingerprint"]) if previous else source["bound_fingerprint"]
	if (bound and bound != source["fingerprint"] and not verified_switch
		and (frappe.db.count("Budget Ledger Entry") or (previous or {}).get("organization_mappings"))):
		frappe.throw(_("The ledger contains data. Its corporation and database cannot be changed here."))
	if bound != source["fingerprint"] and not verified_switch:
		doc.ledger_source_fingerprint = ""


@frappe.whitelist()
def get_sync_settings():
	frappe.only_for("System Manager")
	source = current_source()
	return public_settings(source) | {"organization_mappings": source["organization_mappings"],
		"ledger_count": frappe.db.count("Budget Ledger Entry")}


@frappe.whitelist(methods=["POST"])
def save_sync_settings(corp_id, profile, enabled, expected_version, organization_mappings=None):
	frappe.only_for("System Manager")
	with sync_lock(LOCK_NAME) as acquired:
		if not acquired:
			frappe.throw(_("Budget synchronization is running. Try again after it finishes."))
		doc = frappe.get_doc(SETTINGS)
		if resolve_source(doc)["version"] != expected_version:
			frappe.throw(_("Budget sync settings changed. Reopen the settings and try again."))
		doc.ledger_corp_id = str(corp_id or "").strip()
		doc.ledger_connection_profile = profile
		doc.ledger_sync_enabled = int(str(enabled) in ("1", "true", "True"))
		if organization_mappings is not None:
			from .organization_mapping import values, require_schema
			require_schema()
			rows = frappe.parse_json(organization_mappings) if isinstance(organization_mappings, str) else organization_mappings
			if not isinstance(rows, list) or any(not isinstance(row, dict) for row in rows):
				frappe.throw(_("Invalid organization mappings."))
			doc.set("organization_mappings", values(rows))
		token = _saving.set(True)
		try:
			doc.save()
			# Publish the new configuration before releasing the worker's mutex.
			frappe.db.commit()
		except Exception:
			frappe.db.rollback()
			raise
		finally:
			_saving.reset(token)
		return public_settings(resolve_source(doc))


@frappe.whitelist()
def get_ledger_organization_options():
	frappe.only_for(("System Manager", "Accounts Manager"))
	source = current_source()
	empty = {"companies": [], "departments": [], "groups": []}
	if not source["configured"]:
		return empty | {"available": False, "reason": "not_configured"}
	from .approval_query_service import list_organization_options
	try:
		return list_organization_options(get_ledger_oa_source()) | {"available": True}
	except Exception:
		# The list can still display its saved filters and existing local ledger.
		return empty | {"available": False, "reason": "organization_unavailable"}


@frappe.whitelist()
def get_sync_state():
	"""Safe status projection: no SQL, connection secrets, or exception payloads reach the page."""
	frappe.only_for(("System Manager", "Accounts Manager"))
	from frappe.utils.background_jobs import get_job
	from .oa_budget_sync import LEDGER_JOB_ID, SYNC_LOG_DOCTYPE, LOG_COUNT_FIELDS
	job = get_job(LEDGER_JOB_ID)
	job_state = job.get_status(refresh=True) if job else None
	job_state = getattr(job_state, "value", job_state)
	rows = frappe.get_all(SYNC_LOG_DOCTYPE,
		fields=["name", "status", "started_at", "finished_at", "creation", "run_id", "reason_code", *LOG_COUNT_FIELDS],
		order_by="started_at desc, creation desc", limit_page_length=1)
	last = dict(rows[0]) if rows else None
	# A scheduled run can be newer than the last manual RQ job retained in Redis.
	job_end = getattr(job, "ended_at", None)
	newer_log = bool(last and job_end and frappe.utils.get_datetime(last.get("creation") or last["started_at"]) >
		frappe.utils.convert_utc_to_system_timezone(job_end).replace(tzinfo=None))
	state = "idle"
	reason = None
	if job_state in ("queued", "deferred", "scheduled"):
		state = "queued"
	elif job_state == "started" or is_locked(LOCK_NAME):
		state = "running"
	elif newer_log:
		state = {"Success": "success", "Failed": "failed", "Skipped": "skipped"}.get(last["status"], "unknown")
		reason = last.get("reason_code")
	elif job_state in ("failed", "stopped", "canceled"):
		state = "failed"
	elif job_state == "finished":
		result = job.return_value()
		if isinstance(result, dict):
			state = "skipped" if result.get("skipped") else ("success" if result.get("verified") else "unknown")
			reason = result.get("reason_code")
		else:
			state = "unknown"
	elif last:
		state = {"Success": "success", "Failed": "failed", "Skipped": "skipped"}.get(last["status"], "unknown")
		reason = last.get("reason_code")
	return {"settings": public_settings(current_source()), "state": state,
		"reason": reason, "last": last, "ledger_count": frappe.db.count("Budget Ledger Entry")}
