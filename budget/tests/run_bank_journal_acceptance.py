"""Native isolation scenario. Run with the bench env from its sites directory."""

import base64
from unittest.mock import patch

import frappe


def check_range_replacement(company):
	from budget.api import bank_journal as api, bank_journal_files as files
	from budget.services.bank_journal_schema import BOOK_FIELD, ENTRY

	account = "Synthetic interval " + frappe.generate_hash(length=8)
	book = api.create_book(company, account, "100.00", "2026-01-01")["name"]
	def save(day, side, amount, **extra):
		version = api.get_month(book, "2026-11")["version"]
		return api.save_row(book, dict(date="2026-11-"+day, summary="Manual "+day, **{side:amount}, **extra),
			version, "manual-before-"+day)

	def source_state():
		return frappe.get_all(api.ORIGIN, filters={"book":book}, fields=["name","entry","source_key","digest"],
			order_by="name", limit_page_length=0)

	save("04", "income", "10")
	save("11", "expense", "4", refs=["QA-OA-001"], project="原项目")
	save("20", "income", "1")
	save("21", "expense", "5")
	api.apply_batch(book, [dict(date="2026-11-11", expense="4", refs=["QA-OA-001"], source_key="qa-paid")],
		"DingTalk", 4, "prior-paid-binding")
	api.apply_batch(book, [dict(date="2026-11-05", income="9", bank_ref="QA-OLD-REF",
		source_key=files.formats._record_key("bank-reference", "QA-OLD-REF"))], "Import", 5, "prior-bank-binding")
	before = api.export_rows(book)
	sources = source_state()
	def csv(text):
		return base64.b64encode(text.encode()).decode()
	args = dict(book=book, filename="区间.csv", mode="replace_range", content_base64=csv(
		"日期,摘要,收入,支出,余额,银行流水号\n2026-11-05,新一,20,,130,QA-OLD-REF\n2026-11-20,新二,,8,122,QA-NEW-REF"))
	preview = files.preview_import(**args, review_offset=0)
	expected = dict(start="2026-11-05", end="2026-11-20", old_count=3, new_count=2, opening="110.00",
		old_closing="116.00", new_closing="122.00", difference="6.00")
	assert preview["impact"] == expected and preview["preview_id"] and api.export_rows(book) == before
	request = dict(**args, preview_id=preview["preview_id"], version=preview["version"], request_id="interval-apply")
	saved_count = frappe.db.count(ENTRY, {BOOK_FIELD:book})
	with patch.object(api, "_finish", side_effect=RuntimeError("after all writes")):
		try:
			files.apply_import(**request)
		except RuntimeError as exc:
			assert str(exc) == "after all writes"
		else:
			raise AssertionError("transaction failure was not injected")
	assert source_state() == sources and api.export_rows(book) == before
	assert frappe.db.count(ENTRY, {BOOK_FIELD:book}) == saved_count
	receipt = files.apply_import(**request)
	assert receipt["impact"] == expected and receipt["created"] == 2 and files.apply_import(**request) == receipt
	assert api.known_source_keys(book, "oa", ["qa-paid"]) == {"qa-paid"}
	current = api.export_rows(book)
	assert [r["date"][-2:] for r in current["rows"]] == ["04","05","20","21"]
	assert current["rows"][0] == before["rows"][0] and current["rows"][-1]["name"] == before["rows"][-1]["name"]
	assert current["closing"] == "117.00" and api.get_month(book,"2026-12")["opening"] == "117.00"
	assert api.get_month(book, "2026-11")["undo_range"] == expected
	with patch.object(api, "is_import_user", return_value=True):
		try:
			files.apply_import(**request)
		except frappe.PermissionError:
			pass
		else:
			raise AssertionError("import token could replace a book")
	later = save("22", "income", "1")
	try:
		api.undo(book, receipt["operation"], later["version"], "old-undo-blocked")
	except frappe.ValidationError:
		pass
	else:
		raise AssertionError("later changes were overwritten by undo")
	popped = api.undo(book, later["operation"], later["version"], "undo-later-row")
	restored = api.undo(book, receipt["operation"], popped["version"], "undo-interval")
	assert restored == api.undo(book, receipt["operation"], popped["version"], "undo-interval")
	after = api.export_rows(book)
	assert after == dict(before, version=restored["version"]) and source_state() == sources
	assert api.get_month(book, "2026-12")["opening"] == "111.00"
	assert api.book_ref_rows(book, ["QA-OA-001"])[0]["links"]
	assert files.apply_import(**request) == receipt and api.export_rows(book) == after


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
	check_range_replacement(company)


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
