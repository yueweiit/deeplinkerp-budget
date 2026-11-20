"""Bounded read-only snapshots from the same HTTP archive client used by MX."""
from __future__ import annotations

import hashlib
import json
import time
from uuid import uuid4

import frappe
from frappe import _

from .shared_operating_oa import client
from ..services.cn_approval_contract import KINDS, project
from ..services.oa_budget_parser import BUDGET_PROCESS_CODE

TTL = 600
MAX_ROWS = 20000
MAX_PAGES = 250
SNAPSHOT_WAIT_SECONDS = 75
SNAPSHOT_LOCK_SECONDS = 180


def configuration():
	from deeplinkerp_branding.services.mexico_expense_templates import DEFAULT_CODES, FIELDS
	from deeplinkerp_branding.services.mexico_expense_classification import validate_fields
	from deeplinkerp_branding.services.operating_oa_source import PROCESS_CODES
	http = client()
	url, corps = http.configuration()
	configured = frappe.conf.get("cn_approval_templates")
	if configured is None:
		codes = {"budget": (BUDGET_PROCESS_CODE,), "operating": tuple(dict.fromkeys((*PROCESS_CODES, *DEFAULT_CODES["operating"]))),
			"purchase": DEFAULT_CODES["purchase"]}
		configured = [{"approval_type": kind, "corp_id": corp, "process_code": code}
			for corp in corps for kind, values in codes.items() for code in values]
	if not isinstance(configured, list) or not configured or len(configured) > 100:
		frappe.throw(_("CN approval templates are not configured correctly."))
	templates, seen = [], set()
	for item in configured:
		if not isinstance(item, dict):
			frappe.throw(_("CN approval templates are not configured correctly."))
		key = (item.get("corp_id"), item.get("process_code"))
		if (item.get("approval_type") not in KINDS or key[0] not in corps or not isinstance(key[1], str)
			or not key[1].startswith("PROC-") or len(key[1]) > 200 or key in seen):
			frappe.throw(_("CN approval templates are not configured correctly."))
		mapping = item.get("field_mapping") or {}
		classification_fields = item.get("classification_fields") or ""
		try:
			if isinstance(mapping, str):
				mapping = json.loads(mapping)
			if not isinstance(classification_fields, str):
				classification_fields = json.dumps(classification_fields)
			validate_fields(classification_fields)
		except (ValueError, TypeError):
			frappe.throw(_("CN approval templates are not configured correctly."))
		if not isinstance(mapping, dict) or set(mapping) - set(FIELDS):
			frappe.throw(_("CN approval templates are not configured correctly."))
		if any(not isinstance(ids, list) or not ids or len(ids) > 20 or
			any(not isinstance(value, str) or len(value) > 140 for value in ids) for ids in mapping.values()):
			frappe.throw(_("CN approval templates are not configured correctly."))
		seen.add(key)
		templates.append({"approval_type": item["approval_type"], "corp_id": key[0], "process_code": key[1],
			"field_mapping": json.dumps(mapping), "classification_fields": classification_fields})
	return url, templates


def cache_key(url, templates):
	identity = json.dumps([url, templates], sort_keys=True).encode()
	return "cn-approval-workbench:v3:" + hashlib.sha256(identity).hexdigest()


def load_snapshot(url, templates, *, http=None, clock=time.monotonic):
	http = http or client()
	deadline, pages, total = clock() + 75, 0, 0
	output, seen = [], set()
	for template in templates:
		cursor, snapshot, cursors = None, None, set()
		while True:
			if clock() >= deadline or pages >= MAX_PAGES:
				frappe.throw(_("The approval scan is too large or timed out. Narrow the configured templates and retry."))
			rows, page = http.read_template_page(url, template["corp_id"], template["process_code"], cursor=cursor)
			pages += 1
			if snapshot is not None and page["syncUntil"] != snapshot:
				frappe.throw(_("OA approval snapshot changed. Retry synchronization."))
			snapshot = page["syncUntil"]
			for row in rows:
				key = (row.get("corp_id"), row.get("process_instance_id"))
				if key in seen:
					frappe.throw(_("OA approval pagination returned duplicate records."))
				seen.add(key)
				total += 1
				if total > MAX_ROWS:
					frappe.throw(_("The approval scan is too large or timed out. Narrow the configured templates and retry."))
				item = project(row, template)
				if item is not None:
					output.append(item)
			if not page["hasMore"]:
				break
			cursor = page["nextCursor"]
			if not cursor or cursor in cursors:
				frappe.throw(_("OA approval pagination did not advance."))
			cursors.add(cursor)
	return {"rows": output, "fetched_at": frappe.utils.now(), "template_count": len(templates)}


def get_snapshot(*, refresh=False):
	url, templates = configuration()
	key = cache_key(url, templates)
	cached = frappe.cache.get_value(key, use_local_cache=False)
	if cached is not None and not refresh:
		return cached
	initial_id = cached.get("snapshot_id") if cached is not None else None
	# Redis.lock does not apply Frappe's site prefix itself. Wait for the ongoing
	# scan, then bypass request-local caching to see the other worker's result.
	lock = frappe.cache.lock(frappe.cache.make_key(key + ":lock"),
		timeout=SNAPSHOT_LOCK_SECONDS, blocking_timeout=SNAPSHOT_WAIT_SECONDS)
	waited = not lock.acquire(blocking=False)
	if waited and not lock.acquire(blocking=True):
		frappe.throw(_("Approval data is being refreshed. Please retry shortly."))
	try:
		current = frappe.cache.get_value(key, use_local_cache=False)
		if current is not None and (not refresh or current.get("snapshot_id") != initial_id):
			return current
		if waited:
			# The owner failed without publishing a result. Do not start another
			# full scan after waiting, or misreport the previous snapshot as fresh.
			frappe.throw(_("The concurrent approval refresh did not complete. Please retry."))
		# Only complete snapshots replace the cache. No business documents are written.
		snapshot = {**load_snapshot(url, templates), "snapshot_id": uuid4().hex}
		frappe.cache.set_value(key, snapshot, expires_in_sec=TTL)
		return snapshot
	finally:
		lock.release()
