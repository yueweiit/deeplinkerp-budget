"""预算台账（只读副本）。

数据由 ``budget.services.budget_ledger_sync`` 从外部 ``budget_system`` 库同步进来，
不在 ERP 里手工维护，所以禁用手工新增/编辑入口。
"""

from __future__ import annotations

from frappe.model.document import Document


class BudgetLedgerEntry(Document):
	def validate(self) -> None:
		if self.is_new():
			return
		self.flags.ignore_links = True

	def on_update(self) -> None:
		# 同步任务用 db.set_value/db.insert 写入，不走这里；
		# 若有人从界面改了，记录一条日志便于排查。
		pass
