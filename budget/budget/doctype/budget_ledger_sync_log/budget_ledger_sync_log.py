from __future__ import annotations

from frappe.model.document import Document


class BudgetLedgerSyncLog(Document):
	"""台账同步的运行记录：只由 ``oa_budget_sync`` 写入，页面只读。

	为什么不只用缓存：缓存重启就没了，出错时没人知道上一轮是什么时候、失败在什么阶段。
	这条表也充当「正在同步」的可见状态（插入后立即提交，失败回滚时不受影响）。
	"""

	pass
