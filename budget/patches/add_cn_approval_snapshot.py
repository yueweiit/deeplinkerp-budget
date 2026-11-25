"""Install local CN storage and retain an existing complete v3 cache if available."""
import frappe


def execute():
	frappe.reload_doc("budget", "doctype", "cn_approval_snapshot")
	if "deeplinkerp_branding" not in frappe.get_installed_apps():
		return
	from budget.integrations import cn_approval_source as source, cn_approval_store as store
	from budget.services.cn_approval_sync import identity
	try:
		url, templates = source.configuration()
	except (frappe.ValidationError, ImportError):
		return
	cached = frappe.cache.get_value(source.cache_key(url, templates), use_local_cache=False)
	if not isinstance(cached, dict) or not isinstance(cached.get("rows"), list) or not cached.get("snapshot_id") or not cached.get("fetched_at"):
		return
	key = identity(url, templates)
	record = store.ensure(key)
	if not record.get("snapshot_id") and record.get("sync_status") == "idle":
		store.update(key, {"snapshot_json": store.prepare_snapshot(cached),
			"snapshot_id": cached["snapshot_id"], "last_success_at": cached["fetched_at"],
			"sync_status": "success", "template_count": len(templates), "completed_templates": len(templates)})
