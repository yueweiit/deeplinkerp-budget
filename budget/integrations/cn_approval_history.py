"""Retain the last published facts of disabled templates in management reports."""
from copy import deepcopy
import hashlib
import json

import frappe
from frappe import _

from . import cn_approval_source as source, cn_approval_store as store
from ..services import cn_approval_settings as settings


def merge_retired(snapshot, disabled, records, source_hash):
	remaining = {(row["corp_id"], row["process_code"], row["approval_type"]) for row in disabled
		if row.get("history_state") != "never_enabled"}
	output, seen, retained = list(snapshot["rows"]), {row["key"] for row in snapshot["rows"]}, []
	for record in records:
		payload = json.loads(record["snapshot_json"])
		if payload.get("source_hash") != source_hash:
			continue
		templates = {(row["corp_id"], row["process_code"], row["approval_type"]) for row in payload.get("templates", [])}
		matched = remaining & templates
		if not matched:
			continue
		for original in payload.get("rows", []):
			if (original["corp_id"], original["process_code"], original["kind"]) in matched and original["key"] not in seen:
				seen.add(original["key"])
				output.append({**deepcopy(original), "retired_template": True})
		remaining -= matched
		retained.append({"fetched_at": str(record["last_success_at"]), "templates": len(matched)})
		if not remaining:
			break
	return {**snapshot, "rows": output, "retained_history": retained, "missing_history_templates": len(remaining)}


def read_snapshot():
	snapshot = source.get_snapshot()
	saved = settings.read_saved()
	disabled = [row for row in (saved or {}).get("templates", [])
		if not row["enabled"] and row.get("history_state") != "never_enabled"]
	if not disabled:
		return snapshot
	url, _active = source.configuration()
	_url, allowed_corps = source.client().configuration()
	disabled = [row for row in disabled if row["corp_id"] in allowed_corps]
	if not disabled:
		return snapshot
	source_hash = hashlib.sha256(url.encode()).hexdigest()
	# Only snapshots from this exact OA connection may be considered. The JSON
	# remains internal; Company permissions are applied after analytical splitting.
	records = frappe.get_all(store.DOCTYPE, filters={"snapshot_id": ["is", "set"],
		"snapshot_json": ["like", f'%"source_hash": "{source_hash}"%']},
		fields=["snapshot_json", "last_success_at"], order_by="last_success_at desc, name desc", limit_page_length=201)
	result = merge_retired(snapshot, disabled, records[:200], source_hash)
	if len(records) > 200 and result["missing_history_templates"]:
		frappe.throw(_("Historical CN snapshots exceed the lookup limit. Ask an administrator to review the archive."))
	return result
