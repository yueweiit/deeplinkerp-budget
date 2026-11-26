"""Retired sidebar projection must preserve native desktop link validity."""
from copy import deepcopy
import json
from pathlib import Path

import frappe
import pytest

from budget import expense_navigation as navigation


def fixture_boot():
	old = {"type": "Link", "link_type": "Page", "link_to": "mexico-expenses", "child": 1}
	keep = {"type": "Link", "link_type": "DocType", "link_to": "Account", "child": 1}
	group = {"type": "Section Break", "label": "Expenses"}
	return {
		"page_info": dict.fromkeys((*navigation.PAGES, *navigation.LEGACY_ROUTES), {}),
		"workspace_sidebar_item": {
			"expense management": {"items": [{"type": "Link", "link_type": "Page", "link_to": "expense-management"}]},
			"expense shortcuts": {"items": [group, old]},
			"mixed expenses": {"items": [group, old, keep]},
			"my workspaces": {"items": [old]},
			"already empty": {"items": []},
		},
		"desktop_icons": [
			{"name": "Expense Management", "label": "Expense Management", "link_type": "Workspace Sidebar"},
			{"name": "shortcut", "label": "Expense Shortcuts", "link_type": "Workspace Sidebar"},
			{"name": "target-only", "label": "Renamed", "link_type": "Workspace Sidebar", "link_to": "Expense Shortcuts"},
			{"name": "sidebar-only", "label": "Alternate", "link_type": "Workspace Sidebar", "sidebar": "Expense Shortcuts"},
			{"name": "Mixed", "label": "Mixed Expenses", "link_type": "Workspace Sidebar"},
			{"name": "Personal", "label": "My Workspaces", "link_type": "Workspace Sidebar"},
			{"name": "empty", "label": "Already Empty", "link_type": "Workspace Sidebar"},
			{"name": "external", "label": "Expense Shortcuts", "link_type": "External"},
			{"name": "folder", "label": "Expense Shortcuts", "link_type": "Workspace Sidebar", "icon_type": "Folder"},
		],
	}


def test_only_newly_empty_sidebars_and_their_icons_are_retired(monkeypatch):
	monkeypatch.setattr(frappe, "get_installed_apps", lambda: ["budget", "deeplinkerp_branding"])
	boot = fixture_boot()
	original = deepcopy(boot)
	original_sidebars = boot["workspace_sidebar_item"]
	navigation.boot(boot)
	assert "expense shortcuts" not in boot["workspace_sidebar_item"]
	assert "expense shortcuts" in boot["expense_retired_sidebars"]
	assert boot["workspace_sidebar_item"]["my workspaces"]["items"] == []
	assert "my workspaces" not in boot["expense_retired_sidebars"]
	assert "already empty" not in boot["expense_retired_sidebars"]
	assert [item.get("link_to") for item in boot["workspace_sidebar_item"]["mixed expenses"]["items"]] == [None, "Account"]
	assert [icon["name"] for icon in boot["desktop_icons"]] == ["Expense Management", "Mixed", "Personal", "empty", "external", "folder"]
	assert original_sidebars == original["workspace_sidebar_item"]
	first = deepcopy(boot)
	navigation.boot(boot)
	assert boot == first


@pytest.mark.parametrize("unavailable", ["app", "page"])
def test_fallback_navigation_does_not_prune_custom_sidebars_or_icons(monkeypatch, unavailable):
	monkeypatch.setattr(frappe, "get_installed_apps", lambda: ["budget"] if unavailable == "app" else ["budget", "deeplinkerp_branding"])
	boot = fixture_boot()
	if unavailable == "page":
		boot["page_info"].pop("expense-review")
	original = deepcopy(boot)
	navigation.boot(boot)
	assert boot["workspace_sidebar_item"] == original["workspace_sidebar_item"]
	assert boot["desktop_icons"] == [icon for icon in original["desktop_icons"] if icon["name"] != "Expense Management"]
	assert boot["expense_retired_sidebars"] == []


def journal_boot():
	boot = fixture_boot()
	boot["page_info"][navigation.JOURNAL_ROUTE] = {"title": navigation.JOURNAL_DOCTYPE}
	boot["user"] = {"can_read": [navigation.JOURNAL_DOCTYPE]}
	boot["workspace_sidebar_item"]["expense management"]["items"] = [
		{"type": "Link", "link_type": "Page", "link_to": route} for route in ("expense-details", "expense-review")]
	return boot


@pytest.mark.parametrize("label", ["Cash Journal", "日记账", "Libro de caja"])
def test_authorized_journal_is_localized_without_mutating_shared_metadata(monkeypatch, label):
	monkeypatch.setattr(frappe, "get_installed_apps", lambda: ["budget", "deeplinkerp_branding"])
	monkeypatch.setattr(frappe, "_", lambda text: label if text == navigation.JOURNAL_DOCTYPE else text)
	boot = journal_boot()
	sidebars = boot["workspace_sidebar_item"]
	original = deepcopy(sidebars)
	navigation.boot(boot)
	assert boot["unified_expense_management"] and boot["expense_cash_journal"]
	items = boot["workspace_sidebar_item"]["expense management"]["items"]
	assert [item["link_to"] for item in items] == ["expense-details", navigation.JOURNAL_ROUTE, "expense-review"]
	assert items[1]["label"] == label
	assert sidebars == original
	first = deepcopy(boot)
	navigation.boot(boot)
	assert boot == first


@pytest.mark.parametrize("missing", ["journal_page", "doctype_permission", "unified_page", "branding"])
def test_journal_is_optional_and_requires_both_page_and_doctype_permissions(monkeypatch, missing):
	monkeypatch.setattr(frappe, "get_installed_apps", lambda: ["budget"] if missing == "branding" else ["budget", "deeplinkerp_branding"])
	boot = journal_boot()
	boot["workspace_sidebar_item"]["expense management"]["items"].insert(1,
		{"type": "Link", "link_type": "Page", "link_to": navigation.JOURNAL_ROUTE})
	if missing == "journal_page":
		boot["page_info"].pop(navigation.JOURNAL_ROUTE)
	elif missing == "doctype_permission":
		boot["user"]["can_read"] = []
	elif missing == "unified_page":
		boot["page_info"].pop("expense-review")
	navigation.boot(boot)
	assert boot["expense_cash_journal"] is False
	if missing in ("journal_page", "doctype_permission"):
		assert boot["unified_expense_management"] is True
		assert all(item.get("link_to") != navigation.JOURNAL_ROUTE
			for item in boot["workspace_sidebar_item"]["expense management"]["items"])
	else:
		assert boot["unified_expense_management"] is False


def test_journal_seed_and_runtime_entry_agree_and_preserve_existing_custom_link(monkeypatch):
	monkeypatch.setattr(frappe, "_", lambda text: text)
	seed = json.loads((Path(__file__).parents[1] / "workspace_sidebar/expense_management.json").read_text())
	items = seed["items"]
	assert items == navigation.journal_sidebar_items(items, True)
	without_journal = [item for item in items if item.get("link_to") != navigation.JOURNAL_ROUTE]
	assert navigation.journal_sidebar_items(without_journal, True) == items
	custom = {"type": "Link", "link_type": "Page", "link_to": navigation.JOURNAL_ROUTE, "label": "My Journal", "filters": "mine"}
	assert navigation.journal_sidebar_items([custom], True) == [custom]
	assert navigation.JOURNAL_ROUTE not in navigation.LEGACY_ROUTES
