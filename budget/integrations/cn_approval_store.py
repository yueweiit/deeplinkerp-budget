"""Internal, site-local storage. Public reads must use the permission-scoped service."""
import json

import frappe
from frappe import _

DOCTYPE = "CN Approval Snapshot"
FIELDS = ["name", "snapshot_id", "last_success_at", "sync_status", "sync_token", "queued_at",
	"started_at", "heartbeat_at", "finished_at", "completed_templates", "template_count", "page_count", "error_code"]


def require_schema():
	if not frappe.db.table_exists(DOCTYPE):
		frappe.throw(_("CN approval storage is not installed. Ask an administrator to run the Budget migration."))


def read(key, *, lock=False, payload=False):
	require_schema()
	return frappe.db.get_value(DOCTYPE, key, FIELDS + (["snapshot_json"] if payload else []),
		as_dict=True, for_update=lock)


def ensure(key):
	if not read(key):
		# The primary key arbitrates simultaneous first requests; the command
		# retries a whole transaction if MariaDB reports a snapshot conflict.
		frappe.get_doc({"doctype": DOCTYPE, "source_key": key, "sync_status": "idle"}).insert(
			ignore_permissions=True, ignore_if_duplicate=True)
	return read(key, lock=True)


def update(key, values):
	# Only this internal service writes the read-only DocType; no permission is
	# granted on the unfiltered payload through Desk, REST, reports or exports.
	frappe.db.set_value(DOCTYPE, key, values)


def snapshot(record):
	if not record or not record.get("snapshot_id"):
		return {"rows": [], "fetched_at": None, "snapshot_id": None, "has_snapshot": False}
	payload = json.loads(record["snapshot_json"])
	return {**payload, "snapshot_id": record["snapshot_id"], "has_snapshot": True,
		"fetched_at": str(record["last_success_at"])}
