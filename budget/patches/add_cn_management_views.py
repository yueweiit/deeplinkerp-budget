"""Add management metadata without rewriting approval or accounting history."""
import frappe


def execute():
	frappe.reload_doc("budget", "doctype", "cn_approval_settings")
	for page in ("china_budget_execution", "china_management_reports"):
		frappe.reload_doc("budget", "page", page)
