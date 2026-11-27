"""SQL reporting parity, permission boundaries and atomic refresh failures."""
from copy import deepcopy
from datetime import datetime, timedelta
from decimal import Decimal
import json
from pathlib import Path
import re
import sqlite3
from types import SimpleNamespace

import frappe
import pytest

from budget.services import expense_management as legacy
from budget.services import expense_report_refresh as refresh
from budget.services import expense_report_store as store
from budget.services import expense_reporting as reporting
from budget.services import cn_approval_management_rules as rules


@pytest.fixture(autouse=True)
def group_settings(monkeypatch):
	monkeypatch.setattr(legacy.group_mappings, "read", lambda **kwargs: [])


def line(key, **values):
	return legacy.normalize({"key": key, "source_key": key, "company": "A", "number": key,
		"month": "2026-11", "application_date": "2026-11-10", "kind": "operating", "status": "approved",
		"amount": "100.00", "currency": "CNY", "issues": [], "management_category": "service",
		"source_path": ["Services"], "budget_state": "", "budget_categories_complete": False, **values})


def rate(currency, day):
	return {"CNY": Decimal(1), "MXN": Decimal("0.4"), "USD": Decimal("7")}[currency]


@pytest.fixture
def database(monkeypatch):
	"""Execute the actual selection/aggregate SQL, without a site or network."""
	monkeypatch.setattr(legacy.regions, "company_regions", lambda context: {name: "mx" if name == "B" else "cn" for name in context["companies"]})
	connection = sqlite3.connect(":memory:")
	connection.row_factory = sqlite3.Row
	path = Path(__file__).parents[1] / "budget/doctype/expense_report_line/expense_report_line.json"
	fields = json.loads(path.read_text())["fields"]
	connection.execute('create table "tabExpense Report Line" (name text primary key,' + ",".join(
		f'"{field["fieldname"]}" ' + ("numeric" if field["fieldtype"] in ("Check", "Currency") else "text") for field in fields) + ")")
	calls = []
	def sql(query, params=None, as_dict=False):
		calls.append(query)
		params = dict(params or {})
		def replace(match):
			key = match.group(1)
			value = params[key]
			if isinstance(value, (list, tuple)):
				placeholders = []
				for index, item in enumerate(value):
					name = key + str(index)
					params[name] = item
					placeholders.append(":" + name)
				return "(" + ",".join(placeholders) + ")"
			return ":" + key
		query = re.sub(r"%\((\w+)\)s", replace, query)
		rows = connection.execute(query, {k: v for k, v in params.items() if not isinstance(v, (list, tuple))}).fetchall()
		return [frappe._dict(dict(row)) if as_dict else tuple(row) for row in rows]
	monkeypatch.setattr(frappe, "db", SimpleNamespace(sql=sql))
	monkeypatch.setattr(frappe.utils, "today", lambda: "2026-11-25")
	monkeypatch.setattr(frappe.utils, "now_datetime", lambda: datetime(2026, 11, 25))
	monkeypatch.setattr(frappe, "get_system_settings", lambda key: {"date_format": "yyyy-mm-dd"}.get(key))
	monkeypatch.setattr(frappe, "throw", lambda message, *args: (_ for _ in ()).throw(ValueError(message)))
	monkeypatch.setattr(reporting, "_", lambda message: message)
	monkeypatch.setattr(refresh, "_", lambda message: message)
	monkeypatch.setattr(legacy, "_", lambda message: message)
	monkeypatch.setattr(store, "installed", lambda: False)
	def seed(rows, *, companies=("A", "B", "Empty"), manager=True, complete=True, legacy_keys=()):
		connection.execute('delete from "tabExpense Report Line"')
		converted = legacy.converter(rate)(rows)
		for row in converted:
			packed = store.pack(row)
			connection.execute('insert into "tabExpense Report Line" (' + ",".join(packed)
				+ ") values (" + ",".join("?" for _ in packed) + ")", tuple(packed.values()))
		context = {"companies": set(companies), "manager": manager, "legacy_keys": legacy_keys}
		metadata = {"complete": complete, "has_data": True, "updated_at": {"cn": "", "mx": ""}, "missing_history": 0}
		monkeypatch.setattr(reporting, "read_context", lambda: (context, rules.defaults(), deepcopy(metadata)))
		# The old projection is the business-rule oracle, independently filtered.
		visible = [row for row in rows if (row["company"] in companies or manager and not row["company"])
			and (not row.get("scope_company") or row["scope_company"] in companies)]
		monkeypatch.setattr(legacy, "load", lambda: (deepcopy(visible), context, rules.defaults(), deepcopy(metadata)))
		return converted
	yield seed, calls
	connection.close()


def canonical(value):
	if isinstance(value, dict):
		return {key: canonical(item) for key, item in value.items()}
	if isinstance(value, list):
		return sorted([canonical(item) for item in value], key=lambda item: json.dumps(item, sort_keys=True))
	return value


@pytest.mark.parametrize("view", ["overview", "analysis", "budget", "details", "review"])
@pytest.mark.parametrize("company,category,measure", [("", "", ""), ("A", "", "approved"), ("A", "unclassified", "")])
def test_sql_matches_existing_business_rules(database, monkeypatch, view, company, category, measure):
	seed, _calls = database
	rows = [line("a"), line("b", company="B", currency="USD", amount="1.11"),
		line("old", month="2026-10", application_date="2026-10-08", amount="50"),
		line("pending", status="pending"), line("withdrawn", status="withdrawn"),
		line("unknown", company="", issues=["organization"]), line("no-date", month=None, application_date=None),
		line("bad", amount=None, issues=["amount"]), line("unclassified", management_category=""),
		line("budget", kind="budget", amount="1000", budget_state="effective", source_path=["operation"])]
	seed(rows)
	converter = legacy.converter
	monkeypatch.setattr(legacy, "converter", lambda: converter(rate))
	f = {"view": view, "company": company, "category": category, "measure": measure}
	assert canonical(reporting.query(f)) == canonical(legacy.query(f))
	assert canonical(reporting.export_rows(f)) == canonical(legacy.export_rows(f))


def test_database_pagination_and_single_row_detail_never_load_snapshot(database):
	seed, calls = database
	seed([line(f"r{index:03}") for index in range(130)])
	first = reporting.query({"view": "details"})
	second = reporting.query({"view": "details"}, 50)
	assert first["total"] == 130 and first["has_more"] and len(first["rows"]) == 50
	assert not {row["key"] for row in first["rows"]} & {row["key"] for row in second["rows"]}
	assert any("limit %(limit)s offset %(start)s" in sql for sql in calls)
	key = first["rows"][0]["key"]
	assert reporting.detail(key, {"view": "details"})["row"] == first["rows"][0]
	assert all("Snapshot" not in sql and "Allocation" not in sql for sql in calls)


def test_source_revision_survives_sql_storage_only_for_authorized_detail_resolution(database, monkeypatch):
	from budget.services import expense_approval_links
	seed, _calls = database
	revision = "2026-11-26 10:00:00.000001"
	seed([line("selected", allocation_modified=revision)])
	seen = []
	def resolve(row, context, _rules):
		assert row["company"] in context["companies"]
		seen.append(row["allocation_modified"])
		return {"approval_url": "", "original_expense": None}
	monkeypatch.setattr(expense_approval_links, "original_context", resolve)
	result = reporting.detail("selected", {"view": "details"})
	assert seen == [revision]
	assert "allocation_modified" not in result["row"]
	assert "allocation_modified" not in reporting.query({"view": "details"})["rows"][0]
	assert "allocation_modified" not in reporting.export_rows({"view": "details"})[0][0]


@pytest.mark.parametrize("manager", [False, True])
def test_current_company_permissions_cover_extra_ownership_and_unknown_rows(database, manager):
	seed, _ = database
	seed([line("owned"), line("other", company="B"), line("restricted", scope_company="B"),
		line("unknown", company=""), line("pending-other", company="", scope_company="B")],
		companies=("A",), manager=manager)
	rows = reporting.query({"view": "details"})["rows"]
	assert {row["key"] for row in rows} == ({"owned", "unknown"} if manager else {"owned"})
	assert {row["key"] for row in reporting.export_rows({"view": "details"})[0]} == {row["key"] for row in rows}
	for key in ("other", "restricted", "pending-other"):
		with pytest.raises(ValueError, match="unavailable"):
			reporting.detail(key, {"view": "details"})
	with pytest.raises(ValueError, match="permission"):
		reporting.query({"company": "B"})


def test_missing_currency_never_becomes_zero_or_partial_sum(database, monkeypatch):
	seed, _ = database
	def missing(currency, day):
		raise legacy.fx.RateUnavailable()
	monkeypatch.setattr(__import__(__name__, fromlist=["rate"]), "rate", missing)
	seed([line("cn"), line("mx", currency="MXN")])
	result = reporting.query()
	assert result["summary"]["amounts"] == {"CNY": None, "MXN": None}
	assert result["summary"]["review"] == 2


@pytest.mark.parametrize("view", ["overview", "analysis", "budget", "details", "review"])
@pytest.mark.parametrize("scope", [{}, {"region": "cn"}, {"region": "mx"}, {"company": "B"}, {"company": "A", "region": "mx"}])
def test_regional_sql_and_snapshot_paths_agree(database, monkeypatch, view, scope):
	seed, _ = database
	seed([line("cn", amount="40"), line("cn2", company="Empty", amount="60"),
		line("mx", company="B", currency="MXN", amount="200"),
		line("budget", kind="budget", company="B", budget_state="effective", currency="MXN", amount="1000"),
		line("pending", company="B", status="pending"),
		line("undated", company="B", month=None, application_date=None)])
	converter = legacy.converter
	monkeypatch.setattr(legacy, "converter", lambda: converter(rate))
	f = {"view": view, **scope}
	assert canonical(reporting.query(f)) == canonical(legacy.query(f))
	assert canonical(reporting.export_rows(f)) == canonical(legacy.export_rows(f))


def test_regional_totals_shares_and_drilldown_do_not_mix_currencies(database):
	seed, _ = database
	seed([line("cn", amount="40"), line("cn2", company="Empty", amount="60"),
		line("mx", company="B", currency="MXN", amount="200")])
	result = reporting.query()
	regions = {row["region"]: row for row in result["regions"]}
	assert regions["cn"]["summary"]["amounts"]["CNY"] == "100.00"
	assert regions["mx"]["summary"]["amounts"]["MXN"] == "200.00"
	companies = {row["company"]: row for row in result["company_rows"]}
	assert {key: row["regional_share"] for key, row in companies.items()} == {"A": "40.0", "Empty": "60.0", "B": "100.0"}
	assert companies["B"]["display_currency"] == "MXN"
	f = {"view": "details", "region": "mx", "measure": "approved"}
	detail = reporting.query(f)
	assert [row["key"] for row in detail["rows"]] == ["mx"]
	assert reporting.export_rows(f)[0] == detail["rows"]
	assert reporting.detail("mx", f)["row"] == detail["rows"][0]
	with pytest.raises(ValueError, match="unavailable"):
		reporting.detail("cn", f)
	assert [row["region"] for row in reporting.query({"company": "B"})["regions"]] == ["mx"]
	with pytest.raises(ValueError, match="Invalid"):
		reporting.query({"region": "unknown"})


def test_regional_filters_preserve_company_permissions_and_empty_scope(database):
	seed, calls = database
	seed([line("owned"), line("other", company="B"), line("restricted", scope_company="B"),
		line("unassigned", company="")], companies=("A",), manager=True)
	assert [row["key"] for row in reporting.export_rows({"view": "details", "region": "cn"})[0]] == ["owned"]
	result = reporting.query({"view": "details", "region": "mx"})
	assert result["rows"] == result["regions"] == result["company_rows"] == []
	assert not any("in ()" in sql for sql in calls)
	with pytest.raises(ValueError, match="unavailable"):
		reporting.detail("other", {"region": "mx"})


def test_missing_fx_is_isolated_by_region_and_never_partial(database, monkeypatch):
	seed, _ = database
	def missing(currency, day):
		raise legacy.fx.RateUnavailable()
	monkeypatch.setattr(__import__(__name__, fromlist=["rate"]), "rate", missing)
	seed([line("cn"), line("mx", company="B", currency="MXN", amount="200")])
	regions = reporting.query()["regions"]
	assert [row["summary"]["amounts"][row["currency"]] for row in regions] == ["100.00", "200.00"]
	seed([line("cn"), line("mx", company="B", currency="MXN", amount="200"),
		line("unconvertible", company="B", currency="CNY")])
	regions = reporting.query()["regions"]
	assert [row["summary"]["amounts"][row["currency"]] for row in regions] == ["100.00", None]
	assert next(row for row in reporting.query()["company_rows"] if row["company"] == "B")["regional_share"] is None


def test_mexico_comparison_uses_mxn_even_when_monthly_fx_changes(database, monkeypatch):
	seed, _ = database
	def monthly_rate(currency, day):
		return Decimal(1) if currency == "CNY" else Decimal("0.4" if day.startswith("2026-11") else "0.2")
	monkeypatch.setattr(__import__(__name__, fromlist=["rate"]), "rate", monthly_rate)
	seed([line("current", company="B", currency="MXN", amount="200"),
		line("prior", company="B", currency="MXN", amount="100", month="2026-10", application_date="2026-10-10")])
	result = reporting.query({"company": "B"})
	assert result["comparison"]["percent"] == "300.0"  # Legacy CNY contract retained.
	assert result["regions"][0]["comparison"]["percent"] == "100.0"


@pytest.mark.parametrize("backend", ["sql", "snapshot"])
def test_group_scope_has_its_own_totals_details_and_export_without_a_company_record(database, monkeypatch, backend):
	seed, _ = database
	seed([line("subsidiary"), line("group", company="", entity="", amount="60"),
		line("group-prior", company="", entity="", amount="30", month="2026-10", application_date="2026-10-10"),
		line("bad-group", company="", entity="", amount=None, issues=["amount"]),
		line("unassigned", company="", entity="Unknown source", amount="40")], companies=("A",))
	converter = legacy.converter
	monkeypatch.setattr(legacy, "converter", lambda: converter(rate))
	service = reporting if backend == "sql" else legacy
	result = service.query()
	groups = {row["company"]: row for row in result["company_rows"]}
	key = legacy.regions.GROUP_COMPANY
	assert result["summary"]["amounts"]["CNY"] == "160.00"
	assert groups["A"]["amounts"]["CNY"] == "100.00"
	assert groups[key]["amounts"]["CNY"] == "60.00" and groups[key]["regional_share"] is None
	assert groups[key]["region"] == "group" and result["unassigned"] == 1
	group = next(region for region in result["regions"] if region["region"] == "group")
	assert group["comparison"]["percent"] == "100.0"
	selected = service.query({"company": key, "view": "details", "measure": "approved"})
	assert [row["key"] for row in selected["rows"]] == ["group"]
	assert selected["rows"][0]["company"] == "" and selected["rows"][0]["is_group_expense"] == 1
	assert selected["rows"][0]["display_currency"] == "CNY"
	exported, _, _ = service.export_rows({"company": key})
	assert [row["key"] for row in exported] == ["group"]
	assert service.detail("group", {"company": key})["row"]["region"] == "group"
	if backend == "sql":
		from budget.api import expenses as api
		monkeypatch.setattr(api, "_", lambda value: value)
		monkeypatch.setattr(api, "workbook", lambda data, filename: data)
		monkeypatch.setattr(api.service, "export_rows", service.export_rows)
		assert api.export_data({"company": key})[-1][1] == "Group"
	budget = service.query({"company": key, "view": "budget"})["budgets"][0]["metrics"]["CNY"]
	assert budget["budget"] is None and budget["approved"] is None and budget["balance"] is None


@pytest.mark.parametrize("backend", ["sql", "snapshot"])
def test_group_budget_is_missing_without_hiding_valid_spending(database, monkeypatch, backend):
	seed, _ = database
	seed([line("group", company="", entity="", amount="60")], companies=("A",))
	converter = legacy.converter
	monkeypatch.setattr(legacy, "converter", lambda: converter(rate))
	service = reporting if backend == "sql" else legacy
	metrics = service.query({"company": legacy.regions.GROUP_COMPANY, "view": "budget"})["budgets"][0]["metrics"]["CNY"]
	assert metrics["budget"] is None and metrics["approved"] == "60.00" and metrics["balance"] is None


@pytest.mark.parametrize("backend", ["sql", "snapshot"])
@pytest.mark.parametrize("manager", [True, False])
def test_named_group_assignment_keeps_source_company_for_permissions_but_uses_group_for_reporting(database, monkeypatch, backend, manager):
	seed, _ = database
	seed([line("ordinary"), line("named-group", company="A", entity="CEO office", amount="60", _group_mapping=True)],
		companies=("A",), manager=manager)
	converter = legacy.converter
	monkeypatch.setattr(legacy, "converter", lambda: converter(rate))
	service = reporting if backend == "sql" else legacy
	result = service.query()
	assert result["summary"]["amounts"]["CNY"] == ("160.00" if manager else "100.00")
	assert service.query({"company": "A", "view": "details"})["total"] == 1
	assert service.query({"region": "cn", "view": "details"})["total"] == 1
	if manager:
		selected = service.query({"company": legacy.regions.GROUP_COMPANY, "view": "details"})
		assert selected["total"] == 1 and selected["rows"][0]["company"] == "A"
		assert selected["rows"][0]["display_currency"] == "CNY"
		assert selected["rows"][0]["is_group_expense"] == 1
		assert service.detail("named-group", {"company": legacy.regions.GROUP_COMPANY})["row"]["company"] == "A"
		assert service.export_rows({"company": "A"})[0][0]["key"] == "ordinary"


@pytest.mark.parametrize("backend", ["sql", "snapshot"])
def test_group_expenses_keep_the_original_unassigned_access_boundary(database, monkeypatch, backend):
	seed, _ = database
	seed([line("subsidiary"), line("group", company="", entity="", amount="60")], companies=("A",), manager=False)
	converter = legacy.converter
	monkeypatch.setattr(legacy, "converter", lambda: converter(rate))
	service = reporting if backend == "sql" else legacy
	result = service.query()
	assert legacy.regions.GROUP_COMPANY not in result["companies"]
	assert result["summary"]["amounts"]["CNY"] == "100.00"
	assert all(row["region"] != "group" for row in result["regions"])
	with pytest.raises(ValueError, match="permission"):
		service.query({"company": legacy.regions.GROUP_COMPANY})


@pytest.mark.parametrize("backend", ["sql", "snapshot"])
def test_verified_comparison_keeps_amounts_when_other_lines_need_review(database, monkeypatch, backend):
	seed, _ = database
	seed([line("now", amount="200"),
		line("prior", amount="100", month="2026-10", application_date="2026-10-10"),
		line("bad", amount=None, issues=["amount"]),
		line("pending", status="pending", issues=["history"]),
		line("undated", month=None, application_date=None)], companies=("A",), complete=False)
	converter = legacy.converter
	monkeypatch.setattr(legacy, "converter", lambda: converter(rate))
	result = (reporting if backend == "sql" else legacy).query({"company": "A"})
	comparison = result["regions"][0]["comparison"]
	assert comparison["percent"] == "100.0"
	assert (comparison["current"], comparison["previous"]) == ("200.00", "100.00")
	assert set(comparison["warnings"]) == {"source_incomplete", "undated", "review", "excluded"}
	assert result["summary"]["excluded"] == 1 and result["undated"] == 1


@pytest.mark.parametrize("backend", ["sql", "snapshot"])
@pytest.mark.parametrize("company,currency", [("A", "CNY"), ("B", "MXN"), ("", "CNY")])
def test_comparison_exposes_prior_review_counts_in_the_exact_authorized_window(database, monkeypatch, backend, company, currency):
	seed, _ = database
	ownership = {"company": company, "currency": currency, **({"entity": ""} if not company else {})}
	seed([line("now", amount="200", **ownership),
		line("prior", amount="100", month="2026-10", application_date="2026-10-10", **ownership),
		line("prior-invalid", amount=None, issues=["amount"], month="2026-10", application_date="2026-10-10", **ownership),
		line("after-window", amount=None, issues=["amount"], month="2026-10", application_date="2026-10-30", **ownership),
		line("other-owner", company="Other", amount=None, issues=["amount"], month="2026-10", application_date="2026-10-10")],
		companies=(company,) if company else ())
	converter = legacy.converter
	monkeypatch.setattr(legacy, "converter", lambda: converter(rate))
	result = (reporting if backend == "sql" else legacy).query({"company": company or legacy.regions.GROUP_COMPANY})
	assert result["summary"]["review"] == result["summary"]["excluded"] == 0
	comparison = result["regions"][0]["comparison"]
	assert comparison["percent"] == "100.0" and set(comparison["warnings"]) == {"review", "excluded"}
	assert comparison["periods"] == {"current": {"review": 0, "excluded": 0}, "previous": {"review": 1, "excluded": 1}}
	assert "periods" not in result["comparison"]  # Preserve the legacy global response.


@pytest.mark.parametrize("backend", ["sql", "snapshot"])
@pytest.mark.parametrize("company,currency", [("A", "CNY"), ("B", "MXN")])
@pytest.mark.parametrize("scenario", ["hidden_fx_only", "history", "amount", "unknown_issue", "local_fx_missing", "incomplete", "undated", "zero_baseline"])
def test_local_comparison_uses_verified_sums_with_explicit_review_caveats(database, monkeypatch, backend, company, currency, scenario):
	seed, _ = database
	def missing(code, day):
		raise legacy.fx.RateUnavailable()
	monkeypatch.setattr(__import__(__name__, fromlist=["rate"]), "rate", missing)
	changes = {"history": {"issues": ["history"]}, "amount": {"amount": None},
		"unknown_issue": {"issues": ["new_validation_rule"]}, "local_fx_missing": {"currency": "USD"}}.get(scenario, {})
	rows = [line("now", company=company, **{"currency": currency, "amount": "200", **changes}),
		line("prior", company=company, currency=currency, amount="0" if scenario == "zero_baseline" else "100",
			month="2026-10", application_date="2026-10-10")]
	if scenario == "undated":
		rows.append(line("undated", company=company, currency=currency, month=None, application_date=None))
	seed(rows, companies=(company,), complete=scenario != "incomplete")
	converter = legacy.converter
	monkeypatch.setattr(legacy, "converter", lambda: converter(missing))
	service = reporting if backend == "sql" else legacy
	result = service.query({"company": company})
	region = result["regions"][0]
	comparable = scenario in {"hidden_fx_only", "history", "unknown_issue", "incomplete", "undated"}
	assert region["comparison"]["percent"] == ("100.0" if comparable else None)
	assert region["comparison"]["basis"] == "verified"
	assert region["comparison"]["status"] == ("comparable" if comparable else {
		"amount": "no_current_verified", "local_fx_missing": "amount_unavailable", "zero_baseline": "previous_zero"}[scenario])
	# General review and the legacy comparison retain all original issues.
	assert result["comparison"]["percent"] is None
	assert "exchange_rate" in service.detail("prior", {"month": "2026-10"})["row"]["issues"]
	if scenario == "hidden_fx_only":
		assert region["summary"]["amounts"][currency] == "200.00"
		assert result["summary"]["review"] == 1


@pytest.mark.parametrize("backend", ["sql", "snapshot"])
def test_excel_distinguishes_verified_expenses_from_regional_scope(database, monkeypatch, backend):
	import base64
	from io import BytesIO
	from openpyxl import load_workbook
	from budget.api import expenses as api
	seed, _ = database
	def missing(code, day):
		raise legacy.fx.RateUnavailable()
	monkeypatch.setattr(__import__(__name__, fromlist=["rate"]), "rate", missing)
	seed([line("cn"), line("unknown-country", company="B", currency="MXN"),
		line("no-local-fx", currency="USD"), line("pending", status="pending"),
		line("bad", amount=None), line("budget", kind="budget", budget_state="effective"),
		line("zero", amount="0")])
	monkeypatch.setattr(legacy.regions, "company_regions", lambda context: {"A": "cn", "B": "", "Empty": "cn"})
	converter = legacy.converter
	monkeypatch.setattr(legacy, "converter", lambda: converter(missing))
	service = reporting if backend == "sql" else legacy
	if backend == "sql":
		monkeypatch.setattr(api.service, "export_rows", reporting.export_rows)
	monkeypatch.setattr(api, "_", lambda value: value)
	result = api.export_data({"view": "details"})
	rows = list(load_workbook(BytesIO(base64.b64decode(result["content"]))).active.values)
	index = next(index for index, row in enumerate(rows) if row[0] == "Approval number")
	exported = {row[0]: dict(zip(rows[index], row)) for row in rows[index + 1:]}
	for key in ("cn", "zero", "no-local-fx"):
		assert exported[key]["Verified approved expenditure"] == exported[key]["Included in regional scope"] == "Yes"
	assert exported["zero"]["Reported amount"] == 0
	assert exported["unknown-country"]["Verified approved expenditure"] == "Yes"
	assert exported["unknown-country"]["Included in regional scope"] == "No"
	assert exported["unknown-country"]["Reported amount"] is None
	assert exported["unknown-country"]["Region"] == "Region not configured"
	assert "budget" not in exported
	for key in ("pending", "bad"):
		assert exported[key]["Verified approved expenditure"] == exported[key]["Included in regional scope"] == "No"
	# A missing local rate is still part of the scope, making its whole total
	# unavailable. Never silently omit that line and publish a partial sum.
	assert exported["no-local-fx"]["Reported amount"] is None
	region = service.query()["regions"][0]
	assert region["summary"]["count"] == 3 and region["summary"]["amounts"]["CNY"] is None


def test_unknown_company_country_is_not_guessed_from_name_or_currency(database, monkeypatch):
	seed, _ = database
	seed([line("cn"), line("mx", company="B", currency="MXN")])
	monkeypatch.setattr(legacy.regions, "company_regions", lambda context: {"A": "cn", "B": ""})
	result = reporting.query()
	assert [row["region"] for row in result["regions"]] == ["cn"]
	unknown = next(row for row in result["company_rows"] if row["company"] == "B")
	assert unknown["display_currency"] is unknown["regional_share"] is None
	assert reporting.detail("mx")["row"]["display_currency"] is None


def test_snapshot_contains_only_small_line_data_and_preserves_rounding(database):
	seed, _ = database
	row = line("split", amount="0.34", allocation_amounts=["0.33", "0.33", "0.34"],
		allocation_index=2, header_amount="1.00", raw_payload={"secret": "not-a-real-secret"})
	converted = seed([row])[0]
	packed = store.pack(converted)
	assert "raw_payload" not in packed["detail_json"] and "allocation_amounts" not in packed["detail_json"]
	assert reporting.detail("split")["row"]["amounts"] == converted["amounts"]


def test_legacy_lines_keep_native_doctype_permissions(database):
	seed, _ = database
	seed([line("native"), line("manual", is_legacy=True), line("stale-mapping", is_legacy=True)],
		legacy_keys=["manual"])
	assert {row["key"] for row in reporting.query({"view": "details"})["rows"]} == {"native", "manual"}
	with pytest.raises(ValueError, match="unavailable"):
		reporting.detail("stale-mapping")


@pytest.fixture
def historical_company_scope(database, monkeypatch):
	"""Run both reporting paths with the actual native permission SQL hook."""
	from .test_expense_management import legacy_dataset
	from deeplinkerp_branding.services import mexico_expenses as mx
	seed, _calls = database
	loader = legacy.load
	converter = legacy.converter
	stored = legacy_dataset(monkeypatch)
	stored[0].company = "B"  # Preserve the old manually confirmed Mexican line.
	for row in stored:
		row.mapping_version = "v1"
	env = SimpleNamespace(stored=stored, companies={"A", "B"}, manager=True)
	companies = [frappe._dict(name=name, country=country, is_group=group) for name, country, group in
		(("A", "China", 0), ("B", "Mexico", 0), ("Group", "China", 1))]
	columns = list(stored[0])
	frappe.db.sql('create table native_lines (' + ','.join(
		f'"{key}" ' + ('integer' if key == 'is_active' else 'text') for key in columns) + ')')
	def get_list(doctype, *, filters=None, user=None, fields=None, pluck=None, limit_page_length=0):
		if doctype == "Company":
			return [row.name for row in companies if row.name in env.companies
				and all(row.get(key) == value for key, value in (filters or {}).items())]
		assert doctype == mx.LINE
		frappe.db.sql('delete from native_lines')
		for row in stored:
			frappe.db.sql('insert into native_lines (' + ','.join(columns) + ') values ('
				+ ','.join('%(' + key + ')s' for key in columns) + ')', dict(row))
		rows = frappe.db.sql('select * from native_lines where ' + mx.permission_query_conditions(), as_dict=True)
		rows = [row for row in rows if all(row.get(key) in value[1] if isinstance(value, list)
			else row.get(key) == value for key, value in (filters or {}).items())]
		if limit_page_length:
			rows = rows[:limit_page_length]
		return [row[pluck] for row in rows] if pluck else rows
	monkeypatch.setattr(frappe, "get_list", get_list)
	monkeypatch.setattr(frappe, "session", SimpleNamespace(user="reviewer"))
	monkeypatch.setattr(frappe, "get_roles", lambda *args: ["System Manager"] if env.manager else ["Accounts User"])
	monkeypatch.setattr(frappe.db, "escape", lambda value: "'" + value.replace("'", "''") + "'", raising=False)
	monkeypatch.setattr(mx, "_mapping_version", lambda settings: "v1")
	monkeypatch.setattr(legacy.approvals, "allowed_companies", lambda: env.companies)
	monkeypatch.setattr(legacy.approvals, "access_context", lambda: {"companies": env.companies, "manager": env.manager})
	# Publish through the real legacy projection before testing SQL reads.
	rows, _context, _rules, metadata = loader()
	monkeypatch.setattr(legacy, "converter", converter)
	seed(rows)
	monkeypatch.setattr(legacy, "converter", lambda: converter(rate))
	monkeypatch.setattr(legacy, "load", loader)
	monkeypatch.setattr(reporting, "read_context", refresh.read_context)
	state = {"publication": "p1", "data_revision": "data", "policy_revision": "policy",
		"built_at": datetime(2026, 11, 25), "rates_checked_at": datetime(2026, 11, 25), "fx_missing": 0,
		"metadata_json": json.dumps(metadata), "rules_json": json.dumps(rules.defaults())}
	monkeypatch.setattr(store, "state", lambda **kwargs: state)
	monkeypatch.setattr(refresh, "live_revision", lambda: {"data": "data", "policy": "policy", "complete": True, "rebuilding": False})
	monkeypatch.setattr(refresh, "enqueue", lambda: pytest.fail("unchanged publication must not rebuild"))
	return env


def test_manual_mx_approval_keeps_chinese_sibling_in_projection_and_publication(historical_company_scope):
	from deeplinkerp_branding.services import mexico_expenses as mx
	env = historical_company_scope
	manual = deepcopy(env.stored[0])
	for service in (legacy, reporting):
		assert service.query()["summary"]["amounts"]["CNY"] == "100.00"
		assert service.query()["summary"]["applications"] == 1
		assert service.query({"company": "A"})["summary"]["amounts"]["CNY"] == "70.00"
		assert service.query({"company": "B"})["summary"]["amounts"]["CNY"] == "30.00"
		details = service.query({"view": "details"})["rows"]
		assert {row["key"] for row in details} == {"manual", "mapped"}
		assert service.export_rows()[0] == details
		assert service.detail("mapped")["row"]["amounts"]["CNY"] == "70.00"
	assert env.stored[0] == manual
	# The internal reporting read must not widen ordinary MX queries.
	assert frappe.get_list(mx.LINE, pluck="name") == ["manual"]


@pytest.mark.parametrize("companies,keys,total", [({"A"}, {"mapped"}, "70.00"),
	({"B"}, {"manual"}, "30.00"), (set(), set(), "0.00")])
def test_historical_report_company_permissions_apply_to_both_read_paths(historical_company_scope, companies, keys, total):
	env = historical_company_scope
	env.companies, env.manager = companies, False
	for service in (legacy, reporting):
		assert service.query()["summary"]["amounts"]["CNY"] == total
		assert {row["key"] for row in service.export_rows()[0]} == keys
		for key in {"manual", "mapped"} - keys:
			with pytest.raises(ValueError):
				service.detail(key)


@pytest.mark.parametrize("change", ["stale_mapping", "regional_owner", "group_company"])
def test_historical_report_still_excludes_stale_or_unauthorized_siblings(historical_company_scope, change):
	env = historical_company_scope
	env.manager = False
	env.stored[0].mapping_version = "old"  # Manual decisions retain their exception.
	if change == "stale_mapping":
		env.stored[1].mapping_version = "old"
	elif change == "regional_owner":
		env.stored[1].regional_company = "Forbidden"
	else:
		env.companies.add("Group")
		env.stored[1].company = "Group"
	for service in (legacy, reporting):
		assert service.query()["summary"]["amounts"]["CNY"] == "30.00"
		assert [row["key"] for row in service.export_rows()[0]] == ["manual"]


def test_missing_category_option_is_not_duplicated_across_approval_types(database):
	seed, _ = database
	seed([line("operating", source_path=[]), line("purchase", kind="purchase", source_path=[])])
	assert len(reporting.query()["source_categories"]) == 1


def test_rate_loader_uses_durable_verified_legs_without_network(monkeypatch):
	from deeplinkerp_branding.services import mexico_expense_fx as fx
	monkeypatch.setattr(fx, "load_month_rate", lambda *args, **kwargs: pytest.fail("network access"))
	loader, rates = refresh.rate_loader({"MXN:2026-11-01": "0.4"})
	assert loader("MXN", "2026-11-01") == Decimal("0.4")
	assert rates == {"MXN:2026-11-01": "0.4"}


def test_failed_daily_rate_refresh_retains_verified_exact_day_and_limits_requests(monkeypatch):
	from deeplinkerp_branding.services import mexico_expense_fx as fx
	cache = {}
	monkeypatch.setattr(frappe, "cache", SimpleNamespace(get_value=cache.get,
		set_value=lambda key, value, **kwargs: cache.update({key: value})))
	calls = []
	def unavailable(*args, **kwargs):
		calls.append(args)
		raise fx.RateUnavailable()
	monkeypatch.setattr(fx, "load_month_rate", unavailable)
	loader, _ = refresh.rate_loader({"MXN:2026-11-01": "0.4"}, renew=True)
	assert loader("MXN", "2026-11-01") == Decimal("0.4") and loader.pending
	for month in range(1, 10):
		with pytest.raises(fx.RateUnavailable):
			loader("MXN", f"2026-{month:02}-01")
	assert len(calls) == 6
	# A new source publication does not bypass the per-rate failure backoff.
	second, _ = refresh.rate_loader({"MXN:2026-11-01": "0.4"}, renew=True)
	assert second("MXN", "2026-11-01") == Decimal("0.4")
	assert len(calls) == 6


def test_queue_outage_does_not_fail_a_committed_source(monkeypatch, caplog):
	class Cache:
		def make_key(self, key): return key
		def set(self, *args, **kwargs): return True
		def delete(self, key): pass
	cache = Cache()
	monkeypatch.setattr(frappe, "get_installed_apps", lambda: ["budget", "deeplinkerp_branding"])
	monkeypatch.setattr(store, "installed", lambda: True)
	monkeypatch.setattr(frappe, "cache", lambda: cache)
	monkeypatch.setattr(frappe, "enqueue", lambda *a, **k: (_ for _ in ()).throw(ConnectionError("sensitive transport details")))
	assert refresh.enqueue() is False
	assert "queue unavailable" in caplog.text and "sensitive transport details" not in caplog.text


def test_publication_metadata_uses_same_snapshot_as_line_queries(monkeypatch):
	# A writer can commit after the request's first Company/source SELECT but
	# before it acquires the shared lock. A current-read header must not be
	# combined with old snapshot line rows in MariaDB repeatable-read mode.
	calls = []
	def current_read(*args, **kwargs):
		calls.append("lock")
		return [{"publication": "newly-committed"}]
	def snapshot_read(*args, **kwargs):
		calls.append("snapshot")
		return [{"publication": "request-snapshot"}]
	monkeypatch.setattr(frappe, "db", SimpleNamespace(multisql=current_read, sql=snapshot_read))
	assert store.state(lock="read")["publication"] == "request-snapshot"
	assert calls == ["lock", "snapshot"]


@pytest.mark.parametrize("policy_changed", [False, True])
def test_previous_complete_publication_is_kept_but_new_ownership_blocks_reads(monkeypatch, policy_changed):
	from budget.services import cn_approval_workbench as approvals
	monkeypatch.setattr(approvals, "require_reader", lambda: None)
	monkeypatch.setattr(approvals, "allowed_companies", lambda: {"A"})
	monkeypatch.setattr(frappe, "session", SimpleNamespace(user="Administrator"))
	monkeypatch.setattr(frappe, "throw", lambda message, *args: (_ for _ in ()).throw(ValueError(message)))
	monkeypatch.setattr(frappe, "db", SimpleNamespace(sql=lambda *a, **k: []))
	monkeypatch.setattr(refresh, "_", lambda message: message)
	monkeypatch.setattr(store, "count", lambda *a: 1)
	state = {"publication": "old", "data_revision": "old", "policy_revision": "old" if policy_changed else "policy",
		"built_at": "2026-11-25", "rules_json": "{}", "metadata_json": json.dumps({"complete": True,
		"has_data": True, "has_snapshot": True, "sync_revision": {"cn": ["old"], "mx": "old"}})}
	monkeypatch.setattr(store, "state", lambda **kwargs: state)
	monkeypatch.setattr(refresh, "live_revision", lambda: {"data": "new", "policy": "policy", "complete": True, "rebuilding": False})
	queued = []
	monkeypatch.setattr(refresh, "enqueue", lambda: queued.append(True))
	if policy_changed:
		with pytest.raises(ValueError, match="being prepared"):
			refresh.read_context()
	else:
		_context, _rules, metadata = refresh.read_context()
		assert not metadata["complete"] and metadata["reporting_pending"]
		assert metadata["reporting_publication"] == "old"
	assert queued == [True]


def test_failed_source_or_policy_refresh_cannot_publish_partial_rows(monkeypatch):
	class Lock:
		def __enter__(self): return self
		def __exit__(self, *args): return False
	rollbacks = []
	monkeypatch.setattr(frappe, "get_installed_apps", lambda: ["budget", "deeplinkerp_branding"])
	monkeypatch.setattr(store, "installed", lambda: True)
	monkeypatch.setattr(store, "state", lambda: {"publication": "old", "data_revision": "old"})
	monkeypatch.setattr(store, "publish", lambda *args: pytest.fail("must not publish"))
	monkeypatch.setattr(frappe, "cache", SimpleNamespace(lock=lambda *args, **kwargs: Lock()))
	monkeypatch.setattr(frappe, "session", SimpleNamespace(user="Administrator"))
	monkeypatch.setattr(frappe, "set_user", lambda user: None)
	monkeypatch.setattr(frappe, "flags", frappe._dict())
	monkeypatch.setattr(frappe, "local", SimpleNamespace(site="test.local"))
	monkeypatch.setattr(frappe, "db", SimpleNamespace(rollback=lambda: rollbacks.append(True)))
	versions = iter([{"data": "first", "policy": "p", "rebuilding": False},
		{"data": "second", "policy": "p", "rebuilding": False}])
	monkeypatch.setattr(refresh, "live_revision", lambda: next(versions))
	monkeypatch.setattr(legacy, "load", lambda: ([], {}, {}, {}))
	assert refresh.refresh()["superseded"]
	assert len(rollbacks) == 2
	monkeypatch.setattr(refresh, "live_revision", lambda: {"data": "new", "rebuilding": False})
	monkeypatch.setattr(legacy, "load", lambda: (_ for _ in ()).throw(RuntimeError("parse failed")))
	with pytest.raises(RuntimeError, match="parse failed"):
		refresh.refresh()
	assert len(rollbacks) == 4


def test_missing_rates_retry_after_interval_only(monkeypatch):
	monkeypatch.setattr(frappe.utils, "now_datetime", lambda: datetime(2026, 11, 25))
	state = {"publication": "p", "data_revision": "v", "fx_missing": 1,
		"built_at": frappe.utils.now_datetime() - timedelta(seconds=301)}
	assert refresh.needs_refresh(state, {"data": "v"})
	state["built_at"] = frappe.utils.now_datetime()
	assert not refresh.needs_refresh(state, {"data": "v"})
	assert refresh.needs_refresh(state, {"data": "new"})


@pytest.mark.parametrize("scope", [["corp-A"], ["corp-A", "corp-C"], ["corp-B", "corp-A"]])
def test_current_oa_corporation_scope_invalidates_published_reports(monkeypatch, scope):
	from budget.integrations import cn_approval_source as source, cn_approval_store as cn_store
	from budget.services import cn_approval_settings as settings, cn_approval_sync as cn
	from budget.services import cn_approval_workbench as approvals, organization_mapping
	from deeplinkerp_branding.services import regional_expenses as regional, operating_oa_http as http, mexico_expenses as mx
	allowed = ["corp-A", "corp-B"]
	url = "https://example.invalid"
	cn_settings = {"config_version": "cn-v1", "enabled": 1, "company_mappings": [], "management_rules": {},
		"templates": [{"corp_id": "corp-A", "process_code": "PROC-A", "approval_type": "operating",
			"enabled": 1, "field_mapping": {}, "classification_fields": ""}]}
	mx_settings = frappe._dict(config_version="mx-v1", company_mappings=[], templates=[frappe._dict(
		corp_id="corp-B", process_code="PROC-B", approval_type="expense", enabled=1, last_sync_at="2026-11-25 10:00:00")])
	record = frappe._dict(name="source-B", corp_id="corp-B", connection_fingerprint=regional.digest([url]))
	def sql(query, *args, **kwargs):
		if "max(reviewed_at)" in query:
			return [(None,)]
		if "max(modified)" in query:
			return [("2026-11-25 10:00:00", 1)]
		if "distinct source_key" in query:
			return []
		pytest.fail("Unexpected SQL")
	def get_all(doctype, **kwargs):
		return [frappe._dict(name="A", country="Mexico")] if doctype == "Company" else [record]
	monkeypatch.setattr(frappe, "local", SimpleNamespace(site="isolated-review"))
	monkeypatch.setattr(frappe, "flags", frappe._dict())
	monkeypatch.setattr(frappe, "session", SimpleNamespace(user="Administrator"))
	monkeypatch.setattr(frappe, "db", SimpleNamespace(sql=sql))
	monkeypatch.setattr(frappe, "get_installed_apps", lambda: ["budget", "deeplinkerp_branding"])
	monkeypatch.setattr(frappe, "get_all", get_all)
	monkeypatch.setattr(frappe, "throw", lambda message, *args: (_ for _ in ()).throw(ValueError(message)))
	monkeypatch.setattr(frappe.utils, "now_datetime", lambda: datetime(2026, 11, 25, 10))
	monkeypatch.setattr(http, "configuration", lambda: (url, tuple(allowed)))
	monkeypatch.setattr(source, "client", lambda: http)
	monkeypatch.setattr(settings, "read_saved", lambda: cn_settings)
	monkeypatch.setattr(organization_mapping, "approval_companies", lambda: {})
	monkeypatch.setattr(mx, "_settings", lambda: mx_settings)
	monkeypatch.setattr(regional, "installed", lambda: True)
	monkeypatch.setattr(regional, "_", lambda value: value)
	monkeypatch.setattr(refresh, "_", lambda value: value)
	monkeypatch.setattr(cn_store, "read", lambda key: {})
	monkeypatch.setattr(cn, "status", lambda record, key: {"source_id": key, "snapshot_id": "snapshot-1",
		"settings_version": "cn-v1", "allocation_revision": "unchanged", "has_snapshot": True,
		"status": "success", "allocation_rebuilding": False})
	before = refresh.live_revision()
	with monkeypatch.context() as group_change:
		group_change.setattr(legacy.group_mappings, "read", lambda **kwargs: [{"match_kind": "organization", "corp_id": "corp-A", "source_name": "CEO"}])
		changed = refresh.live_revision()
		assert changed["policy"] != before["policy"] and changed["versions"] == before["versions"]
	assert regional.records() == [record]
	state = {"publication": "p1", "data_revision": before["data"], "policy_revision": before["policy"],
		"rates_checked_at": datetime(2026, 11, 25, 10), "built_at": datetime(2026, 11, 25, 10), "fx_missing": 0,
		"metadata_json": json.dumps({"complete": True, "has_data": True, "has_snapshot": True,
			"sync_revision": before["versions"]}), "rules_json": "{}"}
	monkeypatch.setattr(store, "state", lambda **kwargs: state)
	monkeypatch.setattr(store, "count", lambda *args: 1)
	monkeypatch.setattr(approvals, "require_reader", lambda: None)
	monkeypatch.setattr(approvals, "allowed_companies", lambda: {"A"})
	queued = []
	monkeypatch.setattr(refresh, "enqueue", lambda: queued.append(True))
	allowed[:] = scope
	frappe.flags.clear()
	after = refresh.live_revision()
	if "corp-B" not in scope:
		with pytest.raises(ValueError, match="another OA connection"):
			regional.records()
		assert after["policy"] != before["policy"] and after["data"] != before["data"]
		with pytest.raises(ValueError, match="being prepared"):
			refresh.read_context()
		assert queued == [True]
	else:
		assert after == before
		assert refresh.read_context()[2]["complete"]
		assert queued == []
	# Restoring exactly the authorized source scope can reuse the intact publication.
	allowed[:] = ["corp-A", "corp-B"]
	frappe.flags.clear()
	assert refresh.read_context()[2]["complete"]
	# A deployment must not reuse the old publication which omitted CN siblings.
	with monkeypatch.context() as old_code:
		old_code.setattr(refresh, "VERSION", "expense-report-v3")
		previous = refresh.live_revision()
	assert previous["policy"] != before["policy"] and previous["data"] != before["data"]
	state.update(policy_revision=previous["policy"], data_revision=previous["data"])
	queued.clear()
	with pytest.raises(ValueError, match="being prepared"):
		refresh.read_context()
	assert queued == [True]


@pytest.mark.parametrize("failure", ["client", "key", "set", "queue", "cleanup"])
def test_commit_callback_survives_all_cache_and_queue_outages(monkeypatch, caplog, failure):
	def fail():
		raise ConnectionError("private transport details")
	class Cache:
		def make_key(self, key): return fail() if failure == "key" else key
		def set(self, *args, **kwargs): return fail() if failure == "set" else True
		def delete(self, key): return fail() if failure == "cleanup" else None
	callbacks, rollbacks = [], []
	monkeypatch.setattr(store, "installed", lambda: True)
	monkeypatch.setattr(frappe, "get_installed_apps", lambda: ["budget", "deeplinkerp_branding"])
	monkeypatch.setattr(frappe, "cache", lambda: fail() if failure == "client" else Cache())
	monkeypatch.setattr(frappe, "enqueue", lambda *a, **k: fail())
	monkeypatch.setattr(frappe, "flags", frappe._dict())
	monkeypatch.setattr(frappe, "db", SimpleNamespace(after_commit=SimpleNamespace(add=callbacks.append),
		after_rollback=SimpleNamespace(add=rollbacks.append)))
	refresh.after_source_change()
	refresh.after_source_change()
	assert len(callbacks) == len(rollbacks) == 1
	callbacks[0]()  # Frappe executes this only after the source COMMIT.
	assert not frappe.flags.expense_report_enqueue_registered
	assert "queue unavailable" in caplog.text and "private transport details" not in caplog.text


@pytest.mark.parametrize("entry", ["after_source_change", "enqueue", "scheduled_refresh", "refresh"])
def test_reporting_is_inert_without_optional_branding_app(monkeypatch, entry):
	monkeypatch.setattr(frappe, "get_installed_apps", lambda: ["frappe", "erpnext", "budget"])
	monkeypatch.setattr(store, "installed", lambda: True)
	monkeypatch.setattr(frappe, "flags", frappe._dict())
	monkeypatch.setattr(frappe, "db", SimpleNamespace())
	monkeypatch.setattr(frappe, "cache", lambda: pytest.fail("Unexpected reporting coordination"))
	monkeypatch.setattr(frappe, "enqueue", lambda *a, **k: pytest.fail("Unexpected reporting job"))
	monkeypatch.setattr(refresh, "live_revision", lambda: pytest.fail("Unexpected integration access"))
	assert getattr(refresh, entry)() is None
	assert not frappe.flags.get("expense_report_enqueue_registered")


@pytest.mark.parametrize("renew", [False, True])
def test_missing_rate_retries_reach_later_months_after_cache_expiry(monkeypatch, renew):
	from deeplinkerp_branding.services import mexico_expense_fx as fx
	now, entries, calls = [0], {}, []
	def get(key):
		value, until = entries.get(key, (None, 0))
		return value if now[0] < until else None
	def put(key, value, expires_in_sec):
		entries[key] = (value, now[0] + expires_in_sec)
	def provider(currency, day, **kwargs):
		calls.append(day)
		if day < "2026-07-01":
			raise fx.RateUnavailable()
		return Decimal("0.4")
	monkeypatch.setattr(frappe, "cache", SimpleNamespace(get_value=get, set_value=put))
	monkeypatch.setattr(fx, "load_month_rate", provider)
	monkeypatch.setattr(refresh.time, "monotonic", lambda: now[0])
	rows = [line(str(month), month=f"2026-{month:02}", currency="MXN") for month in range(1, 13)]
	saved = {f"MXN:2026-{month:02}-01": "0.35" for month in range(1, 7)} if renew else {}
	for cycle in range(3):
		before = len(calls)
		loader, saved = refresh.rate_loader(json.loads(json.dumps(saved)), renew=renew)
		converted = legacy.converter(loader)(rows)
		assert len(calls) - before <= 6
		assert all(row["amounts"]["CNY"] == ("35.00" if renew else None) for row in converted[:6])
		now[0] += 360  # All five-minute failure keys expire before every retry.
		if cycle == 0:
			assert all(row["amounts"]["CNY"] is None for row in converted[6:])
		else:
			assert all(row["amounts"]["CNY"] == "40.00" for row in converted[6:])
	assert set(calls) == {f"2026-{month:02}-01" for month in range(1, 13)}
	# A removed month cannot keep the remaining work blocked by its old priority.
	loader, saved = refresh.rate_loader(json.loads(json.dumps(saved)))
	legacy.converter(loader)(rows[6:])
	assert not loader.pending
	assert "_pending" not in saved
