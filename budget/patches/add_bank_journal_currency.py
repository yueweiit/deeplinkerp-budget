"""Add the book currency with a CNY database default for all pre-existing books."""

import frappe


def execute():
	frappe.reload_doc("budget", "doctype", "cn_bank_journal_book")
