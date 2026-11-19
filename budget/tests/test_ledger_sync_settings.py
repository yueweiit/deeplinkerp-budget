from contextlib import contextmanager
from types import SimpleNamespace

import pytest

from budget.services import ledger_sync_settings as settings
from budget.services import oa_budget_sync as sync
from budget.services import organization_mapping


@pytest.fixture
def configured(monkeypatch):
	values = {"budget_db_host": "db.example", "budget_db_user": "reader", "budget_db_password": "private-password",
		"budget_db_name": "dingtalk_oa", "budget_db_port": "5432"}
	read = lambda *keys, default="": next((values[key] for key in keys if values.get(key)), default)
	monkeypatch.setattr(settings, "_runtime_config_value", read)
	monkeypatch.setattr(settings, "shared_db_config", lambda: {"host":"db.example", "user":"reader",
		"password":"private-password", "database":"dingtalk_oa", "port":"5432"})
	monkeypatch.setattr(settings, "_", lambda text: text)
	monkeypatch.setattr(settings.frappe, "only_for", lambda roles: None)
	monkeypatch.setattr(settings.frappe, "throw", lambda message: (_ for _ in ()).throw(ValueError(message)))
	monkeypatch.setattr(organization_mapping, "schema_state", lambda: {"level_column": True, "ready": True})
	return {"ledger_corp_id":"corp-1", "ledger_connection_profile":"budget", "ledger_sync_enabled":1, "modified":"v1"}


@contextmanager
def acquired(*args, **kwargs):
	yield True


@pytest.fixture
def skip_log(monkeypatch):
	rows, commits = [], []
	monkeypatch.setattr(sync.frappe.utils, "now", lambda: "2026-11-19 10:00:00")
	monkeypatch.setattr(sync.frappe, "_", lambda text: text)
	monkeypatch.setattr(sync.frappe, "get_doc", lambda row: SimpleNamespace(
		insert=lambda **kw: rows.append(row)))
	monkeypatch.setattr(sync.frappe, "db", SimpleNamespace(commit=lambda: commits.append(True)))
	return rows, commits


def test_missing_corporation_blocks_before_any_connection(configured, monkeypatch):
	configured["ledger_corp_id"] = ""
	source = settings.resolve_source(configured)
	assert settings.blocked_reason(source) == "not_configured"
	with pytest.raises(ValueError, match="not configured"):
		settings.connection_kwargs(source)
	monkeypatch.setattr(settings, "current_source", lambda: source)
	assert settings.get_ledger_organization_options()["reason"] == "not_configured"


def test_shared_profile_requires_its_own_explicit_connection(configured):
	configured["ledger_connection_profile"] = "shared_oa"
	assert not settings.resolve_source(configured)["connection_configured"]


def test_public_settings_never_return_database_credentials(configured):
	source = settings.resolve_source(configured)
	public = settings.public_settings(source)
	assert source["configured"]
	assert all(secret not in str(public) for secret in ("private-password", "db.example", "reader"))
	assert "default_transaction_read_only=on" in settings.connection_kwargs(source)["options"]


def test_bound_source_changes_are_blocked(configured):
	configured["ledger_source_fingerprint"] = settings.resolve_source(configured)["fingerprint"]
	configured["ledger_corp_id"] = "corp-2"
	assert settings.blocked_reason(settings.resolve_source(configured)) == "source_changed"


@pytest.mark.parametrize("corp", ["corp' OR true", " ", "x" * 201])
def test_invalid_corporation_is_not_ready(configured, corp):
	configured["ledger_corp_id"] = corp
	assert not settings.resolve_source(configured)["configured"]


def test_page_refresh_never_enqueues_even_when_ready(configured, monkeypatch):
	monkeypatch.setattr(settings, "current_source", lambda: settings.resolve_source(configured))
	monkeypatch.setattr(sync.frappe, "db", SimpleNamespace(get_value=lambda *a, **k: None))
	monkeypatch.setattr(sync, "_enqueue_sync", lambda *a: pytest.fail("page load must not enqueue"))
	result = sync.ensure_ledger_fresh()
	assert result["stale"] and not result["queued"]
	assert result["blocked_by"] == "manual_sync_required"


@pytest.mark.parametrize("enabled,corp,reason", [(0,"corp-1","disabled"),(1,"","not_configured")])
def test_disabled_or_incomplete_settings_block_all_sync_paths(configured, monkeypatch, skip_log, enabled, corp, reason):
	configured.update(ledger_sync_enabled=enabled, ledger_corp_id=corp)
	monkeypatch.setattr(settings, "current_source", lambda: settings.resolve_source(configured))
	monkeypatch.setattr(sync, "_enqueue_sync", lambda *a: pytest.fail("must not enqueue"))
	monkeypatch.setattr(sync, "_force_full_due", lambda: pytest.fail("must not schedule"))
	monkeypatch.setattr(sync, "sync_lock", acquired)
	monkeypatch.setattr(sync, "_run_sync", lambda **kw: pytest.fail("must not synchronize"))
	assert sync.enqueue_oa_budget_sync() == {"queued":False,"blocked_by":reason}
	assert sync.scheduled_sync()["reason_code"] == reason
	assert skip_log == ([], [])  # Disabled cron ticks do not create log spam.
	assert sync.sync_from_oa(run_id="queued-attempt")["reason_code"] == reason
	rows, commits = skip_log
	assert len(rows) == len(commits) == 1
	assert rows[0]["status"] == "Skipped"
	assert rows[0]["reason_code"] == reason
	assert rows[0]["run_id"] == "queued-attempt"
	assert rows[0]["finished_at"] == rows[0]["started_at"]


def test_worker_rejects_settings_changed_after_enqueue(configured, monkeypatch, skip_log):
	monkeypatch.setattr(settings, "current_source", lambda: settings.resolve_source(configured))
	monkeypatch.setattr(sync, "sync_lock", acquired)
	monkeypatch.setattr(sync, "_run_sync", lambda **kw: pytest.fail("stale job must not import"))
	assert sync.sync_from_oa(expected_config_version="stale")["reason_code"] == "settings_changed"
	assert skip_log[0][0]["reason_code"] == "settings_changed"
	assert skip_log[1] == [True]


def test_sync_query_requires_corporation_and_template(configured, monkeypatch):
	source = settings.resolve_source(configured)
	calls = []
	monkeypatch.setattr(sync, "pg_fetch", lambda connection, sql, params, **kwargs: calls.append((sql,params)) or [])
	with settings.source_context(source):
		sync._fetch_instances()
		assert settings.current_source() is source
	assert calls[0][1] == ["corp-1", sync.BUDGET_PROCESS_CODE]
	assert "WHERE corp_id = %s" in calls[0][0]


class Doc(dict):
	def __getattr__(self, name): return self.get(name)
	def __setattr__(self, name, value): self[name] = value
	def get_doc_before_save(self): return self.get("old")
	def save(self): settings.validate_settings(self)


def test_save_only_persists_configuration(configured, monkeypatch):
	doc = Doc(configured, old=Doc(configured))
	version = settings.resolve_source(doc)["version"]
	calls = []
	monkeypatch.setattr(settings.frappe, "get_doc", lambda *args: doc)
	monkeypatch.setattr(settings.frappe, "db", SimpleNamespace(commit=lambda: calls.append("commit"), count=lambda *a:0))
	monkeypatch.setattr(settings, "sync_lock", acquired)
	monkeypatch.setattr(settings, "is_locked", lambda *args:True)
	monkeypatch.setattr(settings.frappe, "enqueue", lambda *a, **k:pytest.fail("save must not enqueue"))
	result = settings.save_sync_settings("corp-2", "budget", 0, version)
	assert result["corp_id"] == "corp-2" and not result["enabled"]
	assert calls == ["commit"]
	with pytest.raises(ValueError, match="changed"):
		settings.save_sync_settings("corp-3", "budget", 0, version)


@pytest.mark.parametrize("ready", [False, True])
@pytest.mark.parametrize("enabled", [0, 1])
def test_saved_and_read_settings_share_schema_readiness(configured, monkeypatch, ready, enabled):
	doc = Doc(configured, old=Doc(configured))
	version = settings.resolve_source(doc)["version"]
	commits = []
	monkeypatch.setattr(organization_mapping, "schema_state", lambda: {"level_column": ready, "ready": ready})
	monkeypatch.setattr(settings.frappe, "get_doc", lambda *args: doc)
	monkeypatch.setattr(settings.frappe, "db", SimpleNamespace(commit=lambda: commits.append(True), count=lambda *a: 0))
	monkeypatch.setattr(settings, "sync_lock", acquired)
	monkeypatch.setattr(settings.frappe, "enqueue", lambda *a, **kw: pytest.fail("save must not enqueue"))

	saved = settings.save_sync_settings("corp-1", "budget", enabled, version)
	loaded = settings.get_sync_settings()
	assert saved == {key: loaded[key] for key in saved}
	assert saved["organization_review_ready"] is ready
	assert saved["blocked_by"] == ("disabled" if not enabled else None if ready else "schema_update_required")
	assert commits == [True]


def test_existing_ledger_blocks_scope_switch(configured, monkeypatch):
	doc = Doc(configured, old=Doc(configured))
	doc.ledger_corp_id = "corp-2"
	monkeypatch.setattr(settings, "is_locked", lambda *args:False)
	monkeypatch.setattr(settings.frappe, "db", SimpleNamespace(count=lambda *a:1))
	with pytest.raises(ValueError, match="contains data"):
		settings.validate_settings(doc)


def test_native_form_cannot_overwrite_source_binding(configured):
	doc = Doc(configured, old=Doc(configured))
	doc.ledger_source_fingerprint = "invented"
	with pytest.raises(ValueError, match="managed by synchronization"):
		settings.validate_settings(doc)


def test_non_manager_save_rejected_before_read(configured, monkeypatch):
	monkeypatch.setattr(settings.frappe, "only_for", lambda *args: (_ for _ in ()).throw(PermissionError()))
	monkeypatch.setattr(settings.frappe, "get_doc", lambda *args:pytest.fail("not allowed to read settings"))
	with pytest.raises(PermissionError):
		settings.save_sync_settings("corp-1","budget",1,"version")


@pytest.mark.parametrize("job_status,result,expected", [("queued",None,"queued"),("started",None,"running"),
	("finished",{"verified":True},"success"),("failed",None,"failed"),
	("finished",{},"unknown"),
	("finished",{"skipped":True,"reason_code":"settings_changed"},"skipped")])
def test_sync_status_reports_actual_job_result(configured, monkeypatch, job_status, result, expected):
	from frappe.utils import background_jobs
	job = SimpleNamespace(get_status=lambda **kw:job_status, return_value=lambda:result)
	monkeypatch.setattr(background_jobs, "get_job", lambda *args:job)
	monkeypatch.setattr(settings, "current_source", lambda:settings.resolve_source(configured))
	monkeypatch.setattr(settings, "is_locked", lambda *args:False)
	monkeypatch.setattr(settings.frappe, "get_all", lambda *a, **k:[])
	monkeypatch.setattr(settings.frappe, "db", SimpleNamespace(count=lambda *args:3))
	state = settings.get_sync_state()
	assert state["state"] == expected
	assert state["ledger_count"] == 3


def test_newer_scheduled_log_wins_over_old_completed_job(configured, monkeypatch):
	from datetime import datetime
	from frappe.utils import background_jobs
	job = SimpleNamespace(get_status=lambda **kw:"finished", return_value=lambda:{"verified":True},
		ended_at=datetime(2026,11,18,1))
	monkeypatch.setattr(background_jobs, "get_job", lambda *args:job)
	monkeypatch.setattr(settings, "current_source", lambda:settings.resolve_source(configured))
	monkeypatch.setattr(settings, "is_locked", lambda *args:False)
	monkeypatch.setattr(settings.frappe.utils, "convert_utc_to_system_timezone", lambda value:value)
	monkeypatch.setattr(settings.frappe, "get_all", lambda *a, **kw:[{"status":"Failed", "started_at":"2026-11-19 01:00:00"}])
	monkeypatch.setattr(settings.frappe, "db", SimpleNamespace(count=lambda *a:3))
	assert settings.get_sync_state()["state"] == "failed"


@pytest.mark.parametrize("reason", ["settings_changed", "disabled", "not_configured", "source_changed", "running"])
def test_skipped_attempt_survives_rq_expiration(configured, monkeypatch, skip_log, reason):
	from frappe.utils import background_jobs

	result = sync._record_skipped("Manual", reason, "attempt-2")
	monkeypatch.setattr(background_jobs, "get_job", lambda *args: None)
	monkeypatch.setattr(settings, "current_source", lambda: settings.resolve_source(configured))
	monkeypatch.setattr(settings, "is_locked", lambda *args: False)
	monkeypatch.setattr(settings.frappe, "get_all", lambda *args, **kw: skip_log[0])
	monkeypatch.setattr(settings.frappe.db, "count", lambda *args: 3, raising=False)
	state = settings.get_sync_state()
	assert state["state"] == "skipped"
	assert state["reason"] == reason
	assert state["last"]["run_id"] == result["run_id"] == "attempt-2"


def test_contended_worker_records_skip_without_reading_source(configured, monkeypatch, skip_log):
	@contextmanager
	def occupied(*args):
		yield False

	monkeypatch.setattr(sync, "sync_lock", occupied)
	monkeypatch.setattr(settings, "current_source", lambda: pytest.fail("must not read source"))
	assert sync.sync_from_oa()["reason_code"] == "running"
	assert skip_log[0][0]["reason_code"] == "running"


def test_oa_reads_use_selected_connection_and_corporation_even_when_disabled(configured, monkeypatch):
	from budget.integrations import oa_factory, pg

	source = settings.resolve_source(configured)
	source.update(enabled=False, corp_id="selected-corp", profile="shared_oa")
	source["connection"].update(host="selected.example", database="selected-oa", sslmode="require")
	monkeypatch.setattr(settings, "current_source", lambda: source)
	monkeypatch.setattr(oa_factory, "data_source_mode", lambda: "local")
	calls = []
	monkeypatch.setattr(pg, "fetch", lambda conn, sql, params, **kw: calls.append((conn, sql, params)) or [])
	reader = oa_factory.get_oa_source()
	reader.list_departments()
	reader.list_approval_efficiency_rows(start_date="2026-11-01", end_date="2026-11-30")
	assert len(calls) == 3
	for connection, sql, params in calls:
		assert connection["host"] == "selected.example"
		assert connection["dbname"] == "selected-oa"
		assert connection["sslmode"] == "require"
		assert params[0] == "selected-corp"
		assert "corp_id = %s" in sql


@pytest.mark.parametrize("blocked", ["not_configured", "source_changed"])
def test_unsafe_oa_read_rejected_before_query(configured, monkeypatch, blocked):
	from budget.integrations import pg

	source = settings.resolve_source(configured)
	if blocked == "not_configured":
		source["configured"] = False
	else:
		source["bound_fingerprint"] = "another-source"
	monkeypatch.setattr(settings, "current_source", lambda: source)
	monkeypatch.setattr(pg, "fetch", lambda *a, **kw: pytest.fail("must not connect"))
	with pytest.raises(settings.LedgerReadSourceError):
		settings.get_ledger_oa_source()


@pytest.mark.parametrize("factory_name", ["get_expense_source", "get_budget_expense_source"])
@pytest.mark.parametrize("legacy_corp,legacy_host,allowed", [
	("corp-1", "db.example", True), ("", "db.example", False),
	("other-corp", "db.example", False), ("corp-1", "other.example", False),
])
def test_expense_factories_require_matching_legacy_binding(configured, monkeypatch, factory_name, legacy_corp, legacy_host, allowed):
	from budget.integrations import budget_factory, budget_expense_source, expense_source

	source = settings.resolve_source(configured)
	source["enabled"] = False  # Read access is independent from the sync switch.
	monkeypatch.setattr(settings, "current_source", lambda: source)
	monkeypatch.setattr(settings, "shared_db_config", lambda: {"host": legacy_host, "port": 5432})
	monkeypatch.setattr(settings, "_runtime_config_value", lambda *keys: legacy_corp)
	monkeypatch.setattr(budget_factory, "data_source_mode", lambda: "local")
	reader = object()
	def create():
		assert allowed, "must not instantiate an unsafe expense reader"
		return reader
	monkeypatch.setattr(expense_source, "ExpenseSource", create)
	monkeypatch.setattr(budget_expense_source, "BudgetExpenseSource", create)
	if allowed:
		assert getattr(budget_factory, factory_name)() is reader
	else:
		with pytest.raises(settings.LedgerReadSourceError, match="does not match"):
			getattr(budget_factory, factory_name)()


@pytest.mark.parametrize("event", ["after_commit", "after_rollback"])
def test_native_settings_mutex_lasts_until_transaction_completion(configured, monkeypatch, event):
	from budget.budget.doctype.budget_settings.budget_settings import BudgetSettings
	from frappe.model.document import Document

	callbacks = SimpleNamespace(after_commit=set(), after_rollback=set())
	monkeypatch.setattr(settings.frappe, "db", callbacks)
	monkeypatch.setattr(settings.frappe, "local", SimpleNamespace())
	held = []
	@contextmanager
	def mutex(*args):
		assert not held
		held.append(True)
		try:
			yield True
		finally:
			held.pop()

	def save(doc, *args, **kwargs):
		assert held, "mutex must precede Document database locks"
		return doc

	monkeypatch.setattr(settings, "sync_lock", mutex)
	monkeypatch.setattr(Document, "_save", save)
	monkeypatch.setattr(Document, "insert", save)
	doc = object.__new__(BudgetSettings)
	assert doc._save() is doc
	assert doc.insert() is doc  # Nested/native repeated saves reuse the transaction lock.
	assert held
	assert len(callbacks.after_commit) == len(callbacks.after_rollback) == 1
	for callback in getattr(callbacks, event):
		callback()
	assert not held
	assert settings.frappe.local.budget_settings_lock_release is None
	for callback in callbacks.after_commit | callbacks.after_rollback:
		callback()  # Completion callbacks are idempotent.


def test_native_save_rejected_before_document_write_when_worker_holds_mutex(configured, monkeypatch):
	from budget.budget.doctype.budget_settings.budget_settings import BudgetSettings
	from frappe.model.document import Document

	@contextmanager
	def occupied(*args):
		yield False
	monkeypatch.setattr(settings.frappe, "local", SimpleNamespace())
	monkeypatch.setattr(settings, "sync_lock", occupied)
	monkeypatch.setattr(Document, "_save", lambda *a, **kw: pytest.fail("must not write settings"))
	with pytest.raises(ValueError, match="running"):
		object.__new__(BudgetSettings)._save()
