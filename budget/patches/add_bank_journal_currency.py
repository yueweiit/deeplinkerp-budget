"""Add book currency and bank-balance offset with legacy defaults CNY and zero."""

import frappe


def execute():
	frappe.reload_doc("budget", "doctype", "cn_bank_journal_book")
