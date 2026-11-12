"""预算台账查询接口（只读）。

命名契约与 ``api/approval.py`` 一致：``get_`` / ``list_`` 无副作用，前端可安全重试。
"""

from __future__ import annotations

import frappe

from ..integrations.budget_ledger_source import BudgetLedgerSource
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


def get_source() -> BudgetLedgerSource:
	"""同一请求内复用数据源实例。"""
	source = getattr(frappe.local, "budget_ledger_source", None)
	if source is None:
		source = BudgetLedgerSource()
		frappe.local.budget_ledger_source = source
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
