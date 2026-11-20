"""Add server-owned template history state; leave legacy history unknown."""
import frappe


def execute():
	frappe.reload_doc("budget", "doctype", "cn_approval_template")
