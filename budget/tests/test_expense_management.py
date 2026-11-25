"""Unified totals, ownership, dates, budget absence and dual-currency failures."""
from copy import deepcopy
from decimal import Decimal

import frappe
import pytest

from budget.services import expense_management as s
from budget.services import cn_approval_management_rules as rules
from deeplinkerp_branding.services import regional_expense_automatic as automatic
from deeplinkerp_branding.services import mexico_expenses as mx
from budget.integrations import cn_approval_history
from budget import expense_navigation as navigation

CONVERTER = s.converter


def line(key="one", **values):
	row = dict(key=key, source_key=key, company="A", number=key, month="2026-11",
		application_date="2026-11-10", kind="operating", status="approved", amount="100.00",
		currency="CNY", issues=[], management_category="service", budget_state="")
	return s.normalize({**row, **values})


def rate(currency, day):
	assert day.endswith("-01")
	return {"CNY": Decimal(1), "MXN": Decimal("0.4"), "USD": Decimal("7")}[currency]


@pytest.fixture(autouse=True)
def runtime(monkeypatch):
	monkeypatch.setattr(s, "_", lambda value: value)
	monkeypatch.setattr(s.regional, "_", lambda value: value)
	monkeypatch.setattr(frappe.utils, "today", lambda: "2026-11-24")
	monkeypatch.setattr(frappe, "get_system_settings", lambda key: {"date_format": "yyyy-mm-dd", "time_format": "HH:mm:ss"}.get(key))
	monkeypatch.setattr(frappe, "throw", lambda message, *args: (_ for _ in ()).throw(ValueError(message)))


def dataset(monkeypatch, rows, companies=("A", "B", "Empty"), complete=True):
	context = {"companies": set(companies), "manager": True}
	meta = {"complete": complete, "has_data": True, "updated_at": {"cn": "", "mx": ""}, "missing_history": 0}
	monkeypatch.setattr(s, "load", lambda: (deepcopy(rows), context, rules.defaults(), meta))
	monkeypatch.setattr(s, "converter", lambda: CONVERTER(rate))
	return context


def test_dual_currency_converts_originals_not_rounded_primary():
	rows = s.converter(rate)([line(currency="USD", amount="1.11")])
	assert rows[0]["amounts"] == {"CNY": "7.77", "MXN": "19.43"}
	assert rows[0]["rates"] == {"CNY": "7.000000000000", "MXN": "17.500000000000"}


def test_rounding_reconciles_even_for_company_subset():
	original = line(amount="0.34", allocation_amounts=["0.33", "0.33", "0.34"],
		allocation_index=2, header_amount="1.00")
	result = s.converter(lambda currency, day: Decimal(10))([original])[0]
	assert result["amounts"]["MXN"] == "0.04"
	assert result["amounts"]["CNY"] == "0.34"


def test_missing_rate_keeps_original_currency_and_never_presents_partial_total():
	calls=[]
	def missing(currency, day):
		calls.append((currency, day))
		raise s.fx.RateUnavailable()
	rows=s.converter(missing)([line(), line("mx", currency="MXN"), line("cn2")])
	assert rows[0]["amounts"] == {"CNY":"100.00", "MXN":None}
	assert rows[1]["amounts"] == {"CNY":None, "MXN":"100.00"}
	assert s.summarize(rows)["amounts"] == {"CNY":None, "MXN":None}
	assert len(calls) == 1


def test_unexpected_fx_error_is_not_suppressed():
	def fail(*args): raise RuntimeError("transport programming error")
	with pytest.raises(RuntimeError): s.converter(fail)([line()])


def test_approval_pending_budget_and_invalid_amount_do_not_inflate_spending():
	rows=s.converter(rate)([line(), line("pending", status="pending"), line("budget", kind="budget"),
		line("bad", issues=["amount"], amount=None), line("rejected", status="rejected")])
	result=s.summarize(rows)
	assert result["amounts"] == {"CNY":"100.00", "MXN":"250.00"}
	assert (result["count"],result["pending"],result["excluded"],result["review"]) == (1,1,1,1)


def test_finance_date_issue_does_not_block_application_month_but_amount_pending_does():
	assert line(original_issues=["period"])["eligible"]
	assert not line(original_issues=["amount_pending"])["eligible"]
	assert not line(issues=["detail_mismatch"])["eligible"]


@pytest.mark.parametrize("values", [{"country":"Mexico"}, {"month":"2026-13"}, {"month":"2026-12"},
	{"month":"1899-12"}, {"view":"other"}, {"category":"invented"}, []])
def test_invalid_or_unimplemented_filters_rejected(values):
	with pytest.raises(ValueError): s.filters(values)


def test_authorization_before_aggregates_details_and_export(monkeypatch):
	dataset(monkeypatch,[line()],companies=("A",))
	for method in (s.query, s.export_rows):
		with pytest.raises(ValueError,match="permission"): method({"company":"B"})
	with pytest.raises(ValueError,match="permitted"): s.detail("forbidden")


def test_company_ranking_totals_drilldown_and_export_reconcile(monkeypatch):
	dataset(monkeypatch,[line(),line("b",company="B",currency="MXN",amount="50.00"),line("budget",kind="budget")])
	result=s.query()
	assert result["summary"]["amounts"] == {"CNY":"120.00","MXN":"300.00"}
	assert [row["company"] for row in result["company_rows"]] == ["A","B","Empty"]
	detail=s.query({"company":"B","view":"details"})
	exported,_,_=s.export_rows({"company":"B"})
	assert exported == detail["rows"]
	assert detail["rows"][0]["amounts"] == {"CNY":"20.00","MXN":"50.00"}
	assert "source_key" not in detail["rows"][0]
	assert "allocation_amounts" not in detail["rows"][0]


def test_card_drilldown_and_overview_export_include_only_verified_approvals(monkeypatch):
	dataset(monkeypatch,[line(),line("pending",status="pending"),line("bad",issues=["amount"])])
	detail=s.query({"view":"details","measure":"approved"})
	exported,_,_=s.export_rows({"view":"overview"})
	assert [row["key"] for row in detail["rows"]] == ["one"]
	assert exported == detail["rows"]
	assert s.query({"view":"details"})["total"] == 3
	with pytest.raises(ValueError,match="permitted"):
		s.detail("pending",{"view":"details","measure":"approved"})


def test_missing_budget_never_zero_and_other_company_budget_not_borrowed(monkeypatch):
	dataset(monkeypatch,[line(),line("budget",kind="budget",company="B",budget_state="effective",amount="1000.00")])
	rows={row["company"]:row["metrics"] for row in s.query({"view":"budget"})["budgets"]}
	assert rows["A"]["CNY"]["budget"] is None
	assert rows["A"]["CNY"]["execution_rate"] is None
	assert rows["B"]["CNY"]["budget"] == "1000.00"
	assert rows["Empty"]["MXN"]["budget"] is None


def test_incomplete_source_cannot_present_a_budget_execution_ratio(monkeypatch):
	dataset(monkeypatch,[line(),line("budget",kind="budget",budget_state="effective",amount="1000.00")],complete=False)
	result=s.query({"view":"budget","company":"A"})["budgets"][0]["metrics"]["CNY"]
	assert result["budget"] == "1000.00"
	assert result["execution_rate"] is None and result["balance"] is None


@pytest.mark.parametrize("spent", ["20.00", "120.00"])
def test_unclassified_budget_keeps_amounts_without_comparisons_or_alerts(monkeypatch, spent):
	from budget.api import expenses as api
	dataset(monkeypatch, [
		line("budget", kind="budget", budget_state="effective", management_category="",
			amount="100.00", issues=["management_category"]),
		line("expense", management_category="", amount=spent, issues=["management_category"]),
	], companies=("A",))
	filters = {"view": "budget", "category": "unclassified"}
	result = s.query(filters)["budgets"][0]["metrics"]
	for metrics in result.values():
		assert metrics["budget"] is not None and metrics["approved"] is not None
		assert metrics["balance"] is metrics["forecast_balance"] is metrics["execution_rate"] is None
		assert metrics["flags"] == ["incomplete"]
	assert result["CNY"]["budget"] == "100.00"
	assert result["CNY"]["approved"] == spent
	monkeypatch.setattr(api, "_", lambda value: value)
	monkeypatch.setattr(api, "workbook", lambda data, filename: data)
	exported = api.export_data(filters)[-1]
	assert exported[1] == 100 and exported[3] == Decimal(spent)
	assert exported[5:] == [None, None, None]


def test_export_dual_currency_and_formula_strings_remain_literal(monkeypatch):
	import base64
	from io import BytesIO
	from openpyxl import load_workbook
	from budget.api import expenses as api
	monkeypatch.setattr(api,"_",lambda value:value)
	dataset(monkeypatch,[line("=TEST()")])
	result=api.export_data({"view":"details"})
	sheet=load_workbook(BytesIO(base64.b64decode(result["content"]))).active
	rows=list(sheet.values)
	header=next(row for row in rows if row[0]=="Approval number")
	row=rows[-1]
	assert row[0] == "'=TEST()"
	assert row[header.index("Amount in CNY")] == 100
	assert row[header.index("Amount in MXN")] == 250
	assert row[header.index("Date source")] == "Application date"


def test_budget_export_is_company_matrix_and_preserves_missing_budget(monkeypatch):
	import base64
	from io import BytesIO
	from openpyxl import load_workbook
	from budget.api import expenses as api
	monkeypatch.setattr(api,"_",lambda value:value)
	dataset(monkeypatch,[line()])
	result=api.export_data({"view":"budget","company":"A"})
	sheet=load_workbook(BytesIO(base64.b64decode(result["content"]))).active
	row=list(sheet.values)[-1]
	assert row[:7] == ("A",None,None,100,250,None,None)


def test_undated_rows_stay_out_of_month_totals_but_are_reviewable(monkeypatch):
	dataset(monkeypatch,[line(),line("undated",month=None,application_date=None)])
	assert s.query()["summary"]["count"] == 1
	review=s.query({"view":"review"})
	assert review["undated"] == 1
	assert [row["key"] for row in review["rows"]] == ["undated"]
	assert s.detail("undated",{"view":"review"})["row"]["amounts"]["CNY"] is None


def test_comparison_same_elapsed_days_and_complete_calendar_month(monkeypatch):
	dataset(monkeypatch,[line(),line("previous",month="2026-10",application_date="2026-10-10",amount="50.00"),
		line("late",month="2026-10",application_date="2026-10-25",amount="200.00")])
	assert s.query()["comparison"]["percent"] == "100.0"
	dataset(monkeypatch,[line(month="2026-02",application_date="2026-02-28"),
		line("jan",month="2026-01",application_date="2026-01-31")])
	result=s.query({"month":"2026-02"})["comparison"]
	assert result["percent"] == "0.0"
	assert result["window"]["previous_to"] == "2026-01-31"


def test_incomplete_sources_disable_comparison(monkeypatch):
	dataset(monkeypatch,[line(),line("old",month="2026-10",application_date="2026-10-01")],complete=False)
	assert s.query()["comparison"]["percent"] is None


def test_combined_loader_deduplicates_native_cn_snapshot(monkeypatch):
	native={"kind":"operating","corp_id":"corp","process_instance_id":"same"}
	budget={"kind":"budget","corp_id":"corp","process_instance_id":"budget"}
	monkeypatch.setattr(s.approvals,"require_reader",lambda:None)
	monkeypatch.setattr(automatic,"ensure_rebuild",lambda:False)
	monkeypatch.setattr(automatic,"legacy_manual_identities",lambda:set())
	monkeypatch.setattr(s.regional,"installed",lambda:True)
	monkeypatch.setattr(s.approvals,"access_context",lambda:{"companies":{"A","B"}})
	sync = dict(source_id="source", snapshot_id="snapshot", settings_version="settings", allocation_revision="allocation")
	monkeypatch.setattr(cn_approval_history,"read_snapshot",lambda:{"rows":[native,budget],"has_snapshot":True,"sync":sync})
	monkeypatch.setattr(s.settings,"management_rules",rules.defaults)
	monkeypatch.setattr(s.regional,"management_projection",lambda *a,**k:({("corp","same")},[line("a"),line("b",company="B")]))
	def analytical(rows,*args):
		assert rows == [budget]
		return [line("budget",kind="budget")]
	monkeypatch.setattr(s.management,"analytical_lines",analytical)
	mx_settings = frappe._dict(config_version="mx-settings")
	monkeypatch.setattr(mx,"_settings",lambda:mx_settings)
	monkeypatch.setattr(mx,"_templates",lambda settings:[])
	rows,_,_,metadata=s.load()
	assert [row["key"] for row in rows] == ["budget","a","b"]
	assert metadata["sync_revision"] == {"cn": ["source", "snapshot", "settings", "allocation"],
		"mx": mx.sync_revision(mx_settings)}


def legacy_dataset(monkeypatch, *, snapshot=True, companies=("A", "B")):
	"""Exercise the real loader, projection, filters and exports with local records."""
	source_key = s.regional.digest(["corp", "legacy"])
	source = frappe._dict(name=source_key, corp_id="corp", process_instance_id="legacy",
		raw_json="{}", source_version="v1", request_date="2026-11-10")
	base = dict(source=source_key, source_version="v1", company="A", regional_company="",
		approval_no="NO-1", approval_status="approved", currency="CNY", description="Salary",
		approval_type="operating", review_state="confirmed", issues="[]",
		classification_path='[{"label":"Salaries"}]', is_active=1)
	stored = [frappe._dict(base, name="manual", amount="30.00", company_basis="manual"),
		frappe._dict(base, name="mapped", amount="70.00", company_basis="mapping")]
	def get_list(doctype, filters, **kwargs):
		assert doctype == mx.LINE
		def matches(row, key, value):
			return row.get(key) in value[1] if isinstance(value, list) else row.get(key) == value
		return [row for row in stored if all(matches(row, key, value) for key, value in filters.items())]
	def get_all(doctype, filters, **kwargs):
		assert doctype == mx.SOURCE and source_key in filters["name"][1]
		return [source]
	monkeypatch.setattr(frappe, "get_list", get_list)
	monkeypatch.setattr(frappe, "get_all", get_all)
	monkeypatch.setattr(s.approvals, "require_reader", lambda: None)
	monkeypatch.setattr(automatic, "ensure_rebuild", lambda: False)
	monkeypatch.setattr(automatic, "legacy_manual_identities", lambda: {("corp", "legacy")})
	monkeypatch.setattr(s.regional, "installed", lambda: True)
	monkeypatch.setattr(s.approvals, "access_context", lambda: {"companies": set(companies), "manager": True})
	monkeypatch.setattr(cn_approval_history, "read_snapshot", lambda: {
		"rows": [{"kind": "operating", "corp_id": "corp", "process_instance_id": "legacy"}] if snapshot else [],
		"has_snapshot": snapshot})
	monkeypatch.setattr(s.settings, "management_rules", rules.defaults)
	monkeypatch.setattr(s.regional, "management_projection", lambda *a, **k: (set(), []))
	def analytical(rows, *args):
		assert rows == []  # The native duplicate cannot replace an old manual decision.
		return []
	monkeypatch.setattr(s.management, "analytical_lines", analytical)
	monkeypatch.setattr(mx, "_settings", lambda: frappe._dict(config_version="mx-settings"))
	monkeypatch.setattr(mx, "_templates", lambda settings: [{"enabled": 1, "last_sync_at": "2026-11-24"}])
	monkeypatch.setattr(s, "converter", lambda: CONVERTER(rate))
	return stored


@pytest.mark.parametrize("mapped_company", ["A", "B"])
def test_legacy_manual_approval_keeps_mapped_siblings_once_in_totals_details_and_export(monkeypatch, mapped_company):
	stored = legacy_dataset(monkeypatch)
	stored[1].company = mapped_company
	stored.extend([frappe._dict(stored[1], name="inactive", is_active=0),
		frappe._dict(stored[1], name="excluded", review_state="excluded"),
		frappe._dict(stored[1], name="unrelated", source="another-approval")])
	result = s.query()
	assert result["summary"]["amounts"] == {"CNY": "100.00", "MXN": "250.00"}
	assert result["summary"]["applications"] == 1
	details = s.query({"view": "details"})["rows"]
	exported, _, _ = s.export_rows({"view": "overview"})
	assert {row["key"] for row in details} == {"manual", "mapped"}
	assert details == exported
	assert s.detail("mapped")["row"]["amounts"]["CNY"] == "70.00"
	if mapped_company == "B":
		assert s.query({"company": "B"})["summary"]["amounts"]["CNY"] == "70.00"


def test_legacy_category_identity_is_available_to_mapping_discovery_but_not_public_rows(monkeypatch):
	stored = legacy_dataset(monkeypatch)
	for row in stored:
		row.classification_path = '[{"label":"New source category"}]'
	rows, _, _, _ = s.load()
	assert all(row["source_path"] == ["New source category"] for row in rows)
	assert all(not row["management_category"] for row in rows)
	assert all("source_path" not in row for row in s.query({"view": "details"})["rows"])


def test_legacy_siblings_keep_company_and_regional_permissions(monkeypatch):
	stored = legacy_dataset(monkeypatch, companies=("A",))
	stored[0].company = "B"  # The exception is hidden, but its mapped sibling is visible.
	stored.append(frappe._dict(stored[1], name="hidden-region", regional_company="B"))
	result = s.query({"view": "details"})
	assert [row["key"] for row in result["rows"]] == ["mapped"]
	assert result["summary"]["amounts"]["CNY"] == "70.00"
	with pytest.raises(ValueError, match="permitted"):
		s.detail("manual")


def test_shared_allocation_suppresses_all_legacy_siblings(monkeypatch):
	legacy_dataset(monkeypatch)
	monkeypatch.setattr(s.regional, "management_projection",
		lambda *a, **k: ({("corp", "legacy")}, [line("allocated")]))
	assert [row["key"] for row in s.query({"view": "details"})["rows"]] == ["allocated"]


def test_changed_legacy_sibling_stays_reviewable_without_inflating_totals(monkeypatch):
	stored = legacy_dataset(monkeypatch)
	stored[1].source_version = "old"
	result = s.query()
	assert result["summary"]["amounts"]["CNY"] == "30.00"
	assert result["summary"]["excluded"] == 1
	review = s.query({"view": "review"})["rows"]
	assert [row["key"] for row in review] == ["mapped"]
	assert "regional_allocation" in review[0]["issues"]
	assert review[0]["amounts"] == {"CNY": None, "MXN": None}


def test_mx_classification_reaches_filters_comparison_details_and_export(monkeypatch):
	projection = s.regional.management_projection
	legacy_dataset(monkeypatch, companies=("A",))
	monkeypatch.setattr(s.regional, "management_projection", projection)
	monkeypatch.setattr(automatic, "legacy_manual_identities", lambda: set())
	configured = {**rules.defaults(), "category_mappings": [
		{"kind": "operating", "source_path": ["Services"], "category": "service"}]}
	monkeypatch.setattr(s.settings, "management_rules", lambda: configured)
	records = []
	for instance, day, amount in (("legacy", "2026-11-10", "100.00"), ("previous", "2026-10-10", "50.00")):
		source = dict(cn_application_date=day, approval_type="operating", approval_status="approved", total_amount=amount)
		original = dict(line_key="original", currency="CNY", classification_path='[{"label":"Services"}]')
		entries = [dict(line_key="original", region="mx", company="A", amount=amount, management_category="")]
		records.append(frappe._dict(name=instance, corp_id="corp", process_code="code", process_instance_id=instance,
			origin="mx", state="confirmed", modified="v1", facts_json=s.regional.encode([source, [original]]),
			allocations_json=s.regional.encode(entries)))
	monkeypatch.setattr(s.regional, "records", lambda: records)
	result = s.query({"category": "service"})
	assert result["summary"]["amounts"] == {"CNY": "100.00", "MXN": "250.00"}
	assert result["summary"]["review"] == 0
	assert [row["category"] for row in result["category_rows"]] == ["service"]
	assert result["comparison"]["percent"] == "100.0"
	details = s.query({"view": "details", "category": "service"})["rows"]
	assert len(details) == 1 and details[0]["management_category"] == "service"
	assert s.export_rows({"category": "service"})[0] == details
	assert s.query({"category": "unclassified"})["summary"]["count"] == 0
	assert all(s.regional.decode(record.allocations_json)[0]["management_category"] == "" for record in records)


def test_legacy_only_rows_are_visible_and_exportable_without_claiming_complete_snapshot(monkeypatch):
	from budget.api import expenses as api
	legacy_dataset(monkeypatch, snapshot=False)
	result = s.query()
	assert result["metadata"]["has_data"] is True
	assert result["metadata"]["complete"] is False
	assert result["summary"]["count"] == 2
	assert len(s.export_rows()[0]) == 2
	monkeypatch.setattr(api, "_", lambda value: value)
	monkeypatch.setattr(api, "workbook", lambda data, filename: data)
	assert api.export_data({"view": "budget"})[-2][0] == "A"


def test_hidden_legacy_rows_do_not_claim_data_availability(monkeypatch):
	legacy_dataset(monkeypatch, snapshot=False, companies=("Hidden",))
	result = s.query()
	assert result["metadata"]["has_data"] is False
	with pytest.raises(ValueError, match="No local approval snapshot"):
		s.export_rows()


def test_navigation_merges_only_when_all_pages_are_authorized(monkeypatch):
	monkeypatch.setattr(frappe,"get_installed_apps",lambda:["budget","deeplinkerp_branding"])
	def boot(pages):
		return {"page_info":dict.fromkeys(pages,{}),"workspace_sidebar_item":{"expense management":{}},
			"desktop_icons":[{"name":value} for value in ["Expense Management","Mexico Expenses","China Operating Expenses","Other"]]}
	complete=boot(navigation.PAGES);navigation.boot(complete)
	assert [row["name"] for row in complete["desktop_icons"]] == ["Expense Management","Other"]
	restricted=boot(navigation.PAGES[:-1]);navigation.boot(restricted)
	assert [row["name"] for row in restricted["desktop_icons"]] == ["Mexico Expenses","China Operating Expenses","Other"]
