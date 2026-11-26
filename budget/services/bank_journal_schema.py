"""Extend the native cash journal only on sites which have the owning branding app."""

from contextlib import contextmanager
from contextvars import ContextVar

import frappe

ENTRY = "CN Cash Journal Entry"
BOOK_FIELD = "custom_budget_journal_book"
SEQ = "custom_budget_journal_sequence"
VOID = "custom_budget_journal_void"
EXTRA = "custom_budget_journal_details"
_managed = ContextVar("bank_journal_update", default=False)


def install():
	if not frappe.db.exists("DocType", ENTRY):
		return
	from frappe.custom.doctype.custom_field.custom_field import create_custom_fields

	fields = []
	for key, label, kind in (
		(BOOK_FIELD, "Bank journal book", "Data"),
		(SEQ, "Chronological tie breaker", "Int"),
		(VOID, "Canceled bank movement", "Check"),
		(EXTRA, "Bank journal source details", "Long Text"),
	):
		fields.append(
			dict(
				fieldname=key,
				label=label,
				fieldtype=kind,
				hidden=1,
				read_only=1,
				no_copy=1,
				print_hide=1,
				insert_after="account",
			)
		)
	create_custom_fields({ENTRY: fields})
	frappe.db.add_index(ENTRY, [BOOK_FIELD, VOID, "posting_date", SEQ], "budget_book_date")


@contextmanager
def managed():
	token = _managed.set(True)
	try:
		yield
	finally:
		_managed.reset(token)


def guard(doc, method=None):
	if _managed.get():
		return
	previous = doc.get_doc_before_save()
	old_book = previous.get(BOOK_FIELD) if previous else None
	if doc.get(BOOK_FIELD) or old_book or doc.get(VOID) or doc.get(EXTRA) or doc.get(SEQ):
		frappe.throw("请在银行日记账中修改或撤销该笔流水。", frappe.PermissionError)


def active_filter(user=None):
	# The native list also applies this when called from the old journal page.
	# During install the original DocType may exist before its extension fields.
	if not frappe.get_meta(ENTRY).has_field(VOID):
		return ""
	return f"COALESCE(`tab{ENTRY}`.`{VOID}`, 0) = 0"
