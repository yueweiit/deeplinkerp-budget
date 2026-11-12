"""数据库配置入口：站点只配一套连接，各数据源只可选覆盖库名。"""

from __future__ import annotations

import sys
import types

import pytest

from budget.integrations.budget_expense_source import approval_connection_kwargs
from budget.integrations.budget_ledger_source import BudgetLedgerSourceConfig
from budget.integrations.oa_approval_source import (
	OAApprovalSourceConfig,
	shared_database_name,
	shared_db_config,
)

# 所有参与解析的键：先清空，避免宿主环境/site_config 里的值影响断言
_ALL_KEYS = (
	"budget_db_host",
	"budget_db_port",
	"budget_db_name",
	"budget_db_user",
	"budget_db_password",
	"budget_oa_db_host",
	"budget_oa_db_port",
	"budget_oa_db_name",
	"budget_oa_db_user",
	"budget_oa_db_password",
	"dingtalk_oa_db_host",
	"dingtalk_oa_db_port",
	"dingtalk_oa_db_name",
	"dingtalk_oa_db_user",
	"dingtalk_oa_db_password",
	"oa_mapping_pg_host",
	"oa_mapping_pg_port",
	"oa_mapping_pg_database",
	"oa_mapping_pg_user",
	"oa_mapping_pg_password",
	"budget_ledger_db_name",
	"budget_system_db_name",
	"budget_approval_db_name",
	"dingtalk_approval_db_name",
)


def _prepare(monkeypatch: pytest.MonkeyPatch, env: dict[str, str]) -> None:
	for key in _ALL_KEYS:
		monkeypatch.delenv(key.upper(), raising=False)
	for key, value in env.items():
		monkeypatch.setenv(key.upper(), value)
	# frappe.conf 视为空，只测环境变量优先级
	fake = types.ModuleType("frappe")
	fake.conf = {}
	monkeypatch.setitem(sys.modules, "frappe", fake)


def test_shared_config_prefers_unified_keys(monkeypatch):
	_prepare(
		monkeypatch,
		{
			"budget_db_host": "shared.example",
			"budget_db_port": "6000",
			"budget_db_user": "reader",
			"budget_db_password": "secret",
			"budget_oa_db_host": "legacy.example",
		},
	)
	shared = shared_db_config()
	assert shared["host"] == "shared.example"
	assert shared["port"] == "6000"
	assert shared["user"] == "reader"
	assert shared["password"] == "secret"


def test_legacy_oa_keys_still_work(monkeypatch):
	_prepare(
		monkeypatch,
		{
			"budget_oa_db_host": "oa.example",
			"budget_oa_db_port": "6543",
			"budget_oa_db_user": "reader",
			"budget_oa_db_password": "secret",
		},
	)
	cfg = OAApprovalSourceConfig.from_site_config()
	assert (cfg.host, cfg.port, cfg.user, cfg.password) == (
		"oa.example",
		6543,
		"reader",
		"secret",
	)
	assert cfg.database == "dingtalk_oa"


def test_ledger_inherits_shared_connection(monkeypatch):
	_prepare(
		monkeypatch,
		{
			"budget_db_host": "shared.example",
			"budget_db_port": "6000",
			"budget_db_user": "reader",
			"budget_db_password": "secret",
			"budget_ledger_db_name": "budget_system",
		},
	)
	cfg = BudgetLedgerSourceConfig.from_site_config()
	assert (cfg.host, cfg.port, cfg.user, cfg.password) == (
		"shared.example",
		6000,
		"reader",
		"secret",
	)
	assert cfg.database == "budget_system"


def test_single_database_name_applies_to_every_source(monkeypatch):
	_prepare(monkeypatch, {"budget_db_host": "shared.example", "budget_db_name": "eims"})
	assert shared_db_config()["database"] == "eims"
	assert OAApprovalSourceConfig.from_site_config().database == "eims"
	assert BudgetLedgerSourceConfig.from_site_config().database == "eims"
	assert approval_connection_kwargs()["dbname"] == "eims"


def test_source_specific_name_wins_over_shared(monkeypatch):
	_prepare(
		monkeypatch,
		{
			"budget_db_name": "eims",
			"budget_ledger_db_name": "budget_system",
			"budget_approval_db_name": "dingtalk_approval",
		},
	)
	assert BudgetLedgerSourceConfig.from_site_config().database == "budget_system"
	assert approval_connection_kwargs()["dbname"] == "dingtalk_approval"
	assert OAApprovalSourceConfig.from_site_config().database == "eims"


def test_defaults_keep_current_databases(monkeypatch):
	_prepare(monkeypatch, {})
	assert OAApprovalSourceConfig.from_site_config().database == "dingtalk_oa"
	assert BudgetLedgerSourceConfig.from_site_config().database == "budget_system"
	assert approval_connection_kwargs()["dbname"] == "dingtalk_approval"


def test_shared_database_name_falls_back_to_default(monkeypatch):
	_prepare(monkeypatch, {})
	assert shared_database_name("budget_ledger_db_name", default="budget_system") == "budget_system"
