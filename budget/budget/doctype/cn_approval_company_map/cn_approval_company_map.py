from frappe.model.document import Document


class CNApprovalCompanyMap(Document):
	def validate(self):
		from budget.services.cn_approval_settings import validate_managed_document
		validate_managed_document(self)

	on_trash = validate

	def before_rename(self, *args, **kwargs):
		self.validate()
