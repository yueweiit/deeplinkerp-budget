"""Internal, site-local storage. Public reads must use the permission-scoped service."""
import base64
import json
import zlib

import frappe
from frappe import _

DOCTYPE = "CN Approval Snapshot"
FIELDS = ["name", "snapshot_id", "last_success_at", "sync_status", "sync_token", "queued_at",
	"started_at", "heartbeat_at", "finished_at", "completed_templates", "template_count", "page_count", "error_code"]
COMPRESS_AFTER = 1024 * 1024
MAX_SNAPSHOT_BYTES = 256 * 1024 * 1024
SNAPSHOT_ENCODING = "zlib-json-v1"


def encode_snapshot(payload):
	"""Keep large raw approval evidence lossless without exceeding DB packets."""
	text = json.dumps(payload, ensure_ascii=False)
	raw = text.encode("utf-8")
	if len(raw) > MAX_SNAPSHOT_BYTES:
		raise ValueError("CN snapshot size limit exceeded")
	if len(raw) < COMPRESS_AFTER:
		return text
	# History lookup filters on source_hash in SQL before decoding. Preserve
	# that exact JSON key/spacing; raw approvals and watermarks stay together.
	return json.dumps({"encoding": SNAPSHOT_ENCODING, "source_hash": payload.get("source_hash"),
		"data": base64.b64encode(zlib.compress(raw)).decode("ascii")}, ensure_ascii=False)


def decode_snapshot(value):
	payload = json.loads(value)
	if not isinstance(payload, dict):
		raise ValueError("Invalid CN snapshot")
	if "encoding" not in payload:
		return payload  # Existing uncompressed snapshots remain readable.
	if payload["encoding"] != SNAPSHOT_ENCODING:
		raise ValueError("Unsupported CN snapshot encoding")
	compressed = base64.b64decode(payload["data"], validate=True)
	decoder = zlib.decompressobj()
	raw = decoder.decompress(compressed, MAX_SNAPSHOT_BYTES + 1)
	if len(raw) > MAX_SNAPSHOT_BYTES or not decoder.eof or decoder.unused_data or decoder.unconsumed_tail:
		raise ValueError("Invalid or oversized CN snapshot")
	decoded = json.loads(raw)
	if not isinstance(decoded, dict) or decoded.get("source_hash") != payload.get("source_hash"):
		raise ValueError("CN snapshot source mismatch")
	return decoded


def prepare_snapshot(payload):
	value = encode_snapshot(payload)
	if frappe.db.db_type == "mariadb":
		limit = int(frappe.db.sql("select @@max_allowed_packet")[0][0])
		# Measure the escaped SQL literal, with room for UPDATE metadata. Reject
		# before sending: an oversized packet kills the transaction connection.
		if len(frappe.db.escape(value).encode("utf-8")) + 65536 >= limit:
			raise ValueError("CN snapshot exceeds database packet limit")
	return value


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
	payload = decode_snapshot(record["snapshot_json"])
	return {**payload, "snapshot_id": record["snapshot_id"], "has_snapshot": True,
		"fetched_at": str(record["last_success_at"])}
