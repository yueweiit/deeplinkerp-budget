"""预算额度配置接口（只读）。

对齐 ``dingtalk-budget`` 的 ``/api/quota-configurations`` 系列：原项目只允许超管
访问，这里同样收口到 System Manager。
"""

from __future__ import annotations

import frappe

from ..integrations.budget_factory import get_quota_source

READ_ROLES = ("System Manager",)


def get_source():
	"""数据源由 ``budget_data_source`` 决定，默认读本地表（由 ``quota_sync`` 同步）。"""
	source = getattr(frappe.local, "budget_quota_source", None)
	if source is None:
		source = get_quota_source()
		frappe.local.budget_quota_source = source
	return source


def _filters(
	budget_year=None,
	budget_month=None,
	group_dept_id=None,
	company_dept_id=None,
	department_id=None,
	budget_type=None,
	subject_name=None,
) -> dict:
	return {
		"budget_year": (str(budget_year or "").strip() or None),
		"budget_month": (str(budget_month or "").strip() or None),
		"group_dept_id": (str(group_dept_id or "").strip() or None),
		"company_dept_id": (str(company_dept_id or "").strip() or None),
		"department_id": (str(department_id or "").strip() or None),
		"budget_type": (str(budget_type or "").strip() or None),
		"subject_name": (str(subject_name or "").strip() or None),
	}


@frappe.whitelist()
def get_quota_options() -> dict:
	"""筛选下拉候选值（年度 / 月份 / 集团 / 子公司 / 部门 / 科目 / 预算类型）。"""
	frappe.only_for(READ_ROLES)
	return get_source().get_options()


@frappe.whitelist()
def get_quota_summary(
	budget_year=None,
	budget_month=None,
	group_dept_id=None,
	company_dept_id=None,
	department_id=None,
	budget_type=None,
	subject_name=None,
) -> dict:
	"""4 张统计卡 + 组织额度汇总（集团 / 子公司 / 部门 / 科目）。"""
	frappe.only_for(READ_ROLES)
	return get_source().get_summary(
		**_filters(
			budget_year,
			budget_month,
			group_dept_id,
			company_dept_id,
			department_id,
			budget_type,
			subject_name,
		)
	)


@frappe.whitelist()
def list_quotas(
	budget_year=None,
	budget_month=None,
	group_dept_id=None,
	company_dept_id=None,
	department_id=None,
	budget_type=None,
	subject_name=None,
	page: int = 1,
	page_size: int = 20,
) -> dict:
	"""预算额度明细分页。"""
	frappe.only_for(READ_ROLES)
	return get_source().list_quotas(
		page=int(page or 1),
		page_size=int(page_size or 20),
		**_filters(
			budget_year,
			budget_month,
			group_dept_id,
			company_dept_id,
			department_id,
			budget_type,
			subject_name,
		),
	)
