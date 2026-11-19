"""Budget quotas projected from the shared OA approval snapshot.

The ledger sync owns the source/configuration lock and the transaction. Quotas
are reconciled in that same transaction; this module never opens a budget DB.
Historical quotas outside the snapshot are deactivated, not deleted.
"""
from __future__ import annotations

import json
from typing import Any

import frappe
from frappe import _

from . import ledger_sync_settings as settings
from .oa_budget_parser import BUDGET_PROCESS_CODE
from .oa_budget_quota import money, project
from .sync_lock import is_locked

QUOTA_DOCTYPE = "Budget Quota"
SUBJECT_DOCTYPE = "Budget Quota Subject"
SYNC_LOCK_NAME = settings.LOCK_NAME


class QuotaSyncVerificationError(frappe.ValidationError):
	"""The written approval-derived quota snapshot did not match its projection."""


def _datetime_value(value):
	if not value:
		return None
	value = frappe.utils.get_datetime(value)
	if value.tzinfo is not None:
		value = frappe.utils.convert_utc_to_system_timezone(value).replace(tzinfo=None)
	return value


def _json_text(value: Any) -> str | None:
	"""jsonb 列 → DocType 的 Code(JSON) 文本；空值保持 NULL。"""
	if value in (None, "", [], {}):
		return None
	if isinstance(value, str):
		return value
	return json.dumps(value, ensure_ascii=False)


def _month_start(value: Any) -> str | None:
	"""``2026-05`` → ``2026-05-01``：Date 字段需要完整日期才能用原生日历筛选。"""
	text = str(value or "").strip()
	if not text:
		return None
	return f"{text}-01" if len(text) == 7 and text[4] == "-" else text


def _flag(value: Any) -> int:
	return 1 if value else 0


def _amount(value: Any) -> float:
	return float(value or 0)


def _quota_values(row: dict, synced_at: str) -> dict:
	department_id = str(row.get("department_id") or "").strip()
	company_dept_id = str(row.get("company_dept_id") or "").strip()
	return {
		"form_no": row.get("source_form_no"),
		"corp_id": row.get("corp_id"),
		"process_instance_id": row.get("process_instance_id"),
		"oa_process_code": row.get("source_process_code"),
		"budget_type": row.get("budget_type"),
		"budget_year": row.get("budget_year"),
		"budget_month": _month_start(row.get("budget_month")),
		"group_dept_id": row.get("group_dept_id"),
		"group_name": row.get("group_name"),
		"company_dept_id": row.get("company_dept_id"),
		"company_name": row.get("company_name"),
		"department_id": row.get("department_id"),
		"department_name": row.get("department_name"),
		"department_is_company": _flag(
			department_id and department_id == company_dept_id
		),
		"department_source": row.get("department_source"),
		"organization_is_current": _flag(row.get("organization_is_current")),
		"dept_path_ids": _json_text(row.get("department_path_ids")),
		"dept_path_names": _json_text(row.get("department_path_names")),
		"total_amount": _amount(row.get("total_amount")),
		"detail_amount": _amount(row.get("detail_amount")),
		"detail_amount_mismatch": _flag(row.get("detail_amount_mismatch")),
		"owner_user_id": row.get("owner_user_id"),
		"owner_name": row.get("owner_name"),
		"status": row.get("status"),
		"approval_status": row.get("approval_status"),
		"approval_result": row.get("approval_result"),
		"effective_at": _datetime_value(row.get("effective_at")),
		"deactivated_at": _datetime_value(row.get("deactivated_at")),
		"approval_completed_at": _datetime_value(row.get("approval_completed_at")),
		"source_created_at": _datetime_value(row.get("source_created_at")),
		"source_updated_at": _datetime_value(row.get("source_updated_at")),
		"synced_at": synced_at,
	}


def apply_approval_snapshot(instances, paths, source):
	"""Called only by the locked ledger worker. Never commits or opens a connection."""
	from .oa_budget_sync import _check_deadline

	if source["profile"] != "operating_oa":
		frappe.throw(_("Budget quotas require the shared OA approval source."))
	rows, subjects = project(instances, paths, source)
	synced_at = frappe.utils.now()
	existing = {row.name: row for row in frappe.get_all(QUOTA_DOCTYPE,
		fields=["name", "corp_id", "process_instance_id", "oa_process_code", "status", "deactivated_at"],
		limit_page_length=0)}
	# Validate every collision before touching any quota or subject.
	for row in rows:
		previous = existing.get(row["source_form_no"])
		if previous and (previous.corp_id != source["corp_id"]
			or previous.oa_process_code != BUDGET_PROCESS_CODE
			or previous.process_instance_id != row["process_instance_id"]):
			frappe.throw(_("Budget quota identity conflicts with existing data. Review the source first."))
	grouped = {}
	for line in subjects:
		grouped.setdefault(line["quota_id"], []).append(line)
	names, created, updated = [], 0, 0
	for row in rows:
		_check_deadline()
		values = _quota_values(row, synced_at)
		# Do not erase historical timestamps absent from the approval archive.
		for field in ("effective_at", "approval_completed_at"):
			if values[field] is None:
				values.pop(field)
		name = values["form_no"]
		previous = existing.get(name)
		if values["status"] == "deactivated":
			values["deactivated_at"] = (previous.deactivated_at if previous else None) or synced_at
		if previous:
			frappe.db.set_value(QUOTA_DOCTYPE, name, values, update_modified=False)
			updated += 1
		else:
			frappe.get_doc({"doctype": QUOTA_DOCTYPE, **values}).insert(ignore_permissions=True)
			created += 1
		frappe.db.delete(SUBJECT_DOCTYPE, {"quota_form_no": name})
		for line in grouped.get(name, []):
			frappe.get_doc({"doctype": SUBJECT_DOCTYPE, "quota_form_no": name,
				**{key: value for key, value in line.items() if key != "quota_id"},
				"synced_at": synced_at}).insert(ignore_permissions=True)
		names.append(name)
	deactivated = 0
	current_names = set(names)
	for name, previous in existing.items():
		if (name not in current_names and previous.corp_id == source["corp_id"]
			and previous.oa_process_code == BUDGET_PROCESS_CODE and previous.status == "effective"):
			frappe.db.set_value(QUOTA_DOCTYPE, name, {"status": "deactivated",
				"deactivated_at": synced_at, "synced_at": synced_at}, update_modified=False)
			deactivated += 1
	expected = {row["source_form_no"]: (money(row["total_amount"]), row["status"]) for row in rows}
	actual = {row.name: (money(row.total_amount), row.status) for row in frappe.get_all(QUOTA_DOCTYPE,
		filters={"name": ["in", names]}, fields=["name", "total_amount", "status"], limit_page_length=0)} if names else {}
	actual_subjects = frappe.get_all(SUBJECT_DOCTYPE, filters={"quota_form_no": ["in", names]},
		fields=["quota_form_no", "subject_type", "subject_code", "subject_name", "amount"], limit_page_length=0) if names else []
	def subject_key(line, identity):
		return (line[identity], line["subject_type"], line.get("subject_code") or "", line["subject_name"], money(line["amount"]))
	if (actual != expected or sorted(subject_key(row, "quota_form_no") for row in actual_subjects)
		!= sorted(subject_key(row, "quota_id") for row in subjects)):
		raise QuotaSyncVerificationError(_("Budget quota verification failed. Synchronization was rolled back."))
	return {"created": created, "updated": updated, "deactivated": deactivated,
		"local_total": len(rows), "subject_rows": len(subjects), "verified": True}


def sync_quotas():
	"""Compatibility entry: one shared scan updates both ledger and quota atomically."""
	from .oa_budget_sync import sync_from_oa
	source = settings.current_source()
	if source["profile"] != "operating_oa":
		return {"skipped": True, "reason_code": "shared_oa_required"}
	return sync_from_oa(trigger="Scheduled", expected_config_version=source["version"])


@frappe.whitelist(methods=["POST"])
def run_quota_sync():
	frappe.only_for("System Manager")
	from .oa_budget_sync import sync_from_oa
	source = settings.current_source()
	if source["profile"] != "operating_oa":
		frappe.throw(_("Budget quotas require the shared OA approval source."))
	return sync_from_oa(trigger="Manual", expected_config_version=source["version"])


@frappe.whitelist(methods=["POST"])
def enqueue_quota_sync():
	frappe.only_for("System Manager")
	from .oa_budget_sync import enqueue_oa_budget_sync
	if settings.current_source()["profile"] != "operating_oa":
		return {"queued": False, "blocked_by": "shared_oa_required"}
	return enqueue_oa_budget_sync()


@frappe.whitelist()
def get_quota_sync_status():
	frappe.only_for("System Manager")
	from .oa_budget_sync import _cache_client, CACHE_KEY
	from ..integrations.budget_quota_local_source import scope_sql
	where, params = scope_sql()
	local = frappe.db.sql(f"""SELECT COUNT(*) AS quota_count, COALESCE(SUM(q.total_amount), 0) AS total_amount,
		(SELECT COUNT(*) FROM `tabBudget Quota Subject` s JOIN `tabBudget Quota` q
		ON q.name = s.quota_form_no WHERE {where}) AS subject_count
		FROM `tabBudget Quota` q WHERE {where}""", params, as_dict=True)[0]
	return {"last": _cache_client().get_value(CACHE_KEY) or {},
		"local": local,
		"lock_held": is_locked(SYNC_LOCK_NAME)}
