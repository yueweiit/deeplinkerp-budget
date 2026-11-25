"""Unified controls preserve source authorization and independent transactions."""
from datetime import datetime
from types import SimpleNamespace

import frappe
import pytest

from budget.services import expense_sync as s
from deeplinkerp_branding.services import mexico_expenses as mx


@pytest.fixture
def runtime(monkeypatch):
	from frappe.utils import scheduler
	state = SimpleNamespace(cn={"enabled": True, "configured": True, "status": "success", "active": False,
		"snapshot_id": "snapshot", "last_success_at": "2026-11-24 10:00:00"},
		mx={"enabled": True, "configured": True, "can_manage": False, "active": False, "status": "success"})
	monkeypatch.setattr(s.approvals, "require_reader", lambda: None)
	monkeypatch.setattr(frappe, "flags", frappe._dict(mute_messages=False))
	monkeypatch.setattr(s.settings, "can_manage", lambda: False)
	monkeypatch.setattr(s.cn, "get_status", lambda: state.cn)
	monkeypatch.setattr(s.cn, "enabled", lambda: True)
	monkeypatch.setattr(mx, "get_sync_status", lambda: dict(state.mx))
	monkeypatch.setattr(scheduler, "is_scheduler_inactive", lambda **kw: True)
	monkeypatch.setattr(s, "_", lambda text: text)
	monkeypatch.setattr(s, "template_progress", lambda sources: [])
	return state


def test_status_preserves_settings_permissions_and_warns_about_disabled_scheduler(runtime):
	result = s.get_status()
	assert result["interval_minutes"] == 5 and not result["scheduler_enabled"]
	assert result["sources"]["cn"]["can_sync"]
	assert not result["sources"]["cn"]["can_manage"]
	assert not result["sources"]["mx"]["can_sync"]
	assert "templates" not in result["sources"]["cn"]


def test_unconfigured_cn_does_not_hide_mx_settings(runtime, monkeypatch):
	def unconfigured(): raise frappe.ValidationError("private source configuration")
	monkeypatch.setattr(s.cn, "get_status", unconfigured)
	monkeypatch.setattr(s.settings, "can_manage", lambda: True)
	runtime.mx["can_manage"] = True
	result = s.get_status()
	assert result["sources"]["cn"]["error_code"] == "configuration_required"
	assert not result["sources"]["cn"]["can_sync"]
	assert result["sources"]["cn"]["can_manage"]
	assert result["sources"]["mx"]["can_sync"]
	assert "private" not in str(result)
	assert not frappe.flags.mute_messages


def test_queries_and_commands_require_reader_before_accessing_source(runtime, monkeypatch):
	def denied(): raise frappe.PermissionError()
	monkeypatch.setattr(s.approvals, "require_reader", denied)
	for operation in (s.get_status, lambda: s.enqueue("cn"), lambda: s.enqueue("mx")):
		with pytest.raises(frappe.PermissionError): operation()


def test_each_enqueue_command_uses_its_existing_permission_and_transaction_boundary(runtime, monkeypatch):
	calls = []
	monkeypatch.setattr(s.cn, "request_sync", lambda: calls.append("cn") or {"status": "queued"})
	def denied_mx():
		calls.append("mx")
		raise frappe.PermissionError()
	monkeypatch.setattr(mx, "enqueue_sync", denied_mx)
	assert s.enqueue("cn")["status"] == "queued"
	assert calls == ["cn"]
	with pytest.raises(frappe.PermissionError): s.enqueue("mx")
	assert calls == ["cn", "mx"]


def test_mx_status_serializes_dates_without_exposing_templates_or_error_text(monkeypatch):
	from frappe.utils import background_jobs
	template = frappe._dict(corp_id="private-corp", process_code="private-code", enabled=1,
		last_sync_at=datetime(2026, 11, 24), cursor=None, last_error=None)
	settings = frappe._dict(enabled=1, templates=[template], last_sync_at=template.last_sync_at, config_version="v1")
	monkeypatch.setattr(mx, "_finance", lambda: None)
	monkeypatch.setattr(mx, "_settings", lambda: settings)
	monkeypatch.setattr(mx, "_can_manage_settings", lambda doc: False)
	monkeypatch.setattr(background_jobs, "get_job_status", lambda job: None)
	result = mx.get_sync_status()
	assert result["status"] == "success" and result["completed_templates"] == 1
	assert result["last_update_at"] == "2026-11-24 00:00:00"
	assert result["revision"] == mx.sync_revision(settings)
	assert "private-" not in str(result)
	previous_revision = result["revision"]
	template.last_error = "private upstream failure"
	result = mx.get_sync_status()
	assert result["status"] == "failed" and result["completed_templates"] == 0
	assert result["revision"] == mx.sync_revision(settings) != previous_revision
	assert "private" not in str(result)


def test_only_expense_schedules_change_to_five_minutes():
	from budget import hooks as budget_hooks
	from deeplinkerp_branding import hooks as branding_hooks
	assert budget_hooks.scheduler_events["cron"]["*/5 * * * *"] == ["budget.services.cn_approval_sync.scheduled_sync"]
	assert branding_hooks.scheduler_events["cron"]["*/5 * * * *"] == ["deeplinkerp_branding.services.mexico_expenses.scheduled_sync"]
	assert "deeplinkerp_branding.services.purchase_source_service.scheduled_sync" in branding_hooks.scheduler_events["cron"]["*/15 * * * *"]
