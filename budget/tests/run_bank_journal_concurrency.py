"""Real concurrent database sessions on the disposable bank journal acceptance site."""

import os
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor

import frappe

from budget.api import bank_journal as api
from budget.services.bank_journal_schema import BOOK_FIELD, ENTRY

frappe.init(site="bank-journal.qa", sites_path=os.getcwd())
frappe.connect()
frappe.set_user("Administrator")
if frappe.conf.db_host != "dlbj-qa-1126-db":
	raise RuntimeError("Native concurrency requires the isolated QA database")
try:
	if len(sys.argv) > 1:
		b, key, version, amount = sys.argv[1:]
		try:
			r = api.save_row(
				b, {"date": "2026-11-03", "income": amount, "summary": "parallel smoke"}, version, key
			)
			frappe.db.commit()
			print("OK", r["name"])
		except frappe.TimestampMismatchError:
			frappe.db.rollback()
			print("STALE")
	else:
		company = frappe.db.get_value("Company", {"is_group": 0}, "name")
		transient = None
		if not company:
			transient = company = "QA concurrency " + frappe.generate_hash(length=8)
			c = frappe.get_doc(
				dict(
					doctype="Company",
					company_name=company,
					abbr="QAC",
					default_currency="CNY",
					country="China",
					is_group=0,
				)
			)
			c.name = company
			c.db_insert()
		b = api.create_book(
			company, "QA concurrency " + frappe.generate_hash(length=10), "0.00", "2026-01-01"
		)["name"]
		frappe.db.commit()

		def race(payload):
			res = subprocess.run([sys.executable, __file__, b, *payload], capture_output=True, text=True)
			if res.returncode:
				raise RuntimeError(res.stderr)
			return res.stdout.strip()

		try:
			with ThreadPoolExecutor(2) as ex:
				dup = list(ex.map(race, [("same-request", "0", "45.12")] * 2))
				assert len(set(dup)) == 1 and dup[0].startswith("OK "), dup
				conflicting = list(
					ex.map(
						race, [("different-request-a", "1", "10.00"), ("different-request-b", "1", "20.00")]
					)
				)
				assert (
					sum(m == "STALE" for m in conflicting) == 1
					and sum(m.startswith("OK ") for m in conflicting) == 1
				), conflicting
			frappe.db.rollback()
			r = api.get_month(b, "2026-11")
			assert r["version"] == 2 and len(r["rows"]) == 2 and r["closing"] in ("55.12", "65.12")
			print(
				"NATIVE PASS concurrent identical operation gives one native entry and one receipt, version conflicts produce one winner."
			)
		finally:
			for dt in (api.OP, api.ORIGIN, "CN Bank Journal Reference"):
				frappe.db.delete(dt, {"book": b})
			frappe.db.delete(ENTRY, {BOOK_FIELD: b})
			frappe.db.delete(api.BOOK, {"name": b})
			if transient:
				frappe.db.delete("Company", {"name": transient})
			frappe.db.commit()
finally:
	frappe.destroy()
