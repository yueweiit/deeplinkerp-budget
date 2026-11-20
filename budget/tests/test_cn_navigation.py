"""Optional CN layout must retain standalone budget installation support."""
import importlib.util
from pathlib import Path
from types import ModuleType
from unittest.mock import patch


def load_setup():
	spec = importlib.util.spec_from_file_location("cn_nav_setup", Path(__file__).resolve().parents[1] / "setup.py")
	module = importlib.util.module_from_spec(spec)
	with patch.dict("sys.modules", {"frappe": ModuleType("frappe")}):
		spec.loader.exec_module(module)
	return module


def test_optional_cn_pages_are_required_before_showing_new_navigation():
	setup = load_setup()
	assert setup.cn_navigation_items(lambda route: False) == []
	assert setup.cn_navigation_items(lambda route: route != "china-expense-review") == []
	items = setup.cn_navigation_items(lambda route: True)
	assert [item["link_to"] for item in items if item["type"] == "Link"] == [row[1] for row in setup.CN_PAGES]
	groups = [item for item in items if item["type"] == "Section Break"]
	assert [item["label"] for item in groups] == ["Budget and Expenditure", "Management Analysis", "Details and Review"]
	assert all(item["collapsible"] and item["keep_closed"] and item["indent"] for item in groups)
	assert [item["child"] for item in items if item["type"] == "Link"] == [1, 1, 1, 1, 1, 1]


def test_cn_boot_does_not_depend_on_the_old_budget_source_switch():
    from types import SimpleNamespace
    setup = load_setup()
    setup.frappe.get_installed_apps = lambda: ["budget", "deeplinkerp_branding"]
    setup.frappe.db = SimpleNamespace(exists=lambda *args: True)
    info = SimpleNamespace(page_info={"china-expense-reports": {"title": "Expenditure Analysis"},
        "china-personnel-report": {"title": "Personnel Analysis"}, "mexico-expenses": {"title": "Mexico Expenses"}})
    setup.boot_session(info)
    assert info.cn_budget_workbench is True
    assert info.page_info["china-expense-reports"]["title"] == "Expenditure Analysis"
    assert info.page_info["china-personnel-report"]["title"] == "Management Reports"
    assert info.page_info["mexico-expenses"]["title"] == "Mexico Expenses"
    assert "china-expense-details" not in info.page_info  # Do not add unauthorized Pages.
    setup.frappe.db.exists = lambda dt, name: name != "china-expense-review"
    info.page_info["china-expense-reports"]["title"] = "Expenditure Analysis"
    setup.boot_session(info)
    assert info.cn_budget_workbench is False
    assert info.page_info["china-expense-reports"]["title"] == "Expenditure Analysis"


def test_uninstall_reconciles_surviving_operating_navigation_after_sidebar_removal():
    from types import SimpleNamespace
    from unittest.mock import Mock
    setup = load_setup()
    events = []
    fake = setup.frappe
    fake.conf = SimpleNamespace(developer_mode=True)
    fake.db = SimpleNamespace(exists=lambda *args: True, commit=lambda: events.append("commit"))
    fake.get_installed_apps = lambda: ["budget", "deeplinkerp_branding"]
    fake.delete_doc = lambda dt, name, **kw: events.append(("delete", dt, name))
    fake.clear_cache = lambda: events.append("cache")
    fake.log_error = Mock()
    fake.get_traceback = lambda: "unexpected"
    navigation = ModuleType("deeplinkerp_branding.operating_navigation")
    navigation.ensure_operating_navigation = lambda: events.append("fallback")
    with patch.object(setup, "restore_desktop_icon_position"), patch.object(setup, "restore_budget_sidebar"), patch.dict("sys.modules", {navigation.__name__:navigation}):
        setup.restore_budget_nav()
    fake.log_error.assert_not_called()
    assert events.index(("delete", "Workspace Sidebar", "China Operating Expenses")) < events.index("fallback") < events.index("commit")
    assert fake.conf.developer_mode is True


def test_install_and_migrate_keep_standalone_defaults_until_branding_is_ready():
	import json
	from types import SimpleNamespace
	from unittest.mock import Mock
	setup = load_setup()
	app_root = Path(__file__).resolve().parents[1]
	fallback = ["Budget Ledger Entry", "budget-dashboard", "budget-config"]
	regional = [route for _, route, _, _ in setup.CN_PAGES]
	for branding, pages, expected in (
		(False, set(), fallback),
		(False, {*regional, "operating-expenses"}, fallback),
		(True, {"operating-expenses", *regional[:-1]}, ["operating-expenses", *fallback]),
		(True, set(regional), regional),
	):
		rows = []
		def import_metadata(path, **kwargs):
			metadata = json.loads(Path(path).read_text())
			if metadata["doctype"] == "Workspace Sidebar":
				rows[:] = [SimpleNamespace(**item, name=f"seed-{i}", idx=i) for i, item in enumerate(metadata["items"], 1)]
		def get_doc(value, *args):
			if isinstance(value, dict):
				return SimpleNamespace(db_insert=lambda: rows.append(SimpleNamespace(**value, name=f"new-{len(rows)}")))
			assert value == "Workspace Sidebar"
			return SimpleNamespace(name="China Operating Expenses", items=list(rows))
		def set_value(doctype, name, values, **kwargs):
			item = next(row for row in rows if row.name == name)
			for key, value in values.items():
				setattr(item, key, value)
		fake = setup.frappe
		fake.get_app_path = lambda app, folder, name: str(app_root / folder / name)
		fake.get_installed_apps = lambda: ["budget", "deeplinkerp_branding"] if branding else ["budget"]
		fake.get_doc, fake.clear_cache = get_doc, lambda: None
		fake.db = SimpleNamespace(
			exists=lambda dt, name: dt == "Page" and name in pages,
			delete=lambda dt, f: rows.__setitem__(slice(None), [row for row in rows if row.name != f["name"]]),
			set_value=set_value,
		)
		navigation = ModuleType("deeplinkerp_branding.operating_navigation")
		navigation.ensure_operating_navigation = Mock()
		with patch.object(setup, "ensure_module_def"), patch.object(setup, "restore_desktop_icon_position"), \
			patch.object(setup, "restore_budget_sidebar"), patch("frappe.modules.import_file.import_file_by_path", import_metadata), \
			patch.dict("sys.modules", {navigation.__name__: navigation}):
			for _ in range(2):
				setup.apply_budget_nav()
				assert [row.link_to for row in sorted(rows, key=lambda row: row.idx) if row.type == "Link"] == expected
		assert navigation.ensure_operating_navigation.call_count == (2 if expected == regional else 0)
