"""Verified transport transition without rewriting, importing or deleting a ledger."""
from __future__ import annotations

import hashlib
import json
import time
import uuid
from contextvars import ContextVar
from decimal import Decimal

import frappe
from frappe import _

from . import ledger_sync_settings as settings

_verified = ContextVar("budget_verified_source_transition", default=None)
TTL = 600
FACT_FIELDS = ("form_no", "process_instance_id", "department_id", "budget_kind", "budget_month",
               "total_amount", "detail_amount", "oa_process_code")


def transition_authorized(previous, source):
    proof = _verified.get()
    return bool(proof and previous and proof == (previous["fingerprint"], source["fingerprint"], source["corp_id"])
                and previous["corp_id"] == source["corp_id"] and source["profile"] == "operating_oa")


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, default=str).encode()).hexdigest()


def facts(row):
    result = {key: str(row.get(key) or "").strip() for key in FACT_FIELDS}
    for key in ("total_amount", "detail_amount"):
        number = Decimal(str(row.get(key) or 0))
        if not number.is_finite():
            frappe.throw(_("Budget source verification found invalid amounts."))
        result[key] = str(number.quantize(Decimal("0.01")))
    return result


def verify_ledger(local, remote):
    """Every saved approval must exist with the same identity and financial scope.

    Additional remote approvals are allowed, but are imported only by a later,
    explicit sync. Missing/non-CN approvals, mismatches and ambiguous IDs fail closed.
    """
    from .oa_budget_parser import parse_budget_instance, dedupe_latest
    candidates, parsed = {}, []
    for raw in remote:
        entry = parse_budget_instance(raw)["entry"]
        entry["department_id"] = entry.get("dept_id")
        entry["total_amount"] = entry.get("amount")
        key = entry.get("process_instance_id")
        if not key or key in candidates:
            frappe.throw(_("Budget source verification found ambiguous approval identities."))
        candidates[key] = facts(entry)
        parsed.append(entry)
    seen = set()
    for row in local:
        key = row.get("process_instance_id")
        if not key or key in seen or candidates.get(key) != facts(row):
            frappe.throw(_("The shared OA source does not match every local budget. No source was changed."))
        seen.add(key)
    if not seen:
        frappe.throw(_("The ledger is empty. Select the shared source in Sync Settings."))
    additional = sum(entry["process_instance_id"] not in seen for entry in dedupe_latest(parsed))
    return {"matched": len(seen), "additional": additional,
            "local_digest": digest(sorted([facts(row) for row in local], key=lambda row: row["process_instance_id"])),
            "remote_digest": digest(candidates)}


def _prepare(doc, expected_version, *, lock_rows=False):
    from ..integrations.shared_operating_oa import instances
    from .organization_mapping import require_schema
    require_schema()
    source = settings.resolve_source(doc)
    if source["version"] != expected_version:
        frappe.throw(_("Budget sync settings changed. Reopen the settings and try again."))
    if (source["profile"] not in ("budget", "shared_oa") or not source["scope_configured"]
            or (source["bound_fingerprint"] and source["bound_fingerprint"] != source["fingerprint"])):
        frappe.throw(_("Review the current ledger source binding before switching to the shared API."))
    candidate_doc = dict(ledger_connection_profile="operating_oa", ledger_corp_id=source["corp_id"], ledger_sync_enabled=0)
    candidate = settings.resolve_source(candidate_doc)
    if not candidate["configured"] or candidate["corp_id"] != source["corp_id"]:
        frappe.throw(_("The shared OA API must allow the same corporation as the ledger."))
    local = (frappe.db.sql("SELECT " + ", ".join(FACT_FIELDS) + " FROM `tabBudget Ledger Entry` ORDER BY name FOR UPDATE", as_dict=True)
             if lock_rows else frappe.get_all("Budget Ledger Entry", fields=list(FACT_FIELDS), limit_page_length=0))
    deadline = time.monotonic() + 90

    def check_deadline():
        if time.monotonic() >= deadline:
            frappe.throw(_("Budget source verification timed out. No source was changed."))

    remote = instances(candidate, check_deadline)
    check_deadline()
    proof = verify_ledger(local, remote)
    proof.update(version=source["version"], old=source["fingerprint"], new=candidate["fingerprint"], corp_id=source["corp_id"])
    return proof


def _key(token):
    return "budget-source-transition:" + str(token)


@frappe.whitelist(methods=["POST"])
def preview_shared_source(expected_version):
    frappe.only_for("System Manager")
    with settings.sync_lock(settings.LOCK_NAME) as acquired:
        if not acquired:
            frappe.throw(_("Budget synchronization is running. Try again after it finishes."))
        proof = _prepare(frappe.get_doc(settings.SETTINGS), expected_version)
        token = uuid.uuid4().hex
        frappe.cache.set_value(_key(token), {"user": frappe.session.user, "proof": proof}, expires_in_sec=TTL)
        return {"token": token, "matched": proof["matched"], "additional": proof["additional"]}


@frappe.whitelist(methods=["POST"])
def confirm_shared_source(token, expected_version):
    frappe.only_for("System Manager")
    with settings.sync_lock(settings.LOCK_NAME) as acquired:
        if not acquired:
            frappe.throw(_("Budget synchronization is running. Try again after it finishes."))
        try:
            return _confirm(token, expected_version)
        except Exception:
            # Release row locks and undo settings before releasing the sync mutex.
            frappe.db.rollback()
            raise


def _confirm(token, expected_version):
    saved = frappe.cache.get_value(_key(token))
    if not saved or saved.get("user") != frappe.session.user:
        frappe.throw(_("The source verification expired. Verify again before switching."))
    doc = frappe.get_doc(settings.SETTINGS)
    proof = _prepare(doc, expected_version, lock_rows=True)
    if saved.get("proof") != proof:
        frappe.throw(_("The source or ledger changed after verification. Verify again before switching."))
    # Only this verified, same-corporation transition may update the bound fingerprint.
    verified = _verified.set((proof["old"], proof["new"], proof["corp_id"]))
    saving = settings._saving.set(True)
    try:
        doc.ledger_connection_profile = "operating_oa"
        doc.ledger_corp_id = proof["corp_id"]
        doc.ledger_source_fingerprint = proof["new"]
        doc.ledger_sync_enabled = 0
        doc.save()
        doc.add_comment("Info", _("Switched to the shared OA API after verifying {0} budget approvals. Synchronization remains disabled.").format(proof["matched"]))
        frappe.cache.delete_value(_key(token))
        frappe.db.commit()
    finally:
        settings._saving.reset(saving)
        _verified.reset(verified)
    return {"switched": True, "matched": proof["matched"], "enabled": False}
