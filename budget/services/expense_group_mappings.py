"""Versioned reporting-only assignments; never put virtual names in Company links."""
import hashlib
import json
from contextlib import contextmanager
from contextvars import ContextVar

import frappe
from frappe import _

TARGET = "__expense_group__"
PARENT = "__budget_expense_sync"
KEY = "group_company_mappings"
MAX_BYTES = 60000  # DefaultValue.defvalue is a MariaDB TEXT column.
_prospective = ContextVar("expense_group_prospective_rules", default=None)


def identity(row):
	kind, name = row.get("match_kind"), row.get("source_name")
	if kind not in {"organization", "entity", "department"} or not isinstance(name, str) or not name.strip() or len(name) > 500:
		raise ValueError("Invalid subsidiary mappings")
	corp = row.get("corp_id") or ""
	if not isinstance(corp, str) or kind == "organization" and not corp:
		raise ValueError("Invalid subsidiary mappings")
	return kind, corp if kind == "organization" else "", name.strip() if kind == "organization" else " ".join(name.split())


def clean(rows, corporations=None):
	if not isinstance(rows, list) or len(rows) > 2000:
		raise ValueError("Invalid subsidiary mappings")
	result, seen = [], set()
	for row in rows:
		if not isinstance(row, dict):
			raise ValueError("Invalid subsidiary mappings")
		key = identity(row)
		if key in seen or corporations is not None and key[0] == "organization" and key[1] not in corporations:
			raise ValueError("Invalid subsidiary mappings")
		seen.add(key)
		result.append(dict(zip(("match_kind", "corp_id", "source_name"), key)))
	result.sort(key=identity)
	if len(json.dumps(result, ensure_ascii=False).encode()) > MAX_BYTES:
		raise ValueError("Invalid subsidiary mappings")
	return result


def read(*, lock=False):
	raw = frappe.db.get_value("DefaultValue", {"parent": PARENT, "defkey": KEY}, "defvalue", for_update=lock)
	try:
		return clean(json.loads(raw)) if raw else []
	except (TypeError, ValueError):
		frappe.throw(_("Invalid subsidiary mappings"))


def version(rows):
	return hashlib.sha256(json.dumps(clean(rows), sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def save(rows):
	from frappe.defaults import set_default
	set_default(KEY, json.dumps(clean(rows), ensure_ascii=False), PARENT)


@contextmanager
def command(rows):
	"""Only the version-checked unified command can replace Group assignments."""
	token = _prospective.set({identity(row) for row in clean(rows)})
	try:
		yield
	finally:
		_prospective.reset(token)


def require_no_conflicts(rows):
	# Legacy writers call this after locking their native settings. Read the
	# latest Group row rather than an earlier repeatable-read snapshot.
	keys = _prospective.get()
	if keys is None:
		keys = {identity(row) for row in read(lock=True)}
	if keys & {identity(row) for row in rows}:
		frappe.throw(_("This source is assigned to Group. Change its ownership in Configure OA Synchronization."))


def merge(rows, group_rows):
	"""Expose one editable rule if a legacy writer left an older shadow alias.

	Group already wins in reporting. Reads do not delete the native alias;
	the ordinary versioned save reconciles both stores in one transaction.
	"""
	keys = {identity(row) for row in group_rows}
	return [row for row in rows if identity(row) not in keys] + [
		{**row, "company": TARGET, "excluded": 0, "reason": ""} for row in group_rows]


def lookup(rows, corporations):
	"""Organization rules retain corp scope; entity rules use MX template corps."""
	result = set()
	for row in rows:
		kind, corp, name = identity(row)
		for source in (corp,) if kind == "organization" else corporations:
			result.add((source, None if kind == "organization" else kind, name))
	return result


def matches(aliases, corp, kind, name):
	if not isinstance(name, str) or not name.strip():
		return False
	return ((corp, None, name.strip()) in aliases
		or kind in {"entity", "department"} and (corp, kind, " ".join(name.split())) in aliases)


def allocation_scope(aliases, corp, original, entry):
	"""Shared Group attribution for report projections and monetary write guards."""
	if not original or entry.get("assignment_mode") != "automatic":
		return False, False
	explicit = matches(aliases, corp, original.get("entity_kind"), original.get("source_entity"))
	unnamed = (entry.get("region") == "pending" and not entry.get("matched_company")
		and entry.get("assignment_issue") == "unmapped_company"
		and not str(original.get("source_entity") or "").strip())
	return bool(explicit or unnamed), explicit
