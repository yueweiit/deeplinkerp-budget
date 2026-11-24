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
	return {"interval_minutes": 5, "scheduler_enabled": not is_scheduler_inactive(verbose=False),
		"sources": {"cn": cn_state, "mx": mx_state}}


def enqueue(source):
	approvals.require_reader()
	# One source per command: CN owns its enqueue transaction and may retry it.
	# Combining both writes could roll back a previously reported MX enqueue.
	if source == "cn":
		return cn.request_sync()
	if source == "mx":
		from deeplinkerp_branding.services.mexico_expenses import enqueue_sync
		return enqueue_sync()
	frappe.throw(_("Invalid expense synchronization source."))
