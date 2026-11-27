"""Whole-approval amount review, after the existing complete-source permission guard."""
import re

import frappe
from frappe import _
from frappe.utils import now_datetime

from deeplinkerp_branding.services import expense_amount_review as contract
from deeplinkerp_branding.services import mexico_expenses as mx
from deeplinkerp_branding.services import regional_expenses as regional
from deeplinkerp_branding.services import regional_expense_automatic as automatic

from . import cn_approval_workbench as approvals
from . import expense_group_mappings as groups


def current_facts(record):
	raw, template = regional.decode(record.source_json), regional.decode(record.template_json)
	settings = mx._settings()
	source, lines = regional.facts(raw, template, settings.business_timezone or "America/Mexico_City", record.origin == "cn")
	contract.preserve(source, lines, regional.decode(record.facts_json)[0])
	return source, lines


def preview(record, row):
	# Called only after original_context has authorized the entire approval and
	# matched the reporting publication to this exact allocation revision.
	if row.get("allocation_modified") != str(record.modified):
		return None
	stored, stored_lines = regional.decode(record.facts_json)
	if stored.get("approval_type") != "purchase":
		return None
	if not stored.get(contract.REVIEW) and not any("total" in line.get("base_issues", []) for line in stored_lines):
		return None
	source, lines = current_facts(record)
	proof = source.get(contract.EVIDENCE) or {}
	if not proof.get("tables"):
		return None
	entries = regional.decode(record.allocations_json)
	accepted = contract.accepted(source, lines, entries)
	review = source.get(contract.REVIEW)
	supported = contract.supported(source, lines, entries)
	can_confirm = supported and source.get("approval_status") == "approved" and not accepted
	whole = (len(lines) == len(entries) == 1 and lines[0].get("table_id") == "header"
		and entries[0].get("assignment_mode") == "automatic")
	reason = ("Only whole-approval expenses without separate allocations can use this confirmation."
		if not whole else "Resolve other source amount, currency or allocation issues before confirming the approval amount."
		if not supported else "The approval must be approved before confirming its amount."
		if source.get("approval_status") != "approved" else "")
	return {"source_key": record.name, "revision": str(record.modified), "fingerprint": contract.fingerprint(source, lines),
		"header_amount": proof["header_amount"], "currency": proof["currency"], "tables": proof["tables"],
		"can_confirm": can_confirm, "unavailable_reason": reason,
		"review": {key: review.get(key) for key in ("amount", "currency", "note", "reviewed_by", "reviewed_at")}
			| {"state": "confirmed" if accepted else "stale"} if review else None}


def confirm(key, source_key, expected_revision, expected_fingerprint, note):
	approvals.require_reader()
	if (not all(isinstance(value, str) and re.fullmatch(r"[0-9a-f]{64}", value)
		for value in (key, source_key, expected_fingerprint)) or not isinstance(expected_revision, str)
		or not expected_revision or len(expected_revision) > 100):
		frappe.throw(_("Invalid expense amount confirmation."))
	if not isinstance(note, str) or not note.strip() or len(note) > 1000:
		frappe.throw(_("A review note is required"))
	# Serialize Group rules with settings writers, and use a current locking
	# read rather than an earlier request snapshot or reporting publication.
	settings = mx._settings(lock=True)
	group_sources = groups.lookup(groups.read(lock=True), {row.get("corp_id") for row in mx._templates(settings)})
	record = frappe.get_doc(regional.DOCTYPE, source_key, for_update=True)
	entries = regional.decode(record.allocations_json)
	selected = next((entry for index, entry in enumerate(entries)
		if regional.digest([record.name, "regional-" + entry["region"], index]) == key), None)
	context = {"companies": approvals.allowed_companies(),
		"manager": frappe.session.user == "Administrator" or "System Manager" in frappe.get_roles()}
	from .expense_approval_links import regional_source
	if selected is None or not regional_source({"key": key, "source_key": source_key, "company": selected["company"]}, context):
		frappe.throw(_("Expense detail is unavailable or outside your permitted scope."), frappe.PermissionError)
	raw, template = regional.current_candidate(record.origin, record.name,
		identity=(record.corp_id, record.process_code, record.process_instance_id))
	source, lines = regional.facts(raw, template, settings.business_timezone or "America/Mexico_City", record.origin == "cn")
	previous = regional.decode(record.facts_json)[0]
	if (source.get("source_version") != previous.get("source_version")
		or contract.fingerprint(source, lines) != expected_fingerprint):
		frappe.throw(_("Expense source changed; reload and review again"))
	if not contract.supported(source, lines, entries) or source.get("approval_status") != "approved":
		frappe.throw(_("This approval cannot use whole-approval amount confirmation. Review its source and allocation first."))
	if not context["manager"] and groups.allocation_scope(group_sources, record.corp_id, lines[0], entries[0])[0]:
		frappe.throw(_("Expense detail is unavailable or outside your permitted scope."), frappe.PermissionError)
	version = automatic.configuration(settings)["version"]
	if any(entry.get("assignment_version") != version for entry in entries):
		frappe.throw(_("Expense ownership is being updated automatically. Please wait."))
	contract.preserve(source, lines, previous)
	old_review = source.get(contract.REVIEW) or {}
	# An identical retried POST is a no-op; concurrent different decisions fail.
	if contract.accepted(source, lines, entries) and old_review.get("note") == note.strip() and old_review.get("reviewed_by") == frappe.session.user:
		return {"ok": True}
	if str(record.modified) != expected_revision:
		frappe.throw(_("Expense allocation changed; reload and review again"))
	source[contract.REVIEW] = contract.decision(source, lines, note=note.strip(), user=frappe.session.user, when=now_datetime())
	record.facts_json = regional.fact_payload(source, lines)
	regional.save_record(record)
	from .expense_report_refresh import after_source_change
	after_source_change()
	return {"ok": True}
