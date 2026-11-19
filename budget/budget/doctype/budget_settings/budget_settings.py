"""预算台账的设置页：维护列表筛选下拉的候选值（子公司 / 部门 / 状态 / 执行地区）。"""

from __future__ import annotations

from frappe.model.document import Document


class BudgetSettings(Document):
	def _save(self, *args, **kwargs):
		from budget.services.ledger_sync_settings import acquire_settings_transaction_lock

		acquire_settings_transaction_lock()
		return super()._save(*args, **kwargs)

	def insert(self, *args, **kwargs):
		from budget.services.ledger_sync_settings import acquire_settings_transaction_lock

		acquire_settings_transaction_lock()
		return super().insert(*args, **kwargs)

	def validate(self):
		from budget.services.ledger_sync_settings import validate_settings

		validate_settings(self)

	def on_update(self):
		from budget.services.organization_mapping import apply_changes

		apply_changes(self)
