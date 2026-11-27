"""Resolve original approvals on demand after a reporting row is authorized.

Links are never published with administrator-built reporting snapshots. The
source service rechecks whole-approval access using the current user's scope.
"""
import re

import frappe

from deeplinkerp_branding.services import mexico_expenses as mx
from deeplinkerp_branding.services import regional_expenses as regional

from . import cn_approval_management as management
from . import cn_approval_workbench as approvals
from ..integrations.cn_approval_history import read_snapshot


def original_url(row, context, rules):
	resolved = authorized_source(row, context, rules)
	return mx._dingtalk_url(resolved[1]) if resolved else ""


def original_context(row, context, rules):
	"""Return a small source-line preview only after whole-source authorization."""
	from .expense_source_details import original_expense
	resolved = authorized_source(row, context, rules)
	if not resolved:
		return {"approval_url": "", "original_expense": None}
	kind, source, selected = resolved
	original = original_expense(kind, source, selected, row)
	result = {"approval_url": mx._dingtalk_url(source), "original_expense": original}
	if kind == "regional" and original:
		from .expense_amount_review import preview
		reconciliation = preview(source, row)
		if reconciliation:
			result["amount_reconciliation"] = reconciliation
	return result


def authorized_source(row, context, rules):
	key, source_key = row.get("key"), row.get("source_key")
	if not all(isinstance(value, str) and re.fullmatch(r"[0-9a-f]{64}", value)
		for value in (key, source_key)):
		return None
	if row["kind"] != "budget":
		if frappe.db.exists(regional.DOCTYPE, source_key):
			return regional_source(row, context)
		if frappe.db.exists(mx.LINE, {"name": key, "source": source_key, "is_active": 1}):
			return legacy_source(row, context)
	return native_source(row, rules)


def regional_source(row, context):
	record = frappe.get_doc(regional.DOCTYPE, row["source_key"])
	regional.require_connection(record)
	entries = regional.decode(record.allocations_json)
	# Reject a stale publication's line key or ownership before following its
	# source. The source's whole-document guard also checks every other owner.
	selected = next((entry for index, entry in enumerate(entries)
		if regional.digest([record.name, "regional-" + entry["region"], index]) == row["key"]
		and entry["company"] == row["company"]), None)
	if selected is None:
		return None
	if record.origin == "cn":
		# Retain the CN source's existing restriction on whole shared approvals.
		if not context["manager"]:
			return None
	elif record.origin != "mx" or not mx_source_readable():
		return None
	if any((entry["company"] or entry.get("matched_company")) not in context["companies"]
		for entry in entries if entry["company"] or entry.get("matched_company")):
		return None
	# The current allocation is insufficient: removed native lines can still
	# protect other subsidiaries' comments and attachments on the same approval.
	if not mx._whole_source_allowed(record.name, companies=context["companies"]):
		return None
	return "regional", record, selected


def mx_source_readable():
	muted = frappe.flags.mute_messages
	try:
		frappe.flags.mute_messages = True
		mx._finance()
	except frappe.PermissionError:
		# Report access alone does not grant the legacy source's field permissions.
		return False
	finally:
		frappe.flags.mute_messages = muted
	return True


def legacy_source(row, context):
	if not mx_source_readable():
		return None
	visible = mx._get_management_lines(filters={"name": row["key"], "source": row["source_key"], "is_active": 1},
		fields=["name", "company", "source_version"], limit_page_length=1)
	if not visible or visible[0].company != row["company"]:
		return None
	if not mx._whole_source_allowed(row["source_key"], companies=context["companies"]):
		return None
	return "legacy", frappe.get_doc(mx.SOURCE, row["source_key"]), visible[0]


def native_source(row, rules):
	source = next((item for item in read_snapshot()["rows"] if item["key"] == row["source_key"]), None)
	if source is None:
		return None
	context = approvals.access_context()
	visible = management.analytical_lines([source], context, rules)
	selected = next((item for item in visible if item["key"] == row["key"] and item["company"] == row["company"]), None)
	if selected is None:
		return None
	full_context = {**context, "manager": True, "companies": context["known_companies"]}
	all_lines = management.analytical_lines([source], full_context, rules)
	if not {item["key"] for item in all_lines} <= {item["key"] for item in visible}:
		return None
	return "native", source, selected
