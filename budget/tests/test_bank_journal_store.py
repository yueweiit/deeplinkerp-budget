import json
from contextlib import nullcontext
from datetime import date
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import Mock

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
	req.path = "/api/method/budget.api.bank_journal.create_book"
	restrict_import_request()
	req.method = "GET"
	with pytest.raises(frappe.PermissionError):
		restrict_import_request()
	req.method = "POST"
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


@pytest.mark.parametrize("writes", [0, 1])
def test_lock_retries_a_stale_snapshot_only_before_writing(monkeypatch, writes):
	from types import SimpleNamespace
	from unittest.mock import Mock

	import frappe

	from budget.api import bank_journal as api

	db = SimpleNamespace(transaction_writes=writes, rollback=Mock(), savepoint=Mock())
	monkeypatch.setattr(frappe, "db", db, raising=False)
	existing = object()
	acquire = Mock(side_effect=[frappe.QueryDeadlockError(), existing])
	if writes:
		with pytest.raises(frappe.QueryDeadlockError):
			api._lock_current_snapshot(acquire)
		db.rollback.assert_not_called()
	else:
		assert api._lock_current_snapshot(acquire) is existing
		db.rollback.assert_called_once_with()
		db.savepoint.assert_called_once_with("bank_journal_change")


def test_replacement_impact_uses_inclusive_transaction_dates_and_prior_native_balance(monkeypatch):
	from types import SimpleNamespace
	from unittest.mock import Mock
	import frappe
	from budget.api import bank_journal as api

	doc = SimpleNamespace(name="current")
	old = [frappe._dict(name="a", direction="expense", amount=12)]
	read = Mock(return_value=old)
	prior = Mock(return_value=Decimal("90.00"))
	monkeypatch.setattr(api, "_range_entries", read)
	monkeypatch.setattr(api, "_prior", prior)
	rows = [dict(date="2026-11-05", income="20.00", expense=None, balance=None, source_row=2),
		dict(date="2026-11-20", income=None, expense="5.75", balance="104.25", source_row=3)]
	impact, entries, issues = api.replacement_impact(doc, rows, None)
	read.assert_called_once_with(doc, "2026-11-05", "2026-11-20")
	prior.assert_called_once_with(doc, date(2026, 11, 5))
	assert entries == old and not issues
	assert impact == dict(start="2026-11-05", end="2026-11-20", old_count=1, new_count=2,
		opening="90.00", old_closing="78.00", new_closing="104.25", difference="26.25")
	rows[-1]["balance"] = "104.24"
	assert api.replacement_impact(doc, rows, None)[2][0]["row"] == 3
	assert api.replacement_impact(doc, rows, "0.00")[2][0]["field"] == "opening"


def test_options_only_offer_enabled_standard_two_decimal_currencies_and_show_legacy_books(monkeypatch):
	import frappe
	from budget.api import bank_journal as api

	def get_all(doctype, **query):
		if doctype == api.BOOK:
			assert "currency" in query["fields"]
			return [frappe._dict(name="old", currency=None), frappe._dict(name="foreign", currency="MXN")]
		assert doctype == "Currency" and query["filters"] == {"enabled": 1}
		return [frappe._dict(name=code, fraction_units=units) for code, units in (
			("CNY", 100), ("MXN", 100), ("USD", 100), ("KWD", 1000), ("CLF", 100),
			("JPY", 1), ("EUR", 1000), ("QQQ", 100))]

	monkeypatch.setattr(frappe, "get_all", get_all)
	monkeypatch.setattr(frappe, "get_list", lambda *args, **kwargs: [])
	monkeypatch.setattr(frappe, "has_permission", lambda *args, **kwargs: True)
	monkeypatch.setattr(frappe, "session", SimpleNamespace(user="Administrator"))
	monkeypatch.setattr(frappe.utils, "today", lambda: "2026-11-27")
	monkeypatch.setattr(api, "is_import_user", lambda: False)
	monkeypatch.setattr(api, "allowed_companies", lambda: ["A"])
	options = api.get_options()
	assert options["currencies"] == ["CNY", "MXN", "USD"]
	assert [book["currency"] for book in options["books"]] == ["CNY", "MXN"]
	assert [book.name for book in options["books"]] == ["old", "foreign"]


def test_creation_requires_enabled_currency_and_locks_account_identity_across_currencies(monkeypatch):
	import frappe
	from budget.api import bank_journal as api

	monkeypatch.setattr(api, "supported_currencies", lambda: ["CNY", "MXN", "USD"], raising=False)
	monkeypatch.setattr(api, "authorize_book_creation", Mock())
	monkeypatch.setattr(api, "_transaction", nullcontext)
	monkeypatch.setattr(api, "_lock_current_snapshot", lambda acquire: acquire())
	locked = Mock(return_value=None)
	monkeypatch.setattr(api, "_lock_company_book", locked)
	insert = Mock()
	monkeypatch.setattr(api, "_insert_book", insert)
	monkeypatch.setattr(frappe, "throw", lambda message, *args: (_ for _ in ()).throw(ValueError(message)))
	args = ("A", "  Operating  ", "0", "2024-01-01")
	with pytest.raises(ValueError, match="币种"):
		api.create_book(*args, currency="KWD")
	created = api.create_book(*args, currency="MXN")
	settings = insert.call_args.args[0]
	assert created["currency"] == settings.currency == "MXN"
	locked.assert_called_with("A", settings.name)
	locked.return_value = settings
	assert api.create_book(*args, currency="MXN") == created
	with pytest.raises(ValueError, match="MXN"):
		api.create_book(*args)
	legacy, issues = api.book_settings(*args)
	assert not issues and legacy.currency == "CNY" and legacy.name == settings.name
	with pytest.raises(ValueError, match="MXN"):
		api.apply_batch(legacy.name, [], "Import", 0, "original-id", new_book=legacy)
	insert.assert_called_once()


def test_native_book_schema_keeps_currency_on_historical_books():
	from pathlib import Path

	package = Path(__file__).parents[1]
	schema_path = package / "budget/doctype/cn_bank_journal_book/cn_bank_journal_book.json"
	schema = json.loads(schema_path.read_text())
	fields = {row["fieldname"]: row for row in schema["fields"]}
	currency = fields.get("currency", {})
	assert currency.get("default") == "CNY" and currency.get("set_only_once") == 1
	assert currency["options"] == "Currency" and fields["opening"]["options"] == "currency"
	assert "budget.patches.add_bank_journal_currency" in (package / "patches.txt").read_text().splitlines()
	assert fields["bank_balance_offset"]["set_only_once"] == 1
	assert fields["bank_balance_offset"]["default"] == "0"


def test_new_book_rechecks_enabled_currency_before_insert(monkeypatch):
	import frappe
	from budget.api import bank_journal as api

	get_value = Mock(return_value=None)
	monkeypatch.setattr(frappe, "db", SimpleNamespace(get_value=get_value), raising=False)
	monkeypatch.setattr(frappe, "throw", lambda msg, *args: (_ for _ in ()).throw(ValueError(msg)))
	get_doc = Mock()
	monkeypatch.setattr(frappe, "get_doc", get_doc)
	settings = frappe._dict(name="book", company="A", account="Cash", currency="MXN",
		opening_date="2026-01-01", opening="0.00")
	with pytest.raises(ValueError, match="币种已停用"):
		api._insert_book(settings)
	get_doc.assert_not_called()
	get_value.assert_called_once_with("Currency", {"name": "MXN", "enabled": 1}, ["name", "fraction_units"],
		as_dict=True, for_update=True)
	get_value.return_value = frappe._dict(name="MXN", fraction_units=100)
	api._insert_book(settings)
	assert get_doc.call_args.args[0]["currency"] == "MXN"
	get_doc.return_value.insert.assert_called_once_with(ignore_permissions=True, set_name="book")


@pytest.fixture
def foreign_ledger(monkeypatch):
	import frappe
	from budget.api import bank_journal as api

	book = frappe._dict(name="mx-book", company="A", account="Operating", currency="MXN",
		opening_date="2024-01-01", opening="100.00", next_sequence=0, revision=0)
	entry = frappe._dict(name="entry", reference_number="")
	entry.update(set=entry.__setitem__, insert=Mock(), save=Mock(), check_permission=Mock())
	monkeypatch.setattr(frappe, "throw", lambda msg, *args: (_ for _ in ()).throw(ValueError(msg)))
	monkeypatch.setattr(frappe, "new_doc", lambda doctype: entry)
	monkeypatch.setattr(frappe, "get_doc", lambda doctype, name, **kw: entry)
	monkeypatch.setattr(frappe, "session", SimpleNamespace(user="Operator"))
	monkeypatch.setattr(api, "_transaction", nullcontext)
	monkeypatch.setattr(api, "_begin", lambda *args, **kwargs: (book, "op", "fp", None))
	finish = Mock(side_effect=lambda *args, **kwargs: kwargs.get("result", {}))
	monkeypatch.setattr(api, "_finish", finish)
	monkeypatch.setattr(api, "_reindex", Mock())
	return api, book, entry, finish


def test_manual_foreign_book_edit_preserves_source_remark_and_currency_is_checked(foreign_ledger):
	api, book, entry, finish = foreign_ledger
	row = dict(date="2024-01-05", expense="12.25", summary="Fee", dept="Finance", remark="Nota bancaria")
	api.save_row(book.name, row, 0, "create-test")
	assert entry.currency == "MXN" and api._wire(entry)["currency"] == "MXN"
	assert api._wire(entry)["remark"] == "Nota bancaria"
	# Clients from before the notes field existed can still edit the amount.
	edit = {k: v for k, v in row.items() if k != "remark"}
	api.save_row(book.name, dict(edit, name=entry.name, expense="15.00"), 1, "edit-test")
	assert api._wire(entry)["remark"] == "Nota bancaria" and api._wire(entry)["dept"] == "Finance"
	prior = finish.call_args.args[4][0]["before"]
	assert prior["amount"] == "12.25" and prior["currency"] == "MXN"
	api.save_row(book.name, dict(edit, name=entry.name, remark=""), 2, "clear-note")
	assert api._wire(entry)["remark"] == ""
	with pytest.raises(ValueError, match="MXN"):
		api.save_row(book.name, dict(row, currency="USD"), 3, "wrong-iso")
	entry.currency = "USD"
	with pytest.raises(ValueError, match="不属于"):
		api.cancel_row(book.name, entry.name, 3, "wrong-row")


def test_cancel_and_undo_restore_native_foreign_amount_currency_and_notes(foreign_ledger, monkeypatch):
	import frappe
	api, book, entry, finish = foreign_ledger
	api.save_row(book.name, dict(date="2024-02-12", income="4.50", remark="Comisión"), 0, "create-test")
	before = api._wire(entry)
	api.cancel_row(book.name, entry.name, 1, "cancel-test")
	assert entry.get(api.VOID) == 1
	book.last_operation = "canceled"
	operation = frappe._dict(actor="Operator", book=book.name, operation="Cancel", prior_operation="manual",
		changes=json.dumps(finish.call_args.args[4]))
	monkeypatch.setattr(frappe, "get_doc", lambda dt, name, **kw: operation if dt == api.OP else entry)
	api.undo(book.name, "canceled", 2, "undo-test")
	assert entry.get(api.VOID) == 0 and api._wire(entry) == before
	assert finish.call_args.kwargs["new_last"] == "manual"


def test_foreign_source_matching_and_range_use_original_book_and_currency(foreign_ledger, monkeypatch):
	import frappe
	api, book, entry, _ = foreign_ledger
	api.save_row(book.name, dict(date="2024-02-12", income="4.50"), 0, "manual-test")
	entry.reference_number = "MX-123"
	get_rows = Mock(return_value=[entry])
	monkeypatch.setattr(frappe, "get_list", get_rows)
	monkeypatch.setattr(frappe, "get_all", lambda *args, **kw: [])
	row = {**api._wire(entry), "bank_ref": "MX-123"}
	assert api._matching_entry(book, "Import", row, set()) == entry.name
	assert get_rows.call_args.kwargs["filters"] == {
		api.BOOK_FIELD: book.name, api.VOID: 0, "currency": "MXN", "reference_number": "MX-123"}
	count = Mock(return_value=1)
	monkeypatch.setattr(frappe, "db", SimpleNamespace(count=count), raising=False)
	assert api._range_entries(book, "2024-02-01", "2024-02-29") == [entry]
	assert get_rows.call_args.kwargs["filters"]["currency"] == "MXN"
	# Refuse a replacement if even a single stored row fell outside the readable currency scope.
	count.return_value = 2
	with pytest.raises(ValueError, match="无法覆盖"):
		api._range_entries(book, "2024-02-01", "2024-02-29")


def test_import_checks_rebased_ledger_preserves_bank_fingerprint_and_operation_review(foreign_ledger, monkeypatch):
	api, book, entry, finish = foreign_ledger
	monkeypatch.setattr(api, "_lock_company_book", lambda *args: None)
	monkeypatch.setattr(api, "_lock_current_snapshot", lambda acquire: acquire())
	monkeypatch.setattr(api, "_insert_book", Mock())
	monkeypatch.setattr(api, "_source_record", lambda *args: ("source", None))
	monkeypatch.setattr(api, "_matching_entry", lambda *args: None)
	register, balance_check = Mock(), Mock()
	monkeypatch.setattr(api, "_register", register)
	monkeypatch.setattr(api, "_import_balances", balance_check)
	raw = dict(date="2024-01-05", expense="12.25", summary="Fee", currency="MXN", remark="Nota",
		balance="37.75", ledger_balance="87.75", bank_ref="MX123", source_key="ref:MX123",
		raw_cells=[""] * 15, file_review={"source_row": 3, "original": {"summary": "Cargo"}})
	review = dict(file_sha256="original-file", currency="MXN", excluded_source_rows=[4],
		file_opening="50.00", book_opening="100.00")
	result = api.apply_batch(book.name, [raw], "Import", 0, "import-test", new_book=book, import_review=review)
	assert result["created"] == 1 and result["import_review"] == review
	assert Decimal(str(book.bank_balance_offset)) == 50
	assert entry.currency == api._wire(entry)["currency"] == "MXN" and api._wire(entry)["remark"] == "Nota"
	assert json.loads(entry.get(api.EXTRA))["file_review"]["confirmed_by"] == "Operator"
	balance_check.assert_called_once_with(book, [(date(2024, 1, 5), entry.name, Decimal("87.75"))])
	fingerprint = register.call_args.args[-1]
	assert fingerprint == journal.digest([journal.batch_digest({**journal.clean_row(raw), "bank_ref": "MX123"}), "37.75"])
	monkeypatch.setattr(api, "_source_record", lambda *args: ("source", SimpleNamespace(digest=fingerprint)))
	same_source = {key: value for key, value in raw.items() if key != "ledger_balance"}
	retry = api.apply_batch(book.name, [same_source], "Import", 1, "import-retry")
	assert retry["skipped"] == 1 and "import_review" not in retry
	with pytest.raises(ValueError, match="MXN"):
		api.apply_batch(book.name, [dict(same_source, currency="USD")], "Import", 1, "other-currency")
	with pytest.raises(ValueError, match="新账簿"):
		api.apply_batch(book.name, [raw], "Import", 1, "existing-offset")

	# A later original from the same bank still uses its original bank balance.
	monkeypatch.setattr(api, "_source_record", lambda *args: ("second", None))
	later = dict(date="2024-02-01", income="1.00", bank_ref="MX-02", source_key="second", balance="38.75")
	api.apply_batch(book.name, [later], "Import", 1, "bank-followup", opening="37.75")
	balance_check.assert_called_with(book, [(date(2024, 2, 1), entry.name, Decimal("88.75"))])
	expected = journal.digest([journal.batch_digest({**journal.clean_row(later), "bank_ref": "MX-02"}), "38.75"])
	assert register.call_args.args[-1] == expected
	# A file with only a carry-forward balance still gets exactly one offset.
	monkeypatch.setattr(api, "_prior", lambda *args: Decimal("88.75"))
	later = {**later, "date": "2024-02-02", "balance": None}
	api.apply_batch(book.name, [later], "Import", 2, "opening-followup", opening="38.75")
	with pytest.raises(ValueError, match="期初余额"):
		api.apply_batch(book.name, [later], "Import", 2, "incorrect-opening", opening="38.74")


def test_range_checks_source_or_ledger_balances_in_the_matching_book(foreign_ledger, monkeypatch):
	api, book, _, _ = foreign_ledger
	book.bank_balance_offset = "-80.00"
	monkeypatch.setattr(api, "_range_entries", lambda *args: [])
	monkeypatch.setattr(api, "_prior", lambda *args: Decimal("20.00"))
	bank_rows = [dict(date="2024-02-01", income="2.25", expense=None, balance="102.25", source_row=3)]
	ledger_rows = [dict(bank_rows[0], balance="22.25")]
	bank_impact, _, issues = api.replacement_impact(book, bank_rows, "100.00")
	assert not issues and bank_impact["new_closing"] == "22.25"
	own_impact, _, issues = api.replacement_impact(book, ledger_rows, "20.00", ledger_book=book.name)
	assert not issues and own_impact == bank_impact
	assert api.replacement_impact(book, bank_rows, "100.01")[2][0]["field"] == "opening"
	assert api.replacement_impact(book, [dict(bank_rows[0], balance="102.26")], "100.00")[2][0]["field"] == "balance"
	with pytest.raises(ValueError, match="其他账簿"):
		api.replacement_impact(book, ledger_rows, "20.00", ledger_book="different-book")


def test_undoing_first_import_retains_book_opening_and_its_bank_offset(foreign_ledger, monkeypatch):
	import frappe
	api, book, entry, finish = foreign_ledger
	monkeypatch.setattr(api, "_lock_company_book", lambda *args: None)
	monkeypatch.setattr(api, "_lock_current_snapshot", lambda acquire: acquire())
	monkeypatch.setattr(api, "_insert_book", Mock())
	monkeypatch.setattr(api, "_source_record", lambda *args: ("origin", None))
	monkeypatch.setattr(api, "_matching_entry", lambda *args: None)
	monkeypatch.setattr(api, "_register", Mock())
	monkeypatch.setattr(api, "_import_balances", Mock())
	review = {"file_opening": "120.00", "book_opening": "100.00", "difference": "-20.00", "currency": "MXN"}
	row = dict(date="2024-01-01", income="3.00", balance="123.00", source_key="first")
	api.apply_batch(book.name, [row], "Import", 0, "new-book", new_book=book, import_review=review)
	book.last_operation = "created"
	op = frappe._dict(actor="Operator", book=book.name, operation="Import", prior_operation="",
		changes=json.dumps(finish.call_args.args[4]))
	monkeypatch.setattr(frappe, "get_doc", lambda dt, name, **kw: op if dt == api.OP else entry)
	monkeypatch.setattr(frappe, "db", SimpleNamespace(delete=Mock()), raising=False)
	api.undo(book.name, "created", 1, "undo-import")
	assert entry.get(api.VOID) == 1 and Decimal(str(book.bank_balance_offset)) == -20
	assert book.opening == "100.00"


def test_own_export_append_is_idempotent_for_uncoded_rows_and_changes_require_replace(foreign_ledger, monkeypatch):
	api, book, entry, _ = foreign_ledger
	book.bank_balance_offset = "-80.00"
	api.save_row(book.name, dict(date="2024-02-01", income="2.25", summary="Depósito", remark="original"), 0, "manual")
	native = api._wire(entry)
	monkeypatch.setattr(api, "_prior", lambda *args: Decimal("20.00"))
	monkeypatch.setattr(api, "_rows", lambda *args: [native])
	monkeypatch.setattr(api, "_range_entries", lambda *args: [entry])
	monkeypatch.setattr(api, "authorize_replacement", Mock())
	retire, register = Mock(), Mock()
	monkeypatch.setattr(api, "_retire_entries", retire)
	monkeypatch.setattr(api, "_register", register)
	rows = [{**native, "balance": "22.25", "source_key": "from-ledger-balance", "source_row": 3}]
	assert api.ledger_export_matches(book, rows)
	for replace_range in (False, True):
		result = api.apply_batch(book.name, rows, "Import", 1, "own-export", opening="20.00",
			ledger_book=book.name, replace_range=replace_range)
		assert result["created"] == 0 and result["skipped"] == 1
		if replace_range:
			assert result["impact"]["new_closing"] == "22.25"
	retire.assert_not_called()
	register.assert_not_called()
	changed = [dict(rows[0], remark="modificado")]
	assert not api.ledger_export_matches(book, changed)
	with pytest.raises(ValueError, match="按日期覆盖"):
		api.apply_batch(book.name, changed, "Import", 1, "own-edited", ledger_book=book.name)
	with pytest.raises(ValueError, match="其他账簿"):
		api.apply_batch(book.name, rows, "Import", 1, "wrong-export", ledger_book="different-book")


def test_month_and_export_show_ledger_currency_with_original_amounts(foreign_ledger, monkeypatch):
	import frappe
	api, book, entry, _ = foreign_ledger
	api.save_row(book.name, dict(date="2024-01-05", income="12.25"), 0, "manual-test")
	monkeypatch.setattr(frappe.utils, "today", lambda: "2024-01-31")
	monkeypatch.setattr(api, "authorize_book", lambda *args: book)
	monkeypatch.setattr(api, "_rows", lambda *args: [api._wire(entry)])
	monkeypatch.setattr(api, "_prior", lambda *args: Decimal("100.00"))
	for period in (api.get_month(book.name, "2024-01"), api.export_rows(book.name)):
		assert period["currency"] == period["rows"][0]["currency"] == "MXN"
		assert period["opening"] == "100.00" and period["closing"] == "112.25"
