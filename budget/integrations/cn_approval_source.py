"""Bounded read-only snapshots from the same HTTP archive client used by MX."""
from __future__ import annotations

import hashlib
import json
import re
import time

import frappe
from frappe import _

from .shared_operating_oa import client
from ..services.cn_approval_contract import KINDS, project

MAX_ROWS = 20000
MAX_PAGES = 250
SNAPSHOT_VERSION = 4


def configuration():
	from ..services.cn_approval_settings import read_saved
	url, corps = client().configuration()
	saved = read_saved()
	configured = [item for item in saved["templates"] if item["enabled"]] if saved else legacy_templates(corps)
	return url, normalize_templates(configured, corps, allow_empty=True)


def legacy_templates(corps):
	# Only explicitly configured legacy values may seed the workbench. A fresh
	# site must not select approval templates on the user's behalf.
	configured = frappe.conf.get("cn_approval_templates")
	return [] if configured is None else configured


def normalize_templates(configured, corps, *, allow_empty=False):
	from deeplinkerp_branding.services.mexico_expense_templates import FIELDS
	from deeplinkerp_branding.services.mexico_expense_classification import validate_fields
	if not isinstance(configured, list) or (not configured and not allow_empty) or len(configured) > 100:
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
			any(not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,140}", value) for value in ids) for ids in mapping.values()):
			frappe.throw(_("CN approval templates are not configured correctly."))
		seen.add(key)
		templates.append({"approval_type": item["approval_type"], "corp_id": key[0], "process_code": key[1],
			"field_mapping": json.dumps(mapping), "classification_fields": classification_fields})
	return templates


def cache_key(url, templates):
	# Keep saved legacy bindings intact, but changes to ignored region bindings
	# must neither invalidate local data nor supersede a queued/running scan.
	effective_templates = []
	for template in templates:
		mapping = template.get("field_mapping") or {}
		if isinstance(mapping, str):
			mapping = json.loads(mapping)
		mapping = {key: ids for key, ids in mapping.items() if key != "region"}
		effective_templates.append({**template, "field_mapping": json.dumps(mapping, sort_keys=True)})
	# Durable storage uses the digest, not the prefix. Include the projection
	# version so region-filtered snapshots cannot stand in for the new scope.
	identity = json.dumps([SNAPSHOT_VERSION, url, effective_templates], sort_keys=True).encode()
	return f"cn-approval-workbench:v{SNAPSHOT_VERSION}:" + hashlib.sha256(identity).hexdigest()


def load_snapshot(url, templates, *, http=None, clock=time.monotonic, progress=None):
	http = http or client()
	deadline, pages, total = clock() + 75, 0, 0
	output, seen = [], set()
	for index, template in enumerate(templates):
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
			if progress:
				progress(completed_templates=index + int(not page["hasMore"]), page_count=pages)
			if not page["hasMore"]:
				break
			cursor = page["nextCursor"]
			if not cursor or cursor in cursors:
				frappe.throw(_("OA approval pagination did not advance."))
			cursors.add(cursor)
	return {"rows": output, "fetched_at": frappe.utils.now(), "template_count": len(templates),
		"source_hash": hashlib.sha256(url.encode()).hexdigest(),
		"templates": [{key: template[key] for key in ("corp_id", "process_code", "approval_type")} for template in templates]}


def get_snapshot(*, refresh=False):
	from ..services import cn_approval_sync as sync
	if refresh:
		sync.request_sync()
	return sync.read_snapshot()
