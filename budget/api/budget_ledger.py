"""预算台账查询接口（只读）。

命名契约与 ``api/approval.py`` 一致：``get_`` / ``list_`` 无副作用，前端可安全重试。
"""

from __future__ import annotations

import calendar
from datetime import UTC, date, datetime, timezone
from typing import Any
from urllib.parse import quote

import frappe

from ..integrations.budget_factory import data_source_mode, get_expense_source, get_ledger_source
from ..integrations.oa_approval_source import _runtime_config_value
from ..services.budget_ledger_sync import sync_budget_entries as run_sync

READ_ROLES = ("System Manager", "Accounts Manager")
LEDGER_DOCTYPE = "Budget Ledger Entry"
EXPORT_LIMIT = 20000

# 台账 sheet 的列：与列表视图一致；第三项是列宽
EXPORT_COLUMNS = [
	("form_no", "Form No", 24),
	("company", "Company", 30),
	("department", "Department", 40),
	("budget_kind", "Budget Kind", 16),
	("application_date", "Application Date", 14),
	("budget_month", "Budget Month", 12),
	("execution_region", "Execution Region", 16),
	("status", "Status", 16),
	("amount", "Budget Amount", 16),
]

EXPENSE_DETAIL_HEADERS = [
	"No.",
	"Department",
	"Budget Ownership",
	"Budget Type",
	"Application Date",
	"Budget Month",
	"Budget Item",
	"Budget Amount",
	"Currency",
	"Detail Item",
	"Detail Amount",
	"Calculation Basis",
	"Form No",
	"Status",
	"Synced At",
]
EXPENSE_DETAIL_WIDTHS = [6, 26, 16, 20, 14, 12, 16, 14, 10, 26, 14, 26, 24, 12, 18]

PRODUCTION_DETAIL_HEADERS = [
	"No.",
	"Department",
	"Budget Ownership",
	"Budget Type",
	"Application Date",
	"Budget Period",
	"Budget Category",
	"Detail Category",
	"Code / Expense Detail",
	"Specification / Line / Position",
	"Unit",
	"Unit Price",
	"Overtime Hours",
	"Overtime Rate",
	"Estimated Overtime Amount",
	"Monthly Budget Total",
	"Requested Quantity",
	"Requested Amount",
	"Previous Used",
	"Purpose",
	"Operator",
	"Remark",
	"Form No",
]
PRODUCTION_DETAIL_WIDTHS = [
	6, 26, 16, 14, 14, 12, 16, 16, 26, 30, 8, 12, 12, 12, 16, 16, 14, 16, 12, 24, 14, 24, 24,
]

EXECUTION_HEADERS = [
	"No.",
	"Department",
	"Budget Ownership",
	"Month",
	"Production Budget",
	"Non Production Budget",
	"Total Budget",
]
EXECUTION_WIDTHS = [6, 34, 16, 10, 18, 20, 18]

SUMMARY_HEADERS = ["Metric", "Figure"]
SUMMARY_WIDTHS = [40, 20]

_DETAIL_GROUP_LABELS = {
	"hr": "HR Budget",
	"office": "Office Budget",
	"operation": "Operation Budget",
	"material": "Material Budget",
	"production": "Production Budget",
	"labor": "Labor Budget",
}


def get_source():
	"""同一请求内复用数据源实例；默认读站点本地表（由 OA 归档同步而来）。"""
	source = getattr(frappe.local, "budget_ledger_source", None)
	if source is None:
		source = get_ledger_source()
		frappe.local.budget_ledger_source = source
	return source


def get_expense() -> Any:
	"""同一请求内复用支出数据源实例。"""
	source = getattr(frappe.local, "budget_expense_read_source", None)
	if source is None:
		source = get_expense_source()
		frappe.local.budget_expense_read_source = source
	return source


@frappe.whitelist()
def list_budget_entries(
	budget_type: str | None = None,
	start_month: str | None = None,
	end_month: str | None = None,
	status: str | None = None,
	keyword: str | None = None,
	page: int = 1,
	page_size: int = 10,
) -> dict:
	"""预算列表：``budget_type`` 取 ``production`` / ``non_production`` / 空（全部）。"""
	frappe.only_for(READ_ROLES)
	kind = (budget_type or "").strip() or None
	if kind not in (None, "production", "non_production"):
		frappe.throw("budget_type 只能是 production、non_production 或留空")
	return get_source().list_budget_entries(
		budget_type=kind,
		start_month=(start_month or "").strip() or None,
		end_month=(end_month or "").strip() or None,
		status=(status or "").strip() or None,
		keyword=(keyword or "").strip() or None,
		page=int(page or 1),
		page_size=int(page_size or 10),
	)


@frappe.whitelist()
def get_budget_stats() -> dict:
	"""顶部四个统计卡片。"""
	frappe.only_for(READ_ROLES)
	return get_source().get_stats()


# ---------------------------------------------------------------- 导出


def _cell_value(row: dict, fieldname: str):
	value = row.get(fieldname)
	if fieldname == "budget_kind":
		if value == "production":
			return frappe._("Production Budget")
		if value == "non_production":
			return frappe._("Non Production Budget")
		return ""
	if fieldname == "budget_month":
		# 只到月份
		return str(value)[:7] if value else ""
	if fieldname == "application_date" and hasattr(value, "date"):
		return value.date()
	if fieldname == "amount":
		return float(value or 0)
	return value if value is not None else ""


def _write_sheet(
	workbook,
	title: str,
	headers: list[str],
	rows: list[list],
	widths: list[int],
	*,
	money_columns: tuple[int, ...] = (),
	number_columns: tuple[int, ...] = (),
	date_columns: tuple[int, ...] = (),
	wrap_columns: tuple[int, ...] = (),
):
	from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
	from openpyxl.utils import get_column_letter

	sheet = workbook.create_sheet(title=str(title)[:31])
	sheet.append(headers)

	header_fill = PatternFill("solid", fgColor="DCE7F4")
	header_font = Font(bold=True, color="294F76")
	thin = Side(style="thin", color="D1D8DD")
	border = Border(left=thin, right=thin, top=thin, bottom=thin)

	for index, width in enumerate(widths, start=1):
		cell = sheet.cell(row=1, column=index)
		cell.fill = header_fill
		cell.font = header_font
		cell.alignment = Alignment(horizontal="center", vertical="center", wrap_text=True)
		cell.border = border
		sheet.column_dimensions[get_column_letter(index)].width = width
	sheet.row_dimensions[1].height = 22

	for row in rows:
		sheet.append(list(row))

	last_row = sheet.max_row
	for row_index in range(2, last_row + 1):
		for column_index in range(1, len(headers) + 1):
			cell = sheet.cell(row=row_index, column=column_index)
			cell.border = border
			cell.alignment = Alignment(
				vertical="center", wrap_text=column_index in wrap_columns
			)
			if column_index in money_columns:
				cell.number_format = "#,##0.00"
			elif column_index in number_columns:
				cell.number_format = "0.##"
			elif column_index in date_columns:
				cell.number_format = "yyyy-mm-dd"

	sheet.freeze_panes = "A2"
	if last_row > 1:
		sheet.auto_filter.ref = f"A1:{get_column_letter(len(headers))}{last_row}"
	return sheet


def _expense_detail_rows(ledger: dict, details: list[dict]) -> list[list]:
	rows = []
	for index, detail in enumerate(details, start=1):
		parent = ledger.get(str(detail.get("form_no") or ""))
		if parent is None:
			continue
		rows.append(
			[
				index,
				parent.get("department") or "",
				frappe._("Own department budget"),
				frappe._("Non Production Budget"),
				_cell_value(parent, "application_date"),
				str(parent.get("budget_month") or "")[:7],
				frappe._(_DETAIL_GROUP_LABELS.get(detail.get("detail_group"), "")),
				float(detail.get("original_amount") or 0),
				detail.get("currency") or "CNY",
				detail.get("detail_item")
				or detail.get("budget_purpose_detail")
				or detail.get("budget_detail")
				or "",
				float(detail.get("amount") or 0),
				detail.get("calculation_basis") or "",
				detail.get("form_no") or "",
				parent.get("status") or "",
				parent.get("synced_at") or "",
			]
		)
	return rows


def _production_detail_rows(ledger: dict, details: list[dict]) -> list[list]:
	rows = []
	for index, detail in enumerate(details, start=1):
		parent = ledger.get(str(detail.get("form_no") or ""))
		if parent is None:
			continue
		rows.append(
			[
				index,
				parent.get("department") or "",
				frappe._("Own department budget"),
				frappe._("Production Budget"),
				_cell_value(parent, "application_date"),
				str(parent.get("budget_month") or "")[:7],
				frappe._(_DETAIL_GROUP_LABELS.get(detail.get("detail_group"), "")),
				detail.get("detail_category") or "",
				detail.get("detail_code") or detail.get("item_name") or "",
				detail.get("specification")
				or detail.get("production_line")
				or detail.get("post_name")
				or detail.get("product_name")
				or "",
				detail.get("unit") or "",
				detail.get("unit_price"),
				detail.get("overtime_hours"),
				detail.get("overtime_unit_price"),
				detail.get("estimated_overtime_amount"),
				detail.get("quantity"),
				detail.get("quantity"),
				detail.get("amount"),
				detail.get("original_amount"),
				detail.get("calculation_basis") or "",
				detail.get("work_type") or "",
				detail.get("remark") or "",
				detail.get("form_no") or "",
			]
		)
	return rows


def _execution_rows(rows: list[dict]) -> list[list]:
	aggregated: dict[tuple[str, str], dict[str, float]] = {}
	for row in rows:
		key = (
			str(row.get("department") or "").strip(),
			str(row.get("budget_month") or "")[:7],
		)
		bucket = aggregated.setdefault(key, {"production": 0.0, "non_production": 0.0})
		kind = "production" if row.get("budget_kind") == "production" else "non_production"
		bucket[kind] += float(row.get("amount") or 0)

	result = []
	for index, ((department, month), bucket) in enumerate(
		sorted(aggregated.items(), key=lambda item: (item[0][1], item[0][0])), start=1
	):
		result.append(
			[
				index,
				department,
				frappe._("Own department budget"),
				month,
				round(bucket["production"], 2),
				round(bucket["non_production"], 2),
				round(bucket["production"] + bucket["non_production"], 2),
			]
		)
	return result


def _summary_rows(
	rows: list[dict], expense_rows: list[list], production_rows: list[list], execution: list[list]
) -> list[list]:
	production_orders = sum(1 for row in rows if row.get("budget_kind") == "production")
	non_production_orders = len(rows) - production_orders
	production_total = sum(
		float(row.get("amount") or 0) for row in rows if row.get("budget_kind") == "production"
	)
	non_production_total = sum(
		float(row.get("amount") or 0) for row in rows if row.get("budget_kind") != "production"
	)
	return [
		[frappe._("Production Budget Orders"), production_orders],
		[frappe._("Non Production Budget Orders"), non_production_orders],
		[frappe._("Production Detail Rows"), len(production_rows)],
		[frappe._("Non Production Detail Rows"), len(expense_rows)],
		[frappe._("Execution Rows"), len(execution)],
		[frappe._("Production Budget Amount"), round(production_total, 2)],
		[frappe._("Non Production Budget Amount"), round(non_production_total, 2)],
		[frappe._("Total Budget Amount"), round(production_total + non_production_total, 2)],
		[frappe._("Exported At"), frappe.utils.now()],
	]


def _build_workbook(
	rows: list[dict], expense_details: list[dict], production_details: list[dict]
) -> bytes:
	"""多 sheet 报表：台账 / 非生产明细 / 生产明细 / 执行情况 / 汇总。"""
	from io import BytesIO

	from openpyxl import Workbook

	workbook = Workbook()
	workbook.remove(workbook.active)

	# 1. 台账
	_write_sheet(
		workbook,
		frappe._("Budget Ledger"),
		[frappe._(label) for _field, label, _width in EXPORT_COLUMNS],
		[[_cell_value(row, field) for field, _label, _width in EXPORT_COLUMNS] for row in rows],
		[width for _field, _label, width in EXPORT_COLUMNS],
		money_columns=(9,),
		date_columns=(5,),
		wrap_columns=(2, 3),
	)

	ledger = {str(row.get("form_no") or ""): row for row in rows}
	expense_rows = _expense_detail_rows(ledger, expense_details)
	production_rows = _production_detail_rows(ledger, production_details)
	execution_rows = _execution_rows(rows)

	# 2. 非生产预算明细
	_write_sheet(
		workbook,
		frappe._("Non Production Details"),
		[frappe._(header) for header in EXPENSE_DETAIL_HEADERS],
		expense_rows,
		EXPENSE_DETAIL_WIDTHS,
		money_columns=(8, 11),
		date_columns=(5, 15),
		wrap_columns=(2, 10, 12),
	)

	# 3. 生产预算明细
	_write_sheet(
		workbook,
		frappe._("Production Details"),
		[frappe._(header) for header in PRODUCTION_DETAIL_HEADERS],
		production_rows,
		PRODUCTION_DETAIL_WIDTHS,
		money_columns=(12, 14, 15, 16, 18, 19),
		number_columns=(13, 17),
		date_columns=(5,),
		wrap_columns=(2, 9, 10, 20, 22),
	)

	# 4. 执行情况（按部门 + 月份）
	_write_sheet(
		workbook,
		frappe._("Budget Execution"),
		[frappe._(header) for header in EXECUTION_HEADERS],
		execution_rows,
		EXECUTION_WIDTHS,
		money_columns=(5, 6, 7),
		wrap_columns=(2,),
	)

	# 5. 汇总
	_write_sheet(
		workbook,
		frappe._("Summary"),
		[frappe._(header) for header in SUMMARY_HEADERS],
		_summary_rows(rows, expense_rows, production_rows, execution_rows),
		SUMMARY_WIDTHS,
		money_columns=(2,),
	)

	buffer = BytesIO()
	workbook.save(buffer)
	return buffer.getvalue()


@frappe.whitelist()
def export_budget_entries_xlsx(filters: str | None = None) -> None:
	"""按当前列表筛选导出 xlsx（台账 + 明细 + 执行情况 + 汇总）。"""
	frappe.only_for(READ_ROLES)
	parsed = frappe.parse_json(filters) if filters else []
	rows = frappe.get_list(
		LEDGER_DOCTYPE,
		filters=parsed or None,
		fields=[
			"form_no",
			"company",
			"department",
			"budget_kind",
			"application_date",
			"budget_month",
			"execution_region",
			"status",
			"amount",
			"synced_at",
		],
		order_by="application_date desc, creation desc",
		limit_page_length=EXPORT_LIMIT,
	)

	source = get_source()
	form_nos = [str(row.get("form_no") or "") for row in rows]
	try:
		expense_details = source.list_expense_details(form_nos)
		production_details = source.list_production_details(form_nos)
	except Exception:
		# 明细库读不到时仍导出台账，避免整份报表失败
		frappe.log_error(
			message=frappe.get_traceback(), title="Budget report detail fetch failed"
		)
		expense_details, production_details = [], []

	frappe.response["filename"] = f"{frappe._('Budget Ledger')}_{frappe.utils.nowdate()}.xlsx"
	frappe.response["filecontent"] = _build_workbook(rows, expense_details, production_details)
	frappe.response["type"] = "binary"


@frappe.whitelist()
def sync_budget_entries() -> dict:
	"""手动触发一次全量同步（写入 Budget Ledger Entry）。"""
	frappe.only_for(("System Manager",))
	return run_sync()


# ---------------------------------------------------------------- 详情
# 对齐 dingtalk-budget 的 BudgetList 详情弹窗：
# 预算拆 operation/hr/office，支出拆 management/salary/bonus/officeEquipment/office/tax。
EXPENSE_KEYS = ("management", "salary", "bonus", "officeEquipment", "office", "tax")

# 支出类型为空时（如付款事件行）用分区名兜底，避免明细表出现空白单元格
EXPENSE_SECTION_LABELS = {
	"management": "运营/采购支出",
	"salary": "工资/社保",
	"bonus": "备用金",
	"officeEquipment": "办公设备",
	"office": "办公场地",
	"tax": "个税",
}


def _month_bounds(value: str) -> tuple[str, str]:
	"""``YYYY-MM`` → 当月首末日，供支出按审批完成时间过滤。"""
	text = str(value or "")[:7]
	if len(text) != 7 or text[4] != "-":
		today = date.today()
		return today.replace(day=1).isoformat(), today.isoformat()
	year, month = int(text[:4]), int(text[5:7])
	last_day = calendar.monthrange(year, month)[1]
	return date(year, month, 1).isoformat(), date(year, month, last_day).isoformat()


def _utc_minute(value) -> str:
	"""时间统一按 UTC 输出 ``YYYY-MM-DD HH:mm``，对齐源项目 ``formatUtcDateTime``。"""
	if not value:
		return ""
	if isinstance(value, datetime):
		moment = value.astimezone(UTC) if value.tzinfo else value.replace(
			tzinfo=UTC
		)
		return moment.strftime("%Y-%m-%d %H:%M")
	return str(value).strip()


def _department_matches(budget_department: str, expense_department: str) -> bool:
	"""台账部门是带路径的全名，支出部门是短名，按包含关系匹配。"""
	left = str(budget_department or "").strip()
	right = str(expense_department or "").strip()
	if not left or not right:
		return False
	return left == right or right in left or left in right


# 部门拆分的 split_type → 分区（对齐源项目 sectionKeyForSplit）
SPLIT_SECTION_MAP = {
	"salary": "salary",
	"social_insurance": "salary",
	"bonus": "bonus",
	"office_equipment": "officeEquipment",
	"office_space": "office",
	"individual_income_tax": "tax",
	"it_operation": "management",
	"manual_company_allocation": "management",
}


def _expense_bucket(category: str, expense_type: str, split_type: str = "") -> str:
	"""金额汇总桶；``split_type`` 优先，关键词只作兜底。"""
	kind = str(split_type or "").strip().lower()
	if kind in SPLIT_SECTION_MAP:
		return SPLIT_SECTION_MAP[kind]
	if kind == "administrative":
		# 行政管理费并入「管理支出」，但明细分区按类别单独展示
		return "management"
	text = f"{category} {expense_type}"
	if "个税" in text or "个人所得税" in text:
		return "tax"
	if "工资" in text or "社保" in text or "公积金" in text or "薪" in text:
		return "salary"
	if "备用金" in text or "奖金" in text:
		return "bonus"
	if "办公设备" in text:
		return "officeEquipment"
	if "办公场地" in text or "租金" in text:
		return "office"
	return "management"


def _expense_section(category: str, expense_type: str, split_type: str) -> tuple[str, str]:
	"""明细分区：返回 ``(section_key, section_title)``。

	``administrative`` 拆分成独立分区（源项目 ``administrative:<类别>``），
	避免把管理费用混进「运营/采购支出明细」。
	"""
	kind = str(split_type or "").strip().lower()
	if kind == "administrative":
		label = str(category or "").strip() or "管理费用"
		return f"administrative:{label}", f"{label}明细"
	bucket = _expense_bucket(category, expense_type, split_type)
	label = EXPENSE_SECTION_LABELS.get(bucket, "")
	return bucket, f"{label}明细"


def _payment_event_label(accounting_source: str, evidence: str = "") -> str:
	"""付款事件列：与源项目 ``paymentEventLabel`` 的文案保持一致。"""
	source = accounting_source.strip().lower()
	if source.startswith("payment_event"):
		return "已全额抵扣" if "已全额抵扣" in (evidence or "") else "实际付款"
	if source in ("completed_approval_fallback", "purchase_completed"):
		return "审批完成兜底"
	return "-"


def _dingtalk_links(process_instance_id: str) -> dict:
	"""PC 免登链接 + 钉钉客户端 scheme（原项目同款拼接）。"""
	instance_id = str(process_instance_id or "").strip()
	if not instance_id:
		return {"pc_url": "", "client_url": ""}
	pc_url = (
		"https://aflow.dingtalk.com/dingtalk/mobile/homepage.htm"
		f"?showmenu=false&dd_progress=false#/approval?procInstId={instance_id}"
	)
	return {
		"pc_url": pc_url,
		"client_url": f"dingtalk://dingtalkclient/page/link?url={quote(pc_url, safe='')}&pc_slide=true",
	}


@frappe.whitelist()
def get_budget_entry_detail(form_no: str) -> dict:
	"""预算单详情：台账字段 + 预算明细 + 实际支出 + 钉钉原单链接。"""
	frappe.only_for(READ_ROLES)
	if not form_no or not frappe.db.exists(LEDGER_DOCTYPE, form_no):
		frappe.throw("预算单不存在")

	entry = frappe.get_doc(LEDGER_DOCTYPE, form_no).as_dict()
	month = str(entry.get("budget_month") or "")[:7]

	source = get_source()
	# 详情「备注」沿用预算单原始备注（台账未落库，按单据号现查）
	try:
		entry["remark"] = source.load_remarks([str(form_no)]).get(str(form_no), "")
	except Exception:  # 备注取不到不影响其余信息
		entry["remark"] = ""

	budget = {"operation": 0.0, "hr": 0.0, "office": 0.0, "production": 0.0}
	try:
		for detail in source.list_expense_details([form_no]):
			group = str(detail.get("detail_group") or "")
			if group in budget:
				budget[group] += float(detail.get("amount") or 0)
		for detail in source.list_production_details([form_no]):
			budget["production"] += float(detail.get("amount") or 0)
	except Exception:  # 明细表不可用时仍返回台账信息
		frappe.log_error(message=frappe.get_traceback(), title="Budget detail rows failed")

	budget["total"] = budget["operation"] + budget["hr"] + budget["office"] + budget["production"]
	if not budget["total"]:
		budget["total"] = float(entry.get("amount") or 0)
		if not any(budget[key] for key in ("operation", "hr", "office", "production")):
			budget["operation"] = budget["total"]

	expense = {key: 0.0 for key in EXPENSE_KEYS}
	expense_details: list[dict] = []
	expense_available = bool(month)
	warning = ""
	if month:
		start_date, end_date = _month_bounds(month)
		try:
			rows = get_expense().list_approved_expenses(start_date, end_date)
			matched_rows = [
				row
				for row in rows
				if _department_matches(entry.get("department"), row.get("department"))
			]
			# 「事项/说明」「付款证据」需要额外查审批表，失败只影响这两列
			detail_meta: dict[str, dict] = {}
			try:
				detail_meta = get_expense().load_detail_meta(
					[str(row.get("business_id") or "") for row in matched_rows]
				)
			except Exception:  # 说明缺失不影响金额
				detail_meta = {}
			for row in matched_rows:
				amount = float(row.get("amount") or 0)
				category = str(row.get("category") or "").strip()
				expense_type = str(row.get("expense_type") or "").strip()
				split_type = str(row.get("split_type") or "").strip()
				bucket = _expense_bucket(category, expense_type, split_type)
				section, section_title = _expense_section(category, expense_type, split_type)
				expense[bucket] += amount
				business_id = str(row.get("business_id") or "").strip()
				meta = detail_meta.get(business_id) or {}
				accounting_source = str(row.get("accounting_source") or "")
				# 日期列：付款类行取付款时间，其余取审批完成时间（与源项目一致）
				is_payment = accounting_source.startswith("payment_event") or (
					accounting_source == "monthly_settlement"
				)
				detail_date = _utc_minute(
					meta.get("paid_at") if is_payment else meta.get("completed_at")
				)
				expense_details.append(
					{
						"section": section,
						"section_title": section_title,
						"date": detail_date or str(row.get("accounting_date") or ""),
						"business_id": business_id,
						"expense_type": category
						or expense_type
						or EXPENSE_SECTION_LABELS.get(bucket, ""),
						"description": meta.get("description") or meta.get("title") or "",
						"amount": round(amount, 2),
						"payment_event": _payment_event_label(
							accounting_source, meta.get("evidence") or ""
						),
						"remark": meta.get("evidence") or "",
					}
				)
			expense_details.sort(key=lambda item: (item["date"], item["business_id"]))
		except Exception as exc:  # 支出库不可用时只影响支出区块
			expense_available = False
			warning = f"实际支出读取失败：{exc}"
	expense["total"] = sum(expense[key] for key in EXPENSE_KEYS)
	for key, value in list(budget.items()):
		budget[key] = round(float(value or 0), 2)
	for key, value in list(expense.items()):
		expense[key] = round(float(value or 0), 2) if expense_available else None
	if not expense_available:
		expense_details = []
	if data_source_mode() == "local":
		from ..services.ledger_sync_settings import current_source

		corp_id = current_source()["corp_id"]
	else:
		corp_id = _runtime_config_value("budget_corp_id", "budget_oa_corp_id", "dingtalk_oa_corp_id")

	return {
		"entry": entry,
		"budget": budget,
		"expense": expense,
		"remaining": round(budget["total"] - expense["total"], 2) if expense_available else None,
		"expense_available": expense_available,
		"dingtalk": _dingtalk_links(entry.get("process_instance_id")),
		"expense_details": expense_details,
		"warning": warning,
		"corp_id": corp_id,
	}
