"""CN company scope and reporting currency across the read-only endpoints."""
import base64
from copy import deepcopy
from decimal import Decimal
from io import BytesIO
from unittest.mock import Mock

import frappe
import openpyxl
import pytest

from budget.api import cn_approvals as api
from budget.services import cn_approval_contract as contract
from budget.services import cn_approval_fx as fx
from budget.services import cn_approval_workbench as service
from budget.tests.test_cn_approvals import runtime, sample
from deeplinkerp_branding.services import mexico_expense_fx as shared_fx


def row(kind="operating", **values):
	item = contract.project(*sample(kind))
	return {**item, "company": "Allowed Co", **values}


def setup_rows(monkeypatch, rows):
	context = {"companies": {"Allowed Co", "Other Co"}, "manager": True}
	monkeypatch.setattr(service, "load", lambda **kwargs: ({"fetched_at": "2026-11-20"}, deepcopy(rows), context))
	monkeypatch.setattr(shared_fx, "load_month_rate", lambda currency, month: Decimal({"USD": "7", "MXN": ".35"}[currency]))
	monkeypatch.setattr(frappe, "only_for", lambda *args: None)
	monkeypatch.setattr(api, "_", lambda text: text)
	monkeypatch.setattr(frappe, "get_system_settings", lambda key: {"date_format": "yyyy-mm-dd", "time_format": "HH:mm:ss"}.get(key))
	return context


@pytest.fixture(autouse=True)
def no_settings_io(monkeypatch):
	monkeypatch.setattr(service.settings, "can_manage", lambda *a: False)


@pytest.mark.parametrize("kind", contract.KINDS)
def test_cn_uses_projected_month_without_mx_finance_policy(kind):
	item = row(kind, currency="USD", month="2026-10", application_date="2026-09-20", finance_approved_date="2026-11-10")
	loader = Mock(return_value=Decimal("7"))
	result = fx.convert([item, item], "CNY", loader=loader)
	assert result[0]["converted_amount"] == "700.00"
	assert result[0]["exchange_rate_date"] == "2026-10-01"
	assert result[0]["fx_date_basis"] == ("budget" if kind == "budget" else "application")
	loader.assert_called_once_with("USD", "2026-10-01")
	assert "converted_amount" not in item


def test_projection_sets_budget_month_independently_and_expenses_use_application_month():
	raw, config = sample("budget")
	next(f for f in raw["form_component_values"] if f["id"] == "DDDateField_RG3T9I5UX1S0")["value"] = "2026-10"
	assert contract.project(raw, config)["month"] == "2026-10"
	for kind in ("operating", "purchase"):
		item = row(kind)
		assert item["month"] == item["application_date"][:7]


def test_cross_currency_rounds_each_approval_once_then_sums_and_reuses_month_legs():
	loader = Mock(side_effect=lambda currency, month: Decimal("7" if currency == "USD" else ".35" if month == "2026-11-01" else ".28"))
	items = [row(currency="MXN", amount="0.10") for _ in range(3)]
	converted = fx.convert(items, "USD", loader=loader)
	assert [item["converted_amount"] for item in converted] == ["0.01"] * 3
	assert service.money_totals(converted)[0]["amount"] == "0.03"
	assert loader.call_count == 2
	results = fx.convert([row(currency="MXN"), row(currency="MXN", month="2026-10")], "USD", loader=loader)
	assert [item["converted_amount"] for item in results] == ["5.00", "4.00"]


def test_same_currency_zero_and_large_amounts_stay_exact_without_provider():
	loader = Mock(side_effect=AssertionError("provider must not be called"))
	results = fx.convert([row(amount="0.00"), row(amount="99999999999999.99")], "CNY", loader=loader)
	assert [item["converted_amount"] for item in results] == ["0.00", "99999999999999.99"]
	assert all(item["exchange_rate"] == "1" for item in results)


@pytest.mark.parametrize("values", [{"currency": None}, {"currency": "EUR"}, {"currency": "USD", "month": None}, {"amount": None}, {"issues": ["structure"]}])
def test_incomplete_conversion_never_becomes_zero_or_complete_total(values):
	item = row(**values)
	converted = fx.convert([row(), item], "CNY", loader=Mock(side_effect=AssertionError("unexpected rate lookup")))
	total = service.money_totals(converted)[0]
	assert total == {"currency": "CNY", "amount": None, "known_amount": "100.00", "incomplete": 1}
	assert converted[-1]["fx_status"] == "incomplete"


def test_rate_failure_is_not_replaced_with_identity_or_partial_success():
	loader = Mock(side_effect=ValueError("month-start unavailable"))
	with pytest.raises(ValueError, match="month-start unavailable"):
		fx.convert([row(), row(currency="USD")], "CNY", loader=loader)


def test_company_scope_and_legacy_original_currency_filter(monkeypatch):
	items = [row(), row(currency="USD", number="usd"), row(company="Other Co"), row(company="")]
	setup_rows(monkeypatch, items)
	old = service.overview({"currency": "USD"})
	assert old["total"] == 1 and "converted_amount" not in old["rows"][0]
	result = service.overview({"company": "Allowed Co", "currency": "USD", "reporting_currency": "CNY"})
	assert result["total"] == 2
	assert result["summary"]["cards"][1]["totals"][0]["amount"] == "800.00"
	assert result["options"]["companies"] == ["Allowed Co", "Other Co"]
	assert sorted(result["options"]["reporting_currencies"]) == ["CNY", "MXN", "USD"]
	assert all("converted_amount" not in item for item in items)


def test_company_is_validated_for_both_list_and_export_before_fx(monkeypatch):
	setup_rows(monkeypatch, [row(currency="USD")])
	monkeypatch.setattr(fx, "convert", Mock(side_effect=AssertionError("must check company first")))
	for call in (service.overview, api.export_data):
		with pytest.raises(ValueError, match="permission"):
			call({"company": "Forbidden Co", "reporting_currency": "CNY"})


def test_conversion_does_not_fetch_rates_for_filtered_out_company_or_period(monkeypatch):
	setup_rows(monkeypatch, [row(), row(currency="USD", company="Other Co"), row(currency="USD", month="2026-09")])
	monkeypatch.setattr(shared_fx, "load_month_rate", Mock(side_effect=AssertionError("outside query scope")))
	assert service.overview({"company": "Allowed Co", "reporting_currency": "CNY"})["total"] == 1


@pytest.mark.parametrize("review", [True, False])
@pytest.mark.parametrize("issue,amount", [("amount", None), ("organization", "100.00")])
def test_issue_filters_apply_before_rates_in_overview_and_export(monkeypatch, review, issue, amount):
	items = [row(number="wanted", amount=amount, issues=[issue]), row(number="unrelated", currency="USD", issues=[])]
	setup_rows(monkeypatch, items)
	loader = Mock(side_effect=ValueError("unrelated USD rate unavailable"))
	monkeypatch.setattr(shared_fx, "load_month_rate", loader)
	f = {"reporting_currency": "CNY", "review": review, "issue": issue}
	result = service.overview(f)
	assert [item["number"] for item in result["rows"]] == ["wanted"]
	assert result["summary"]["review_count"] == result["total"] == 1
	book = openpyxl.load_workbook(BytesIO(base64.b64decode(api.export_data(f)["content"])))
	assert book.active.max_row == 2 and book.active["A2"].value == "wanted"
	loader.assert_not_called()
	assert all("converted_amount" not in item for item in items)


@pytest.mark.parametrize("issue", ["", "exchange_rate"])
def test_review_finds_conversion_issues_without_loading_unrelated_rates(monkeypatch, issue):
	items = [row(number="currency", currency=None, issues=["currency"]),
		row(number="period", currency="USD", month=None, issues=["period"]),
		row(number="amount", amount=None, issues=["amount"]),
		row(number="clean", currency="USD", issues=[])]
	setup_rows(monkeypatch, items)
	loader = Mock(side_effect=ValueError("unrelated USD rate unavailable"))
	monkeypatch.setattr(shared_fx, "load_month_rate", loader)
	result = service.overview({"reporting_currency": "CNY", "review": True, "issue": issue})
	expected = {"currency", "period"} if issue else {"currency", "period", "amount"}
	assert {item["number"] for item in result["rows"]} == expected
	assert result["summary"]["issues"]["exchange_rate"] == 2
	assert all(item["converted_amount"] is None for item in result["rows"])
	loader.assert_not_called()


def test_filtered_approval_still_requires_its_own_valid_rate(monkeypatch):
	setup_rows(monkeypatch, [row(currency="USD", issues=["organization"])])
	monkeypatch.setattr(shared_fx, "load_month_rate", Mock(side_effect=ValueError("selected USD rate unavailable")))
	for call in (service.overview, api.export_data):
		with pytest.raises(ValueError, match="selected USD rate unavailable"):
			call({"reporting_currency": "CNY", "review": True, "issue": "organization"})


@pytest.mark.parametrize("rebuilding", [False, True])
def test_independent_filter_options_use_company_permissions_without_oa_or_fx(monkeypatch, rebuilding):
	from deeplinkerp_branding.services import regional_expense_automatic
	reader = Mock()
	companies = Mock(return_value=["Allowed B", "Allowed A"])
	monkeypatch.setattr(frappe, "only_for", reader)
	monkeypatch.setattr(frappe, "get_list", companies)
	monkeypatch.setattr(service, "load", Mock(side_effect=AssertionError("OA must not be read")))
	monkeypatch.setattr(service, "access_context", Mock(side_effect=AssertionError("source mappings must not be read")))
	monkeypatch.setattr(shared_fx, "load_month_rate", Mock(side_effect=AssertionError("FX must not be read")))
	monkeypatch.setattr(regional_expense_automatic, "ensure_rebuild", lambda: rebuilding)
	assert api.get_filter_options() == {"companies": ["Allowed A", "Allowed B"], "reporting_currencies": ["CNY", "MXN", "USD"],
		"can_manage_settings": False, "allocation_rebuilding": rebuilding}
	reader.assert_called_once_with(("System Manager", "Accounts Manager"))
	companies.assert_called_once_with("Company", filters={"is_group": 0}, pluck="name", limit_page_length=0)


def test_independent_filter_options_reject_unauthorized_readers(monkeypatch):
	monkeypatch.setattr(frappe, "only_for", Mock(side_effect=frappe.PermissionError("role required")))
	companies = Mock(side_effect=AssertionError("must check reader first"))
	monkeypatch.setattr(frappe, "get_list", companies)
	with pytest.raises(frappe.PermissionError, match="role required"):
		api.get_filter_options()
	companies.assert_not_called()


def test_new_conversion_issues_participate_in_review_queue(monkeypatch):
	setup_rows(monkeypatch, [row(currency="EUR", issues=[])])
	result = service.overview({"reporting_currency": "CNY", "review": True, "issue": "exchange_rate"})
	assert result["total"] == result["summary"]["review_count"] == 1
	assert result["rows"][0]["issues"] == ["exchange_rate"]


def test_overview_detail_and_export_use_identical_conversion_and_keep_original(monkeypatch):
	item = row(currency="USD", number="=untrusted")
	setup_rows(monkeypatch, [item])
	from deeplinkerp_branding.services import mexico_expenses
	monkeypatch.setattr(mexico_expenses, "_dingtalk_url", lambda value: "")
	f = {"company": "Allowed Co", "reporting_currency": "CNY"}
	result = service.overview(f)
	detail = api.get_detail(item["key"], "CNY")
	assert result["rows"][0]["converted_amount"] == detail["converted_amount"] == "700.00"
	assert detail["amount"] == "100.00" and detail["currency"] == "USD"
	assert "converted_amount" not in api.get_detail(item["key"])
	book = openpyxl.load_workbook(BytesIO(base64.b64decode(api.export_data(f)["content"])))
	values = dict(zip(next(book.active.values), list(book.active.values)[1]))
	assert values["Application amount"] == 100 and values["Currency"] == "USD"
	assert values["Converted amount"] == 700 and values["Reporting Currency"] == "CNY"
	assert values["Exchange rate"] == "7.000000000000" and values["Exchange rate date"] == "2026-11-01"
	assert values["Exchange rate basis"] == "Application month"
	assert values["Approval number"] == "'=untrusted"


def test_excel_retains_large_decimal_cents_as_text():
	assert api.excel_amount("19999999999999.99") == "19999999999999.99"
	assert api.excel_amount("9999999999999.99") == Decimal("9999999999999.99")
	assert api.excel_amount(None) is None


@pytest.mark.parametrize("target", ["EUR", "usd", "All", "<script>"])
def test_invalid_reporting_currency_rejected_before_reading_data(monkeypatch, target):
	monkeypatch.setattr(service, "load", Mock(side_effect=AssertionError("must validate first")))
	with pytest.raises(ValueError, match="filters"):
		service.overview({"reporting_currency": target})
	with pytest.raises(ValueError, match="filters"):
		service.detail("a" * 64, target)
