"""Native isolation scenario. Run with the bench env from its sites directory."""

from unittest.mock import patch

import frappe


def run():
	from budget.api import bank_journal as api
	from budget.services.bank_journal_schema import BOOK_FIELD, ENTRY

	if frappe.local.site != "bank-journal.qa" or frappe.conf.db_host != "dlbj-qa-1126-db":
		raise RuntimeError("This scenario requires the disposable bank-journal.qa database")
	company = frappe.db.get_value("Company", {"is_group": 0}, "name")
	if not company:
		fixture = frappe.get_doc(
			dict(
				doctype="Company",
				company_name="QA Journal Company",
				abbr="QAJ",
				default_currency="CNY",
				country="China",
				is_group=0,
			)
		)
		fixture.name = "QA Journal Company"
		fixture.db_insert()
		company = fixture.name
	if not frappe.db.exists("Currency", "CNY"):
		raise RuntimeError("QA site needs the native currency seed")
	account = "Synthetic QA " + frappe.generate_hash(length=10)
	b = api.create_book(company, account, "25.50", "2026-01-01")["name"]
	assert api.create_book(company, account, "25.50", "2026-01-01")["name"] == b
	another = api.create_book(company, account + " Other", "0", "2026-01-01")["name"]
	assert {b, another}.issubset({r["name"] for r in api.get_options()["books"]})
	assert api.get_month(b)["month"] == frappe.utils.today()[:7]
	legacy_count = api.get_options()["legacy_count"]
	native_legacy = frappe.get_doc(
		dict(
			doctype=ENTRY,
			company=company,
			currency="CNY",
			posting_date="2026-11-01",
			direction="income",
			amount="0.01",
			summary="Unassigned native entry",
		)
	).insert()
	assert api.get_options()["legacy_count"] == legacy_count + 1
	native_legacy.summary = "Original native edit"
	native_legacy.save()
	assert native_legacy.name in frappe.get_list(ENTRY, filters={"name": native_legacy.name}, pluck="name")
	native_legacy.delete()
	assert api.get_options()["legacy_count"] == legacy_count

	def row(date, side, amount, **more):
		return {"date": date, side: amount, "summary": "QA receipt", **more}

	def must_fail(func, *args):
		try:
			func(*args)
		except frappe.ValidationError, frappe.PermissionError, frappe.TimestampMismatchError, ValueError:
			return
		raise AssertionError("Expected a validation, permission or stale-version exception")

	add = row("2026-10-31", "income", "4.50")
	created = api.save_row(b, add, 0, "test-first")
	assert api.save_row(b, add, 0, "test-first") == created
	must_fail(api.save_row, b, dict(add, income="4.51"), 0, "test-first")
	assert created["version"] == 1
	pay = row("2026-11-02", "expense", "5.00", refs=["QA-1126"])
	applied = api.save_row(b, pay, 1, "test-second")
	assert api.book_ref_rows(b, ["QA-1126"])[0]["name"] == applied["name"]
	nov = api.get_month(b, "2026-11")
	assert nov["opening"] == "30.00" and nov["closing"] == "25.00" and len(nov["rows"]) == 1
	assert list(nov["rows"][0]["links"]) == ["QA-1126"]
	assert not api.get_month(another, "2026-11")["rows"]
	must_fail(api.save_row, b, dict(pay, expense="1", name=applied["name"]), 0, "test-stale")
	must_fail(api.cancel_row, another, applied["name"], 0, "test-cross-book")
	native = frappe.get_doc(ENTRY, applied["name"])
	native.summary = "Wrong surface"
	must_fail(native.save)
	edited = api.save_row(b, dict(pay, expense="6.00", name=applied["name"]), 2, "test-edit")
	assert api.get_month(b, "2026-11")["closing"] == "24.00"
	undone = api.undo(b, edited["operation"], 3, "test-undo")
	assert undone["version"] == 4 and api.get_month(b, "2026-11")["closing"] == "25.00"
	imported = [row("2026-11-08", "expense", "2.50", source_key="unique-bank-ref-1", balance="22.50")]
	# Failure after the native insertion and source binding must roll back both.
	bad = [dict(imported[0], balance="0.01")]
	must_fail(api.apply_batch, b, bad, "Import", 4, "test-import")
	assert not api.known_source_keys(b, "Import", ["unique-bank-ref-1"])
	raw = [""] * 14
	imported[0]["raw_cells"] = raw
	outcome = api.apply_batch(b, imported, "Import", 4, "test-import", template={"label": "QA"})
	assert outcome["created"] == 1
	exported = api.export_rows(b)
	assert exported["template"] == {"label": "QA"} and exported["rows"][-1]["raw_cells"] == raw
	assert exported["closing"] == "22.50"
	again = api.apply_batch(b, imported, "Import", 5, "test-reimport")
	assert again["skipped"] == 1 and again["version"] == 5
	saved = exported["rows"][-1]["name"]
	cancelled = api.cancel_row(b, saved, 5, "test-cancel")
	assert saved not in frappe.get_list(ENTRY, filters={BOOK_FIELD: b}, pluck="name")
	assert api.apply_batch(b, imported, "Import", 6, "test-cancelled-replay")["skipped"] == 1
	assert api.get_month(b, "2026-11")["closing"] == "25.00"
	api.undo(b, cancelled["operation"], 6, "test-restore")
	assert api.get_month(b, "2026-11")["closing"] == "22.50"
	assert api.get_month(b, "2026-12")["opening"] == "22.50"
	assert api.export_rows(b, "2026-11")["opening"] == "30.00"
	# A matching subsequent OA source links the existing payment, without
	# adding a duplicate expense to the account balance.
	oa = api.apply_batch(b, [dict(pay, source_key="instance-qa")], "DingTalk", 7, "test-oa-source")
	assert oa["created"] == 0 and oa["matched"] == 1 and api.get_month(b, "2026-11")["closing"] == "22.50"
	assert api.known_source_keys(b, "oa", ["instance-qa"]) == {"instance-qa"}
	second_cancel = api.cancel_row(b, saved, 8, "test-repeat-cancel")
	assert api.get_month(b, "2026-11")["closing"] == "25.00"
	api.undo(b, second_cancel["operation"], 9, "test-repeat-restore")
	assert api.get_month(b, "2026-11")["closing"] == "22.50"
	# First-import undo restores both the initialized book baseline and the
	# original file layout. A deliberate fresh import reapplies exactly once.
	bankline = row(
		"2026-11-08", "income", "2.00", source_key="bank-unique", bank_ref="TRANSFER-1", balance="2.00"
	)
	first_file = api.apply_batch(
		another, [bankline], "Import", 0, "test-bank-first", template={"sample": True}
	)
	must_fail(
		api.apply_batch,
		another,
		[dict(bankline, summary="changed identity")],
		"Import",
		1,
		"test-bank-change",
	)
	must_fail(api.apply_batch, another, [dict(bankline, balance="9.99")], "Import", 1, "test-bank-balance")
	assert api.get_month(another, "2026-11")["income"] == "2.00"
	api.undo(another, first_file["operation"], 1, "test-undo-first-file")
	reset = api.export_rows(another)
	assert (
		reset["opening"] == "0.00"
		and reset["closing"] == "0.00"
		and reset["rows"] == []
		and reset["template"] is None
	)
	readd = api.apply_batch(another, [bankline], "Import", 2, "test-import-again", template={"sample": True})
	assert readd["created"] == 1 and len(api.export_rows(another)["rows"]) == 1
	weak = row("2026-11-09", "expense", "0.10", source_key="weak-fee", weak_file_proof="file-a")
	api.apply_batch(another, [weak], "Import", 3, "test-weak-file")
	must_fail(
		api.apply_batch, another, [dict(weak, weak_file_proof="file-b")], "Import", 4, "test-weak-ambiguous"
	)
	# Book/company authorization is checked on every request, including receipt replay.
	with patch.object(api, "allowed_companies", return_value=set()):
		must_fail(api.get_month, b, "2026-11")
		must_fail(api.cancel_row, b, saved, 8, "test-permission")
	print(
		"NATIVE PASS: company/roles, bound dates and monthly balances, native create/edit/void/restore, "
		"same-key replay, rollback, manual and source associations, import export, alternate book isolation."
	)


if __name__ == "__main__":
	import os

	frappe.init(site="bank-journal.qa", sites_path=os.getcwd())
	try:
		frappe.connect()
		frappe.set_user("Administrator")
		from budget.api import bank_journal

		with patch.object(
			bank_journal,
			"_links",
			side_effect=lambda company, refs: {r: "https://aflow.dingtalk.com/?procInstId=qa" for r in refs},
		):
			run()
	finally:
		if getattr(frappe.local, "db", None):
			frappe.db.rollback()
		frappe.destroy()
