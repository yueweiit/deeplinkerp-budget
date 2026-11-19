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
	assert setup.cn_navigation_items(lambda route: route != "china-personnel-report") == []
	items = setup.cn_navigation_items(lambda route: True)
	assert [item["link_to"] for item in items if item["type"] == "Link"] == [row[1] for row in setup.CN_PAGES]
	assert [item["label"] for item in items if item["type"] == "Section Break"] == ["Expense Reports"]
	assert all(item["collapsible"] and item["keep_closed"] for item in items if item["type"] == "Section Break")
	assert [item["child"] for item in items if item["type"] == "Link"] == [0, 0, 1, 1, 1, 0]


def test_cn_boot_does_not_depend_on_the_old_budget_source_switch():
    from types import SimpleNamespace
    setup = load_setup()
    setup.frappe.get_installed_apps = lambda: ["budget", "deeplinkerp_branding"]
    setup.frappe.db = SimpleNamespace(exists=lambda *args: True)
    info = SimpleNamespace()
    setup.boot_session(info)
    assert info.cn_budget_workbench is True
    setup.frappe.db.exists = lambda dt, name: name != "china-personnel-report"
    setup.boot_session(info)
    assert info.cn_budget_workbench is False


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
