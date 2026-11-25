import frappe
from frappe import _
from frappe.model.document import Document


class ExpenseReportState(Document):
	def validate(self):
		frappe.throw(_("Expense reporting records are maintained automatically."), frappe.PermissionError)

	def on_trash(self):
		self.validate()
