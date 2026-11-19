"""Explicit, corporation-scoped organization assignments shared by review and sync."""
from __future__ import annotations

import frappe
from frappe import _

FIELDS = ("corp_id", "department_id", "company", "department", "organization_level")


def schema_state():
    """Inspect actual schema; a source checkout alone does not migrate a site."""
    column = frappe.db.has_column("Budget Ledger Entry", "reviewed_organization_level")
    ready = bool(column and frappe.db.table_exists("Budget Organization Mapping")
                 and frappe.get_meta("Budget Ledger Entry").has_field("reviewed_organization_level")
                 and frappe.get_meta("Budget Settings").has_field("organization_mappings"))
    return {"level_column": column, "ready": ready}


def require_schema():
    if not schema_state()["ready"]:
        frappe.throw(_("Budget organization review requires a database update. Ask an administrator to complete the update."))


def values(rows):
    return [{key: str(row.get(key) or "").strip() for key in FIELDS} for row in (rows or [])]


def validate(doc, corp_id):
    seen = set()
    for row in doc.get("organization_mappings") or []:
        for key in FIELDS:
            row.set(key, str(row.get(key) or "").strip())
        row.corp_id = row.corp_id or corp_id
        if not corp_id or row.corp_id != corp_id:
            frappe.throw(_("Organization mappings must belong to the configured OA corporation."))
        if (not row.department_id or len(row.department_id) > 64 or not row.company
                or row.company == "待确认" or len(row.company) > 140 or len(row.department) > 255):
            frappe.throw(_("Enter a valid organization ID, company and department."))
        if row.department_id in seen:
            frappe.throw(_("Each organization ID can have only one mapping."))
        seen.add(row.department_id)
        if row.organization_level not in ("company", "department"):
            frappe.throw(_("Select the organization level."))
        if row.organization_level == "department" and not row.department:
            frappe.throw(_("A department-level mapping requires a department name."))
        if row.organization_level == "company" and row.department:
            frappe.throw(_("A company-level mapping must leave the department name empty."))


def by_id(source):
    return {row["department_id"]: row for row in source.get("organization_mappings", [])
            if row["corp_id"] == source["corp_id"]}


def overlay_paths(paths, source):
    paths = {key: dict(value) for key, value in paths.items()}
    for key, row in by_id(source).items():
        paths.setdefault(key, {}).update(reviewed_company=row["company"],
            reviewed_department=row["department"], reviewed_organization_level=row["organization_level"])
    return paths


def apply_changes(doc):
    """Update only reviewed presentation fields, atomically with the saved mapping.

    Keep original OA paths, amounts and details intact; removing a mapping restores
    the path-derived presentation. Invalidate the digest so the next sync rechecks it.
    The settings controller holds the same mutex as synchronization until commit.
    """
    from .oa_budget_sync import _json_list, _presentation
    old = doc.get_doc_before_save()
    before = {row["department_id"]: row for row in values(old.get("organization_mappings") if old else [])}
    after = {row["department_id"]: row for row in values(doc.get("organization_mappings"))}
    changed = [key for key in before.keys() | after.keys() if before.get(key) != after.get(key)]
    if not changed:
        return
    require_schema()
    entries = frappe.get_all("Budget Ledger Entry", filters={"department_id": ["in", changed]},
            fields=["name", "department_id", "dept_name", "dept_path_names"], limit_page_length=0)
    for row in entries:
        mapping = after.get(row.department_id)
        company, department = ((mapping["company"], mapping["department"]) if mapping else
                              _presentation(row.dept_name or "", _json_list(row.dept_path_names)))
        frappe.db.set_value("Budget Ledger Entry", row.name, {"company": company, "department": department,
            "reviewed_organization_level": mapping["organization_level"] if mapping else "",
            "source_digest": ""})
    from .oa_budget_quota import organization
    from ..integrations.budget_quota_local_source import scope_filters
    from .ledger_sync_settings import resolve_source
    scope = scope_filters(resolve_source(doc))
    original_names = {row.name: row.dept_name for row in entries}
    for row in frappe.get_all("Budget Quota", filters={**scope, "department_id": ["in", changed]},
            fields=["name", "department_id", "dept_path_ids", "dept_path_names"], limit_page_length=0):
        path = {"ids": _json_list(row.dept_path_ids), "names": _json_list(row.dept_path_names)}
        mapping = after.get(row.department_id)
        if mapping:
            path.update(reviewed_company=mapping["company"], reviewed_department=mapping["department"],
                        reviewed_organization_level=mapping["organization_level"])
        # A quota without a ledger/path cannot recover an original OA label after
        # review removal. Clear that presentation instead of keeping a stale mapping.
        presentation = organization({"dept_id": row.department_id,
                                     "dept_name": original_names.get(row.name) or ""}, path)
        fields = ("company_name", "company_dept_id", "department_name", "organization_is_current")
        update = {key: presentation[key] for key in fields}
        update["department_is_company"] = bool(row.department_id and
                                                row.department_id == presentation["company_dept_id"])
        frappe.db.set_value("Budget Quota", row.name, update)


def _context(name, doc):
    from . import ledger_sync_settings as settings
    require_schema()
    row = frappe.db.get_value("Budget Ledger Entry", name, ["department_id", "dept_name"], as_dict=True)
    if not row or not row.department_id:
        frappe.throw(_("This budget has no organization ID. Correct the OA source first."))
    source = settings.resolve_source(doc)
    if not source["scope_configured"] or (source["bound_fingerprint"] and source["bound_fingerprint"] != source["fingerprint"]):
        frappe.throw(_("Review the ledger source binding before editing organization mappings."))
    return source, row


@frappe.whitelist()
def get_mapping_context(name):
    frappe.only_for("System Manager")
    from . import ledger_sync_settings as settings
    source, row = _context(name, frappe.get_doc(settings.SETTINGS))
    return {"department_id": row.department_id, "source_name": row.dept_name,
            "version": source["version"], "mapping": by_id(source).get(row.department_id)}


@frappe.whitelist(methods=["POST"])
def save_mapping(name, company, department, organization_level, expected_version):
    frappe.only_for("System Manager")
    from . import ledger_sync_settings as settings
    with settings.sync_lock(settings.LOCK_NAME) as acquired:
        if not acquired:
            frappe.throw(_("Budget synchronization is running. Try again after it finishes."))
        doc = frappe.get_doc(settings.SETTINGS)
        source, entry = _context(name, doc)
        if source["version"] != expected_version:
            frappe.throw(_("Budget sync settings changed. Reopen the settings and try again."))
        existing = next((row for row in doc.get("organization_mappings") or []
                         if row.department_id == entry.department_id and row.corp_id == source["corp_id"]), None)
        data = dict(corp_id=source["corp_id"], department_id=entry.department_id, company=company,
                    department=department, organization_level=organization_level)
        if existing:
            existing.update(data)
        else:
            doc.append("organization_mappings", data)
        token = settings._saving.set(True)
        try:
            doc.save()
            frappe.db.commit()
        except Exception:
            frappe.db.rollback()
            raise
        finally:
            settings._saving.reset(token)
    return {"saved": True}
