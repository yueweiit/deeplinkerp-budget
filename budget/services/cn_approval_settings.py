"""Managed CN configuration; MX keeps its own templates and company aliases."""
import hashlib
import json
from contextlib import contextmanager
from contextvars import ContextVar

import frappe
from frappe import _
from frappe.utils import cint
from . import cn_approval_management_rules as management

SETTINGS = "CN Approval Settings"
TEMPLATE_FIELDS = ("enabled", "approval_type", "template_name", "corp_id", "process_code",
	"field_mapping", "classification_fields", "history_state")
MAP_FIELDS = ("corp_id", "source_name", "company")
_managed = ContextVar("cn_approval_settings_write", default=False)


@contextmanager
def managed_write():
	token = _managed.set(True)
	try:
		yield
	finally:
		_managed.reset(token)


def validate_managed_document(doc):
	if not _managed.get():
		frappe.throw(_("CN approval settings can only be changed through the workbench."), frappe.PermissionError)


def read_saved(*, lock=False):
	# No Redis/doc cache: a running worker must observe configuration changes.
	if not frappe.db.exists("DocType", SETTINGS):
		return None
	doc = frappe.get_doc(SETTINGS, SETTINGS, for_update=True) if lock else frappe.get_single(SETTINGS)
	if not doc.initialized:
		return None
	return {"enabled": cint(doc.enabled), "config_version": doc.config_version or "",
		"templates": [{key: row.get(key) for key in TEMPLATE_FIELDS} for row in doc.templates],
		"company_mappings": [{key: row.get(key) for key in MAP_FIELDS} for row in doc.company_mappings],
		"management_rules": management.clean(doc.get("management_rules") or {})}


def management_rules():
	saved = read_saved()
	return management.clean((saved or {}).get("management_rules") or {})


def enabled():
	return sync_options()["enabled"]


def sync_options():
	saved = read_saved()
	return {"enabled": bool(saved["enabled"] if saved is not None else cint(frappe.conf.get("cn_approval_sync_enabled", 1))),
		"settings_version": saved["config_version"] if saved else ""}


def version():
	saved = read_saved()
	return saved["config_version"] if saved else ""


def company_mappings():
	saved = read_saved()
	return saved["company_mappings"] if saved else frappe.conf.get("cn_approval_company_map") or []


def can_manage(companies=None):
	if frappe.session.user == "Administrator":
		return True
	if "System Manager" not in frappe.get_roles():
		return False
	from .cn_approval_workbench import allowed_companies
	companies = allowed_companies() if companies is None else companies
	known = set(frappe.get_all("Company", filters={"is_group": 0}, pluck="name", limit_page_length=0))
	return known.issubset(companies)


def require_manager():
	# Configuration changes affect every reader, including previously unmapped
	# source names. A company-restricted manager cannot reassign hidden data.
	if not can_manage():
		frappe.throw(_("A System Manager with access to all subsidiaries is required to configure CN approvals."), frappe.PermissionError)


def parse(value):
	try:
		return json.loads(value) if isinstance(value, str) else value
	except (ValueError, TypeError):
		frappe.throw(_("Invalid CN approval settings."))


def clean_templates(entries, corps):
	from ..integrations.cn_approval_source import normalize_templates
	entries = parse(entries)
	clean = normalize_templates(entries, corps, allow_empty=True)
	for raw, item in zip(entries, clean):
		flag = raw.get("enabled", 0)
		name = raw.get("template_name") or ""
		if flag not in (0, 1, "0", "1") or not isinstance(name, str) or len(name) > 140:
			frappe.throw(_("Invalid CN approval settings."))
		item.update(enabled=cint(flag), template_name=name.strip())
	return clean


def clean_mappings(entries, corps):
	from .cn_approval_workbench import allowed_companies
	entries = parse(entries)
	if not isinstance(entries, list) or len(entries) > 1000:
		frappe.throw(_("Invalid subsidiary mappings"))
	companies, clean, seen = allowed_companies(), [], set()
	for item in entries:
		if (not isinstance(item, dict) or not isinstance(item.get("corp_id"), str)
			or item["corp_id"] not in corps or not isinstance(item.get("source_name"), str)
			or not item["source_name"].strip() or len(item["source_name"]) > 500
			or not isinstance(item.get("company"), str) or item["company"] not in companies):
			frappe.throw(_("Invalid subsidiary mappings"))
		key = (item["corp_id"], item["source_name"].strip())
		if key in seen:
			frappe.throw(_("Duplicate source entity mapping"))
		seen.add(key)
		clean.append({"corp_id": key[0], "source_name": key[1], "company": item["company"]})
	return clean


def revision(values):
	facts = {key: values[key] for key in ("enabled", "templates", "company_mappings")}
	facts["management_rules"] = values.get("management_rules") or management.defaults()
	return hashlib.sha256(json.dumps(facts, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def defaults(corps):
	from ..integrations.cn_approval_source import legacy_templates
	entries = legacy_templates(corps)
	# Legacy server templates were all active. Do not change their fetch scope.
	clean = clean_templates([{**item, "enabled": 1} for item in entries], corps)
	for item in clean:
		item["history_state"] = "expected"
	values = {"enabled": cint(frappe.conf.get("cn_approval_sync_enabled", 1)), "templates": clean,
		"company_mappings": frappe.conf.get("cn_approval_company_map") or [], "management_rules": management.defaults()}
	return {**values, "config_version": revision(values)}


def get_settings():
	require_manager()
	from ..integrations.shared_operating_oa import client
	from .cn_approval_workbench import allowed_companies
	_url, corps = client().configuration()
	return {**(read_saved() or defaults(corps)), "corporations": corps, "companies": sorted(allowed_companies()),
		"management_categories": management.CATEGORIES}


def save_settings(enabled, templates, company_mappings, expected_config_version, management_rules=None):
	# This API owns its transaction, just like the background enqueue command.
	for attempt in range(3):
		try:
			return save_once(enabled, templates, company_mappings, expected_config_version, management_rules)
		except frappe.QueryDeadlockError:
			frappe.db.rollback()
			if attempt == 2:
				raise


def save_once(enabled, templates, company_mappings, expected_config_version, management_rules=None):
	require_manager()
	from ..integrations.shared_operating_oa import client
	_url, corps = client().configuration()
	if enabled not in (0, 1, "0", "1"):
		frappe.throw(_("Invalid CN approval settings."))
	values = {"enabled": cint(enabled), "templates": clean_templates(templates, corps),
		"company_mappings": clean_mappings(company_mappings, corps)}
	# A stable parent lock also serializes the first save of an empty Single.
	if not frappe.db.get_value("DocType", SETTINGS, "name", for_update=True):
		frappe.throw(_("CN approval settings require a site migration."))
	current = read_saved(lock=True) or defaults(corps)
	if current["config_version"] != expected_config_version:
		frappe.throw(_("Settings changed; reload and try again"))
	try:
		values["management_rules"] = management.clean(management_rules if management_rules is not None else current.get("management_rules") or {})
	except (TypeError, ValueError) as error:
		frappe.throw(_(str(error)))
	old = {(row["corp_id"], row["process_code"]): row for row in current["templates"]}
	new = {(row["corp_id"], row["process_code"]): row for row in values["templates"]}
	if old.keys() - new.keys():
		frappe.throw(_("Disable historical templates instead of removing them"))
	if any(row["approval_type"] != new[key]["approval_type"] for key, row in old.items()):
		frappe.throw(_("An existing template cannot change approval form"))
	for key, row in new.items():
		previous = old.get(key)
		# Never trust a client-supplied history flag. A template can leave the
		# never-enabled state only once; legacy rows without evidence stay strict.
		row["history_state"] = "never_enabled" if not row["enabled"] and (
			previous is None or previous.get("history_state") == "never_enabled"
		) else "expected"
	values["config_version"] = revision(values)
	persist(values)
	return {"ok": True, "config_version": values["config_version"]}


def persist(values):
	if not frappe.get_meta("CN Approval Template").has_field("history_state"):
		frappe.throw(_("CN approval settings require a site migration."))
	doc = frappe.get_doc(SETTINGS, SETTINGS, for_update=True)
	doc.update({**values, "management_rules": json.dumps(values.get("management_rules") or management.defaults(), ensure_ascii=False), "initialized": 1})
	with managed_write():
		doc.save(ignore_permissions=True)


def preview_template(template):
	require_manager()
	from ..integrations.shared_operating_oa import client
	from .cn_approval_contract import project, ISSUES
	http = client()
	url, corps = http.configuration()
	clean = clean_templates([parse(template)], corps)[0]
	rows, _page = http.read_template_page(url, clean["corp_id"], clean["process_code"], limit=3)
	projected = [item for row in rows if (item := project(row, clean)) is not None]
	issues = sorted({issue for item in projected for issue in item["issues"]})
	return {"sample_count": len(rows), "cn_count": len(projected), "issues": [_(ISSUES[key]) for key in issues]}


def get_unmapped_sources(start=0):
	require_manager()
	from .cn_approval_workbench import access_context
	from ..integrations.cn_approval_history import read_snapshot
	from .cn_approval_management import allocation_sources, company_for
	try:
		start = int(start)
	except (ValueError, TypeError):
		frappe.throw(_("Invalid CN approval filters."))
	if not 0 <= start <= 20000:
		frappe.throw(_("Invalid CN approval filters."))
	rows, _context = read_snapshot()["rows"], access_context()
	counts = {}
	for row in rows:
		for entity in {row.get("entity", ""), *allocation_sources(row)} - {""}:
			if not company_for({**row, "entity": entity, "entity_id": row.get("entity_id") if entity == row.get("entity") else ""}, _context):
				key = (row["corp_id"], entity)
				counts.setdefault(key, set()).add(row["key"])
	items = [{"corp_id": corp, "source_name": name, "count": len(keys)} for (corp, name), keys in sorted(counts.items())]
	return {"rows": items[start:start + 50], "total": len(items), "start": start, "page_length": 50}


def get_unmapped_categories(start=0):
	require_manager()
	from .cn_approval_management import load_lines
	try:
		start = int(start)
	except (ValueError, TypeError):
		frappe.throw(_("Invalid CN approval filters."))
	if not 0 <= start <= 50000:
		frappe.throw(_("Invalid CN approval filters."))
	_snapshot, rows, _context, _rules = load_lines()
	groups = {}
	for row in rows:
		if not row["management_category"]:
			key = (row["kind"], tuple(row["source_path"]))
			groups.setdefault(key, set()).add(row["source_key"])
	items = [{"kind": kind, "source_path": list(path), "count": len(keys)} for (kind, path), keys in sorted(groups.items()) if path]
	return {"rows": items[start:start + 50], "total": len(items), "start": start}
