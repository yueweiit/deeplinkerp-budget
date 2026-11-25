"""Unified controls over the existing independent OA synchronization jobs."""
import frappe
from frappe import _

from . import cn_approval_settings as settings
from . import cn_approval_sync as cn
from . import cn_approval_workbench as approvals


def cn_revision(state):
	"""Use the same snapshot identity for displayed reports and status polling."""
	return [state.get(key) for key in ("source_id", "snapshot_id", "settings_version", "allocation_revision")]


def get_status():
	from frappe.utils.scheduler import is_scheduler_inactive
	from deeplinkerp_branding.services import mexico_expenses as mx
	approvals.require_reader()
	can_manage = settings.can_manage()
	muted = frappe.flags.mute_messages
	try:
		# Configuration failures are represented in the status panel; a polling
		# request must not also queue repeated native error popups.
		frappe.flags.mute_messages = True
		state = cn.get_status()
		cn_state = {key: state.get(key) for key in ("status", "active", "enabled", "error_code",
			"completed_templates", "template_count", "allocation_rebuilding")}
		cn_state.update(configured=bool(state.get("configured")), last_update_at=state.get("last_success_at"),
			revision=cn_revision(state))
	except frappe.ValidationError:
		# An unconfigured source must not hide the other source's status/settings.
		cn_state = {"status": "unconfigured", "active": False, "configured": False,
			"enabled": cn.enabled(), "error_code": "configuration_required"}
	finally:
		frappe.flags.mute_messages = muted
	cn_state.update(can_manage=can_manage, can_sync=bool(cn_state["enabled"] and cn_state["configured"]))
	mx_state = mx.get_sync_status()
	mx_state["can_sync"] = bool(mx_state["can_manage"] and mx_state["configured"])
	sources = {"cn": cn_state, "mx": mx_state}
	progress = template_progress(sources)
	return {"interval_minutes": 5, "scheduler_enabled": not is_scheduler_inactive(verbose=False),
		"sources": sources, "templates": progress, "overall": overall(sources, progress),
		"can_manage": bool(can_manage and mx_state["can_manage"])}


def template_progress(sources):
	"""Expose workflow labels and opaque identities, never corporate/template IDs."""
	from deeplinkerp_branding.services import mexico_expenses as mx
	from .expense_sync_settings import identity
	cn_saved = settings.read_saved() or {}
	if not cn_saved and not sources["cn"].get("configured"):
		cn_rows = []
	elif not cn_saved:
		try:
			from ..integrations.cn_approval_source import configuration
			_url, rows = configuration()
			cn_rows = [dict(row, enabled=1) for row in rows]
		except frappe.ValidationError:
			cn_rows = []
	else:
		cn_rows = cn_saved["templates"]
	mx_rows = mx._templates(mx._settings())
	mx_states = {row["key"]: row for row in sources["mx"].get("templates", [])}
	grouped, cn_index = {}, 0
	for source, rows in (("cn", cn_rows), ("mx", mx_rows)):
		state = sources[source]
		for row in rows:
			key = mx.digest(identity(row))
			if key not in grouped:
				grouped[key] = {"key": key, "name": row.get("template_name") or _("Approval template {0}").format(len(grouped) + 1),
					"approval_type": row.get("approval_type"), "steps": []}
			status = "disabled"
			updated = None
			if row.get("enabled"):
				if source == "mx":
					item = mx_states.get(key, {})
					status, updated = item.get("status", state["status"]), item.get("last_update_at")
				else:
					if state.get("active"):
						completed = state.get("completed_templates") or 0
						status = "staged" if cn_index < completed else "running" if cn_index == completed and state["status"] == "running" else "queued"
					else:
						status = "success" if state["status"] == "success" else "failed" if state["status"] == "failed" else "pending"
					updated = state.get("last_update_at")
					cn_index += 1
			grouped[key]["steps"].append({"source": source, "status": status, "last_update_at": updated})
	for row in grouped.values():
		steps = [step for step in row["steps"] if step["status"] != "disabled"]
		states = {step["status"] for step in steps}
		row["status"] = next((status for status in ("running", "queued", "failed", "staged", "pending", "success") if status in states), "disabled")
		row["last_update_at"] = min((step["last_update_at"] for step in steps if step["last_update_at"]), default=None) if steps and all(step["last_update_at"] for step in steps) else None
	return list(grouped.values())


def overall(sources, templates):
	active = [row for row in templates if row["status"] != "disabled"]
	failed = sum(row["status"] == "failed" for row in active)
	completed = sum(row["status"] == "success" for row in active)
	busy = any(source.get("active") or source.get("allocation_rebuilding") for source in sources.values())
	status = "running" if busy else "partial_failure" if failed and completed else "failed" if failed or any(s["status"] == "failed" for s in sources.values()) else "success" if active and completed == len(active) else "pending" if active else "unconfigured"
	# MX timestamps advance per page. They only describe a complete update
	# after every enabled template has finished and local publication is idle.
	complete_times = [row["last_update_at"] for row in active if row["last_update_at"]]
	return {"status": status, "completed": completed, "total": len(active), "failed": failed,
		"last_update_at": min(complete_times) if status == "success" and len(complete_times) == len(active) else None}


def enqueue(source, retry_failed=False):
	approvals.require_reader()
	if retry_failed not in (False, True, 0, 1, "0", "1"):
		frappe.throw(_("Invalid synchronization setting"))
	retry_failed = bool(int(retry_failed))
	# One source per command: CN owns its enqueue transaction and may retry it.
	# Combining both writes could roll back a previously reported MX enqueue.
	if source == "cn":
		return cn.request_sync()
	if source == "mx":
		from deeplinkerp_branding.services.mexico_expenses import enqueue_sync
		return enqueue_sync(retry_failed=retry_failed) if retry_failed else enqueue_sync()
	frappe.throw(_("Invalid expense synchronization source."))
