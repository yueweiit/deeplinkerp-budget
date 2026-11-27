from datetime import date
from decimal import Decimal

import pytest

from budget.services import bank_journal_contract as journal


def test_default_month_and_closed_open_query_bounds():
	assert journal.month_bounds(None, "2026-11-26") == ("2026-11", date(2026, 11, 1), date(2026, 12, 1))
	assert journal.month_bounds("2026-12", "2026-11-26")[2] == date(2027, 1, 1)
	with pytest.raises(ValueError):
		journal.month_bounds("2026-13", "2026-11-26")


def test_source_refs_preserve_text_and_refuse_lossy_numbers():
	assert journal.references("200000000000000000001\n/200000000000000000002") == [
		"200000000000000000001",
		"200000000000000000002",
	]
	with pytest.raises(ValueError):
		journal.references([200000000000000000001])


@pytest.mark.parametrize("amount", ["0", "nan", "-1", "0.001", "1e309", True])
def test_invalid_native_amount(amount):
	with pytest.raises(ValueError):
		journal.clean_row({"date": "2026-11-06", "income": amount})


def test_amount_and_blank_are_distinct_and_source_keys_not_client_fields():
	result = journal.clean_row(
		{"date": "2026-11-06", "income": "1.25", "refs": [], "custom_budget_journal_book": "fake"}
	)
	assert result["income"] == "1.25" and result["expense"] is None
	assert "custom_budget_journal_book" not in result


def test_receipt_binds_payload_independent_of_mapping_order():
	assert journal.digest({"b": 2, "a": 1}) == journal.digest({"a": 1, "b": 2})
	assert journal.batch_digest({"income": "1", "refs": ["1"], "balance": "22"}) != journal.batch_digest(
		{"income": "2", "refs": ["1"], "balance": "22"}
	)


def test_service_role_can_only_use_the_import_routes(monkeypatch):
	import frappe
	from types import SimpleNamespace
	from budget.services.bank_journal_access import restrict_import_request

	forms = {}
	req = SimpleNamespace(path="/api/method/budget.api.bank_journal.get_options", method="GET")
	monkeypatch.setattr(frappe, "session", SimpleNamespace(user="journal-ai@example.invalid"))
	monkeypatch.setattr(frappe, "get_roles", lambda *_: ["Bank Journal Import API"])
	monkeypatch.setattr(frappe, "request", req, raising=False)
	monkeypatch.setattr(frappe, "form_dict", forms, raising=False)
	monkeypatch.setattr(frappe, "get_request_header", lambda *_: "token example:example")
	monkeypatch.setattr(frappe, "throw", lambda message, exc: (_ for _ in ()).throw(exc(message)))
	restrict_import_request()
	forms["cmd"] = "budget.api.bank_journal.save_row"
	with pytest.raises(frappe.PermissionError):
		restrict_import_request()
	forms.clear()
	req.method = "POST"
	req.path = "/api/method/budget.api.bank_journal_files.apply_rows"
	restrict_import_request()
	req.path = "/api/method/budget.api.bank_journal.save_row"
	with pytest.raises(frappe.PermissionError):
		restrict_import_request()
	monkeypatch.setattr(frappe, "get_roles", lambda *_: ["Accounts User"])
	restrict_import_request()


def test_service_role_has_no_company_access_without_explicit_grants(monkeypatch):
	import frappe
	from types import SimpleNamespace
	from budget.services import bank_journal_access as access

	monkeypatch.setattr(frappe, "session", SimpleNamespace(user="journal-ai@example.invalid"))
	monkeypatch.setattr(frappe, "get_roles", lambda *_: ["Bank Journal Import API"])
	monkeypatch.setattr(access, "allowed_companies", lambda: {"A", "B"})
	monkeypatch.setattr(frappe, "get_all", lambda *a, **kw: [])
	assert access.journal_companies() == set()
	monkeypatch.setattr(frappe, "get_all", lambda *a, **kw: ["B", "not visible"])
	assert access.journal_companies() == {"B"}
