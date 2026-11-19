"""可视化报表接口（只读）。

对应原项目 ``GET /api/report``：一次性返回统计卡、月度趋势、部门执行率、
部门预算 vs 实际支出与审批效率。聚合口径见 ``services/report_service.py``。
"""

from __future__ import annotations

import frappe
from frappe.utils import get_first_day, get_last_day, getdate

from ..services.report_service import build_report

READ_ROLES = ("System Manager", "Accounts Manager")


@frappe.whitelist()
def get_report_data(start_date: str | None = None, end_date: str | None = None, company: str | None = None) -> dict:
	"""区间报表；不传日期时按当月。"""
	frappe.only_for(READ_ROLES)
	today = getdate()
	start = getdate(start_date) if start_date else get_first_day(today)
	end = getdate(end_date) if end_date else get_last_day(today)
	if start > end:
		frappe.throw(frappe._("Start date must not be after end date"))
	if company:
		from .cn_workbench import _check
		_check()
	return build_report(start.isoformat(), end.isoformat(), company=company)
