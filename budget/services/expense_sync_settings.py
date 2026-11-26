"""One editor over existing approval configuration, with atomic versioned saves."""
import frappe
from frappe import _

from deeplinkerp_branding.services import mexico_expenses as mx
from . import cn_approval_settings as cn

RULES = {"application": ("cn",), "expense": ("mx",), "both": ("cn", "mx")}
AUTOMATIC_RULES = {"budget": "application", "operating": "both", "purchase": "both", "expense": "expense"}
DEFAULTS_PARENT = "__budget_expense_sync"
DEFAULTS_KEY = "template_defaults"


def read_template_defaults(*, lock=False):
	# Read the site-scoped value directly, not user defaults or Redis. The existing
	# settings locks serialize changes, including creation of the first default.
	raw = frappe.db.get_value("DefaultValue", {"parent": DEFAULTS_PARENT, "defkey": DEFAULTS_KEY}, "defvalue", for_update=lock)
	return cn.parse(raw) if raw else {}


def save_template_defaults(values):
	from frappe.defaults import set_default
	set_default(DEFAULTS_KEY, frappe.as_json(values), DEFAULTS_PARENT)


def suggested_defaults(rows, corporations, stored):
	def common(field):
		values = {row.get(field) for row in rows} - {None, ""}
		return next(iter(values)) if len(values) == 1 else ""
	return {"corp_id": stored.get("corp_id") or (corporations[0] if len(corporations) == 1 else common("corp_id")),
		"processing_rule": stored.get("processing_rule") or common("processing_rule")}


def clean_defaults(values, corporations, *, processing_mode=None):
	values = cn.parse(values)
	if not isinstance(values, dict):
		frappe.throw(_("Invalid OA synchronization configuration."))
	result = {key: values.get(key) or "" for key in (("corp_id",) if processing_mode == "approval" else ("corp_id", "processing_rule"))}
	if not all(isinstance(value, str) for value in result.values()):
		frappe.throw(_("Invalid OA synchronization configuration."))
	if result["corp_id"] and result["corp_id"] not in corporations:
		frappe.throw(_("The common source corporation is not authorized."))
	if result.get("processing_rule") and result["processing_rule"] not in RULES:
		frappe.throw(_("Invalid common processing rules."))
	return result


def inherit_defaults(row, defaults):
	row = dict(row)
	# New identities must reach both parsers in the same canonical form. Keep
	# historical bindings immutable; normalizing them would be a migration.
	if not row.get("key") and isinstance(row.get("process_code"), str):
		row["process_code"] = row["process_code"].strip()
	if defaults is not None and not row.get("key"):
		row.update(defaults)
		# Only project a shared rule onto the parsers that support this form.
		if row.get("processing_rule") == "both":
			row["processing_rule"] = {"budget": "application", "expense": "expense"}.get(row.get("approval_type"), "both")
	return row


def validate_template(row, index, corporations=None, *, require_finance=True):
	for field, label in (("corp_id", _("Corporation ID")), ("processing_rule", _("Processing rules")),
		("process_code", _("Process Code")), ("template_name", _("Template Name")), ("approval_type", _("Approval Form"))):
		if not isinstance(row.get(field), str) or not row[field].strip():
			frappe.throw(_("Approval template row {0}: {1} is required.").format(index, label))
	if corporations is not None and row["corp_id"] not in corporations:
		frappe.throw(_("Approval template row {0}: the source corporation is not authorized.").format(index))
	rule, kind = row["processing_rule"], row["approval_type"]
	if kind not in AUTOMATIC_RULES or rule not in RULES or (kind == "budget" and rule != "application") or (kind == "expense" and rule != "expense"):
		frappe.throw(_("Approval template row {0}: the processing rules do not support this approval form.").format(index))
	if require_finance and rule in {"expense", "both"} and not any(isinstance(row.get(field), str) and row[field].strip()
		for field in ("finance_node_names", "finance_activity_ids")):
		frappe.throw(_("Approval template row {0}: specify finance node names or activity IDs in the template details.").format(index))


def identity(row):
	return (row.get("corp_id"), row.get("process_code"))


def prepare_template(raw, defaults, merged, processing_mode):
	"""Choose adapters on the server; retain historical source identities and mappings."""
	if processing_mode not in (None, "approval"):
		frappe.throw(_("Invalid OA synchronization configuration."))
	row = inherit_defaults(raw, defaults)
	if processing_mode != "approval":
		return row
	if any(row.get(key) is not None and not isinstance(row[key], str)
		for key in ("corp_id", "process_code", "approval_type")):
		frappe.throw(_("Invalid approval template"))
	previous = merged.get(identity(row))
	if previous:
		if row.get("key") != previous["key"] or row.get("approval_type") != previous["approval_type"]:
			frappe.throw(_("An existing template cannot change approval form"))
		row["processing_rule"] = previous["processing_rule"]
	elif row.get("key"):
		frappe.throw(_("Invalid approval template"))
	else:
		row["processing_rule"] = AUTOMATIC_RULES.get(row.get("approval_type"), "")
	# The unified editor cannot erase or rewrite evidence used by legacy views.
	for field in ("finance_node_names", "finance_activity_ids"):
		row[field] = (previous or {}).get(field) or ""
	return row


def mode(flags):
	flags = {bool(value) for value in flags}
	return "mixed" if len(flags) > 1 else "enabled" if True in flags else "disabled"


def enabled(value, previous):
	if value not in {"enabled", "disabled", "mixed"}:
		frappe.throw(_("Invalid OA synchronization configuration."))
	return int(bool(previous)) if value == "mixed" else int(value == "enabled")


def read(*, lock=False):
	cn.require_manager()
	# All shared ownership writers take MX settings before other settings locks.
	mx_doc = mx._settings(lock=lock)
	mx._require_settings_access(mx_doc)
	_url, corps = mx.http.configuration()
	if lock and not frappe.db.get_value("DocType", cn.SETTINGS, "name", for_update=True):
		frappe.throw(_("CN approval settings require a site migration."))
	cn_saved = cn.read_saved(lock=lock) or cn.defaults(corps)
	return cn_saved, mx_doc, corps


def merge_templates(cn_rows, mx_rows):
	grouped = {}
	for source, rows in (("cn", cn_rows), ("mx", mx_rows)):
		for row in rows:
			grouped.setdefault(identity(row), {})[source] = row
	result = []
	for key, bindings in grouped.items():
		first = bindings.get("cn") or bindings["mx"]
		if len({row.get("approval_type") for row in bindings.values()}) != 1:
			frappe.throw(_("Duplicate approval templates have conflicting types. Review the existing configuration."))
		item = {"key": mx.digest(key), "corp_id": key[0], "process_code": key[1],
			"template_name": first.get("template_name") or key[1], "approval_type": first.get("approval_type"),
			"sync_mode": mode(row.get("enabled") for row in bindings.values()),
			"processing_rule": "both" if len(bindings) == 2 else "application" if "cn" in bindings else "expense"}
		for source, prefix in (("cn", "application"), ("mx", "expense")):
			row = bindings.get(source) or {}
			item[prefix + "_fields"] = row.get("field_mapping") or "{}"
			item[prefix + "_classification"] = row.get("classification_fields") or ""
		for field in ("finance_node_names", "finance_activity_ids"):
			item[field] = (bindings.get("mx") or {}).get(field) or ""
		result.append(item)
	return result


def get_settings():
	cn_saved, mx_doc, corps = read()
	rows = merge_templates(cn_saved["templates"], mx._templates(mx_doc))
	stored_defaults = read_template_defaults()
	mappings = [{"match_kind": "organization", "corp_id": row["corp_id"],
		"source_name": row["source_name"], "company": row["company"], "excluded": 0, "reason": ""}
		for row in cn_saved["company_mappings"]]
	mappings += [{"match_kind": row.get("entity_kind") or "entity", "corp_id": "",
		"source_name": row.source_entity, "company": row.company or "",
		"excluded": int(row.get("expense_scope") == "non_mx"), "reason": row.get("scope_reason") or ""}
		for row in mx_doc.company_mappings or []]
	from .cn_approval_workbench import allowed_companies
	return {"sync_mode": mode((cn_saved["enabled"], mx_doc.enabled)), "processing_mode": "approval",
		"versions": {"cn": cn_saved["config_version"], "mx": mx_doc.config_version or ""},
		"business_timezone": mx_doc.business_timezone or "America/Mexico_City", "corporations": corps,
		"templates": rows, "company_mappings": mappings,
		"template_defaults": suggested_defaults(rows, corps, stored_defaults),
		"template_defaults_version": mx.digest(stored_defaults),
		"companies": sorted(allowed_companies()), "expense_companies": mx._companies(),
		"management_rules": cn_saved.get("management_rules") or cn.management.defaults(),
		"management_categories": cn.management.CATEGORIES}


def mapping_candidates(kind, start=0):
	"""Reuse authorized local sources; never preview or synchronize OA here."""
	read()  # Same combined configuration permissions as the editor and save.
	try:
		start = int(start)
	except (ValueError, TypeError):
		frappe.throw(_("Invalid mapping page"))
	limits = {"organizations": 20000, "entities": 1000000, "categories": 50000}
	if not isinstance(kind, str) or kind not in limits or not 0 <= start <= limits[kind]:
		frappe.throw(_("Invalid mapping page"))
	if kind == "organizations":
		result = cn.get_unmapped_sources(start)
	elif kind == "entities":
		result = mx.get_unmapped_entities(start)
		return {"rows": [{"match_kind": row["entity_kind"], "source_name": row["source_entity"],
			"count": row["line_count"], "suggestions": row["suggestions"]} for row in result["rows"]],
			"start": start, "has_more": result["has_more"]}
	else:
		from .expense_management import load
		rows, _context, _rules, _metadata = load()
		groups = {}
		for row in rows:
			if row.get("source_path") and not row.get("management_category"):
				key = (row["kind"], tuple(row["source_path"]))
				groups.setdefault(key, set()).add(row["source_key"])
		items = [{"kind": form, "source_path": list(path), "count": len(keys)}
			for (form, path), keys in sorted(groups.items())]
		result = {"rows": items[start:start + 50], "total": len(items)}
	return {"rows": result["rows"], "start": start, "has_more": start + 50 < result["total"]}


def split_templates(rows, cn_saved, mx_doc, *, corporations=None, template_defaults=None, processing_mode=None):
	if not isinstance(rows, list) or len(rows) > 200 or any(not isinstance(row, dict) for row in rows):
		frappe.throw(_("Invalid approval template"))
	old = {"cn": {identity(row): row for row in cn_saved["templates"]},
		"mx": {identity(row): row for row in mx._templates(mx_doc)}}
	merged = {identity(row): row for row in merge_templates(cn_saved["templates"], mx._templates(mx_doc))}
	result, seen = {"cn": [], "mx": []}, set()
	for index, raw in enumerate(rows, 1):
		row = prepare_template(raw, template_defaults, merged, processing_mode)
		validate_template(row, index, corporations, require_finance=processing_mode != "approval")
		key = identity(row)
		if key in seen:
			frappe.throw(_("Approval template row {0}: this source corporation and process code are already listed.").format(index))
		seen.add(key)
		previous = merged.get(key)
		if previous and (row.get("key") != previous["key"] or row.get("processing_rule") != previous["processing_rule"]):
			frappe.throw(_("Existing template processing rules cannot be changed."))
		if not previous and (row.get("key") or row.get("sync_mode") == "mixed"):
			frappe.throw(_("Invalid approval template"))
		processors = RULES.get(row.get("processing_rule"))
		if not processors:
			frappe.throw(_("Invalid approval template"))
		for source in processors:
			prefix = "application" if source == "cn" else "expense"
			original = old[source].get(key) or {}
			name = row.get("template_name") or ""
			# A merged label must not rename a second legacy binding on a no-op save.
			if previous and name == previous["template_name"]:
				name = original.get("template_name") or ""
			item = {"corp_id": key[0], "process_code": key[1], "approval_type": row.get("approval_type"),
				"template_name": name, "enabled": enabled(row.get("sync_mode"), original.get("enabled")),
				"field_mapping": row.get(prefix + "_fields") or "{}",
				"classification_fields": row.get(prefix + "_classification") or ""}
			if source == "mx":
				item.update({field: row.get(field) or "" for field in ("finance_node_names", "finance_activity_ids")})
			result[source].append(item)
	if merged.keys() - seen:
		frappe.throw(_("Disable historical templates instead of removing them"))
	return result


def split_mappings(rows):
	if not isinstance(rows, list) or len(rows) > 2000:
		frappe.throw(_("Invalid subsidiary mappings"))
	result = {"cn": [], "mx": []}
	for row in rows:
		if not isinstance(row, dict) or row.get("excluded", 0) not in (0, 1, "0", "1"):
			frappe.throw(_("Invalid subsidiary mappings"))
		kind, excluded = row.get("match_kind"), int(row.get("excluded") or 0)
		if kind == "organization":
			if excluded:
				frappe.throw(_("Organization mappings require a subsidiary."))
			result["cn"].append({key: row.get(key) for key in ("corp_id", "source_name", "company")})
		elif kind in {"entity", "department"}:
			result["mx"].append({"entity_kind": kind, "source_entity": row.get("source_name"),
				"company": row.get("company") or "", "expense_scope": "non_mx" if excluded else "mx",
				"scope_reason": row.get("reason") or ""})
		else:
			frappe.throw(_("Invalid subsidiary mappings"))
	return result


def save_settings(values):
	"""Own the whole command transaction; never let one source save alone."""
	values = cn.parse(values)
	if not isinstance(values, dict) or not isinstance(values.get("versions"), dict):
		frappe.throw(_("Invalid OA synchronization configuration."))
	processing_mode = values.get("processing_mode")
	if processing_mode not in (None, "approval"):
		frappe.throw(_("Invalid OA synchronization configuration."))
	for attempt in range(3):
		try:
			cn_saved, mx_doc, corps = read(lock=True)
			if values["versions"] != {"cn": cn_saved["config_version"], "mx": mx_doc.config_version or ""}:
				frappe.throw(_("Settings changed; reload and try again"))
			defaults = None
			if "template_defaults" in values:
				if values.get("template_defaults_version") != mx.digest(read_template_defaults(lock=True)):
					frappe.throw(_("Settings changed; reload and try again"))
				defaults = clean_defaults(values["template_defaults"], corps, processing_mode=processing_mode)
			templates = split_templates(values.get("templates"), cn_saved, mx_doc,
				corporations=corps, template_defaults=defaults, processing_mode=processing_mode)
			mappings = split_mappings(values.get("company_mappings"))
			# Reuse each source's validation and existing storage. CN save_once is
			# intentional: its outer retry must not discard an already-saved MX half.
			mx.save_settings(company_mappings=mappings["mx"], business_timezone=values.get("business_timezone"),
				enabled=enabled(values.get("sync_mode"), mx_doc.enabled),
				expected_config_version=values["versions"]["mx"], approval_templates=templates["mx"] or None,
				**({"management": True} if processing_mode == "approval" else {}))
			cn.save_once(enabled=enabled(values.get("sync_mode"), cn_saved["enabled"]),
				templates=templates["cn"], company_mappings=mappings["cn"],
				expected_config_version=values["versions"]["cn"], management_rules=values.get("management_rules"))
			if defaults is not None:
				save_template_defaults(defaults)
			return {"ok": True}
		except Exception as error:
			frappe.db.rollback()
			if isinstance(error, frappe.QueryDeadlockError) and attempt < 2:
				continue
			raise


def prepare_preview(template, template_defaults=None, processing_mode=None):
	# Preview is an explicit, bounded OA read, never part of opening the editor.
	cn_saved, mx_doc, corps = read()
	row = cn.parse(template)
	if not isinstance(row, dict):
		frappe.throw(_("Invalid approval template"))
	merged = {identity(item): item for item in merge_templates(cn_saved["templates"], mx._templates(mx_doc))}
	defaults = clean_defaults(template_defaults, corps, processing_mode=processing_mode) if template_defaults is not None else None
	row = prepare_template(row, defaults, merged, processing_mode)
	index = row.get("idx")
	validate_template(row, index if type(index) is int and 1 <= index <= 200 else 1, corps,
		require_finance=processing_mode != "approval")
	return row, corps


def inspect_template(template, business_timezone, template_defaults=None, processing_mode="approval"):
	row, corps = prepare_preview(template, template_defaults, processing_mode)
	from .expense_template_editor import inspect
	return inspect(row, business_timezone, corps, RULES[row["processing_rule"]])


def preview_template(template, business_timezone, template_defaults=None, processing_mode=None):
	row, corps = prepare_preview(template, template_defaults, processing_mode)
	if processing_mode == "approval":
		from .expense_sync_preview import preview
		return preview(row, business_timezone, corps, RULES[row["processing_rule"]])
	result = []
	for source in RULES.get(row.get("processing_rule"), ()):
		prefix = "application" if source == "cn" else "expense"
		item = {key: row.get(key) for key in ("corp_id", "process_code", "approval_type", "template_name")}
		item.update(enabled=0, field_mapping=row.get(prefix + "_fields") or "{}",
			classification_fields=row.get(prefix + "_classification") or "")
		if source == "cn":
			check = cn.preview_template(item)
			result.append({"rule": "application", "count": check["sample_count"], "issues": check["issues"]})
		else:
			item.update({key: row.get(key) or "" for key in ("finance_node_names", "finance_activity_ids")})
			check = mx.preview_template(item, business_timezone)
			result.append({"rule": "expense", "count": check["applications"],
				"issues": [_(mx.contract.ISSUES.get(key, key)) for key in check["issues"]]})
	if not result:
		frappe.throw(_("Invalid approval template"))
	return result
