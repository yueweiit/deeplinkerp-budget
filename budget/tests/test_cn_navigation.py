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
	assert [item["label"] for item in items if item["type"] == "Section Break"] == ["Expense Reports", "Budget Management"]
	assert all(item["collapsible"] and item["keep_closed"] for item in items if item["type"] == "Section Break")
	assert [item["child"] for item in items if item["type"] == "Link"] == [0, 0, 1, 1, 0]
