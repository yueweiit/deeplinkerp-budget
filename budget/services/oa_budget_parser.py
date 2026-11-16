"""把钉钉 OA 归档的审批实例解析成预算台账。

数据来源只有两张表：

- ``ding_approval_instance``：``form_component_values`` 是表单字段（JSON），
  ``raw_payload.businessId`` 是业务单号（对应原来的 ``form_no``）；
- ``ding_approval_task``：审批任务流水，这里不用，留给效率统计。

字段名是中英西三语混排（例如 ``明细项目Ítem detallado``），因此一律按**中文前缀**
匹配，不依赖完整字符串，避免模板改语言后失效。

解析结果：
    {"entry": {...Budget Ledger Entry 字段...}, "details": [{...明细行...}]}
"""

from __future__ import annotations

import json
import re
from typing import Any

# 预算申请模板（「预算类型」字段决定生产 / 非生产）
BUDGET_PROCESS_CODE = "PROC-45C2862D-07D1-48AE-A595-4A9EE54FBF8C"

# 子表单字段 → 明细分组
DETAIL_GROUPS = (
	("hr", ("人资预算", "Presupuesto de Recursos Humanos")),
	("office", ("办公场地预算", "Presupuesto del espacio de oficina")),
	("operation", ("预算项目", "Proyecto presupuestario")),
)

# 兜底项目名用的中文分组名：办公场地预算的子表单没有「明细项目」列，
# 不兜底的话台账/导出里这一列会是空的，行没法辨认
GROUP_LABELS = {"hr": "人资预算", "office": "办公场地预算", "operation": "预算项目"}

# 模板自带的填写提示（「说明」列里全是它），不是业务内容，不能落库当说明
TEMPLATE_HINTS = ("如果汇率为空", "Si la tasa de cambio está vacía")

_NUM_RE = re.compile(r"-?\d+(?:[.,]\d+)?")


def _text(value: Any) -> str:
	if value is None:
		return ""
	if isinstance(value, (dict, list)):
		return json.dumps(value, ensure_ascii=False)
	return str(value).strip()


def _num(value: Any) -> float:
	"""金额/人数统一成 float：容忍千分位、货币符号、空值。"""
	text = _text(value).replace(",", "")
	if not text:
		return 0.0
	match = _NUM_RE.search(text)
	if not match:
		return 0.0
	try:
		return float(match.group(0))
	except ValueError:
		return 0.0


def _components(instance: dict) -> list[dict]:
	value = instance.get("form_component_values")
	if isinstance(value, str):
		try:
			value = json.loads(value)
		except ValueError:
			return []
	return value if isinstance(value, list) else []


def _field(components: list[dict], *prefixes: str) -> Any:
	"""按中文前缀取第一个有值的字段（同名字段可能重复，取非空的）。"""
	for prefix in prefixes:
		for item in components:
			if not isinstance(item, dict):
				continue
			name = _text(item.get("name"))
			if not name.startswith(prefix) and prefix not in name:
				continue
			value = item.get("value")
			if value not in (None, "", [], {}):
				return value
	return None


def _sub_rows(components: list[dict], *prefixes: str) -> list[dict[str, Any]]:
	"""把子表单解析成行字典列表：``[{"明细项目Ítem detallado": "工资", ...}]``。"""
	raw = _field(components, *prefixes)
	if raw is None:
		return []
	rows = raw
	if isinstance(raw, str):
		try:
			rows = json.loads(raw)
		except ValueError:
			return []
	if not isinstance(rows, list):
		return []

	parsed: list[dict[str, Any]] = []
	for item in rows:
		if not isinstance(item, dict):
			continue
		cells = item.get("rowValue") or []
		row: dict[str, Any] = {}
		for cell in cells:
			if not isinstance(cell, dict):
				continue
			label = _text(cell.get("label"))
			row[label] = cell.get("value")
		if row:
			parsed.append(row)
	return parsed


def _cell(row: dict[str, Any], *prefixes: str) -> Any:
	"""行内按中文前缀取值。"""
	for prefix in prefixes:
		for label, value in row.items():
			if label.startswith(prefix) or prefix in label:
				if value not in (None, ""):
					return value
	return None


def _budget_kind(value: Any) -> str:
	text = _text(value).lower()
	if "非生产" in text or "no producción" in text or "no produccion" in text:
		return "non_production"
	return "production"


def _month(value: Any) -> str | None:
	"""``2026-11`` / ``2026-11-01`` / ``2026/11`` 统一成 YYYY-MM-01。"""
	text = _text(value)
	match = re.search(r"(\d{4})[-/年]?(\d{1,2})", text)
	if not match:
		return None
	return f"{match.group(1)}-{int(match.group(2)):02d}-01"


def _date(value: Any) -> str | None:
	text = _text(value)
	match = re.search(r"(\d{4})[-/年](\d{1,2})[-/月](\d{1,2})", text)
	if not match:
		return _month(text)
	return f"{match.group(1)}-{int(match.group(2)):02d}-{int(match.group(3)):02d}"


def _payload(instance: dict) -> dict:
	value = instance.get("raw_payload")
	if isinstance(value, str):
		try:
			return json.loads(value) or {}
		except ValueError:
			return {}
	return value if isinstance(value, dict) else {}


def _detail_rows(components: list[dict], group: str) -> list[dict[str, Any]]:
	"""按分组拍平子表单，字段名对齐现有 ``budget_hr/office/operation`` 表。"""
	prefixes = dict(DETAIL_GROUPS)[group]
	rows = _sub_rows(components, *prefixes)
	out: list[dict[str, Any]] = []

	for index, row in enumerate(rows):
		source_item = _text(_cell(row, "明细项目", "预算明细", "Ítem detallado"))
		# 有的单据把子表单名本身填进了「明细项目」（表头残留），当空处理走兜底名
		if group == "office" and source_item.startswith(GROUP_LABELS["office"]):
			source_item = ""
		detail = {
			"detail_group": group,
			"detail_type": group,
			"row_index": index,
			# 办公场地预算的子表单没有「明细项目」列，用「分组名-序号」兜底，
			# 否则台账和导出里这列全空，一行行看过去认不出是什么
			"detail_item": source_item or f"{GROUP_LABELS[group]}-{index + 1}",
			"headcount": _num(_cell(row, "人数", "Número de personal")),
			"original_amount": _num(_cell(row, "原币金额", "Monto en moneda")),
			"currency": _text(_cell(row, "币种", "Moneda")),
			"exchange_rate": _num(_cell(row, "汇率", "Tipo de cambio")),
			"rmb_amount": _num(_cell(row, "人民币金额", "Monto en yuanes")),
			"calculation_basis": _text(_cell(row, "计算依据", "Base de cálculo")),
		}

		if group == "operation":
			# 「管理支出 / 销售费用 / 财务相关费用…」是科目分类
			detail["operation_expense"] = _text(
				_cell(row, "管理支出", "销售费用", "财务相关费用", "Gastos de operación")
			)
		# 「说明」三类子表单都有：人资/办公放无 label 的说明文字，预算项目放业务用途。
		# 模板提示语（「如果汇率为空…」）整列都是，属于噪音，过滤掉。
		detail["budget_purpose_detail"] = _note(
			_cell(row, "说明", "Descripción")
			or ("" if group == "operation" else _first_unlabeled(row))
		)

		detail["amount"] = detail["rmb_amount"] or detail["original_amount"]
		# 项目名兜底后每行都有值，判空要回到源端字段，否则空行也会被留下
		if (
			source_item
			or detail["amount"]
			or detail["headcount"]
			or detail["budget_purpose_detail"]
			or detail.get("operation_expense")
		):
			out.append(detail)
	return out


def _legacy_detail_rows(components: list[dict]) -> list[dict[str, Any]]:
	"""旧模板的「预算明细」子表单：只有「预算用途明细」和「金额」两列。

	旧模板没有 hr / office / operation 的分组概念，这里统一归到 ``operation``，
	与「非生产预算」口径一致，页面明细列表也能正常展示；
	金额同时写进原币 / 人民币 / 最终金额，保证与单据总额对得上。
	"""
	out: list[dict[str, Any]] = []
	for index, row in enumerate(_sub_rows(components, "预算明细")):
		amount = _num(_cell(row, "金额", "Monto", "Importe"))
		item = _text(_cell(row, "预算用途明细", "用途明细", "Descripción"))
		if not item and not amount:
			continue
		out.append(
			{
				"detail_group": "operation",
				"detail_type": "operation",
				"row_index": index,
				"detail_item": item,
				"budget_purpose_detail": item,
				"currency": "CNY",
				"original_amount": amount,
				"rmb_amount": amount,
				"amount": amount,
				"remark": "旧模板「预算明细」",
			}
		)
	return out


def _first_unlabeled(row: dict[str, Any]) -> Any:
	""""人资预算"首列没有 label，按插入顺序取第一个非空值。"""
	for label, value in row.items():
		if label or value in (None, ""):
			continue
		return value
	return None


def _note(value: Any) -> str:
	"""过滤掉模板自带的填写提示：它们占了整列「说明」，但不是业务内容。"""
	text = _text(value)
	if text.startswith(TEMPLATE_HINTS):
		return ""
	return text


def parse_budget_instance(instance: dict) -> dict:
	"""解析一条预算申请实例。

	``instance`` 需包含：``form_component_values``、``raw_payload``、
	``process_instance_id``、``status``、``result``、``create_time``、
	``originator_dept_id``、``originator_dept_name``、``process_code``。
	"""
	components = _components(instance)
	payload = _payload(instance)

	form_no = _text(payload.get("businessId")) or _text(instance.get("process_instance_id"))
	dept_id = _text(instance.get("originator_dept_id"))
	dept_name = _text(instance.get("originator_dept_name")) or _text(
		_field(components, "部门", "Departamento")
	)

	# 新模板用「预算月份」，旧模板只有「填报月份」。旧单若不做兜底，budget_month 会是
	# NULL，按月的统计、去重、额度对账全部失效，所以两段都解析并让预算月份兜底。
	budget_month = _month(_field(components, "预算月份", "Mes presupuestario"))
	declaration_month = _month(_field(components, "填报月份", "Mes de declaración"))

	entry = {
		"form_no": form_no,
		"process_instance_id": _text(instance.get("process_instance_id")),
		"dept_name": dept_name,
		"dept_id": dept_id,
		"dept_key": f"id:{dept_id}" if dept_id else f"legacy:{form_no}",
		"budget_kind": _budget_kind(_field(components, "预算类型", "Tipo de presupuesto")),
		"application_date": _date(_field(components, "申请日期", "Fecha de solicitud")),
		"budget_month": budget_month or declaration_month,
		"declaration_month": declaration_month,
		"execution_region": _text(_field(components, "执行地区", "Región de ejecución")),
		"status": _text(instance.get("status")),
		"amount": _num(_field(components, "预算总金额", "Presupuesto Total")),
		"remark": _text(_field(components, "备注", "Observaciones")),
		"oa_process_code": _text(instance.get("process_code")),
		"creator_name": _text(instance.get("originator_user_name")),
		"dept_path_names": None,
		"source_created_at": _text(instance.get("create_time")) or None,
	}

	details: list[dict[str, Any]] = []
	for group, _prefixes in DETAIL_GROUPS:
		group_rows = _detail_rows(components, group)
		for row in group_rows:
			row["form_no"] = form_no
			row["process_instance_id"] = entry["process_instance_id"]
		details.extend(group_rows)

	# 旧模板没有 hr / office / operation 三组，只有一张「预算明细」表
	if not details:
		details = _legacy_detail_rows(components)
		for row in details:
			row["form_no"] = form_no
			row["process_instance_id"] = entry["process_instance_id"]

	entry["detail_amount"] = round(sum(row["amount"] for row in details), 2)
	return {"entry": entry, "details": details}


def dedupe_latest(entries: list[dict]) -> list[dict]:
	"""同一 ``(dept_key, budget_month)`` 只保留 ``source_created_at`` 最新的一单。

	与线上 ``ROW_NUMBER() OVER (PARTITION BY dept_key, budget_month_key)`` 口径一致。
	"""
	latest: dict[tuple, dict] = {}
	for entry in entries:
		key = (entry.get("dept_key"), entry.get("budget_month"))
		current = latest.get(key)
		if current is None or _text(entry.get("source_created_at")) > _text(
			current.get("source_created_at")
		):
			latest[key] = entry
	return list(latest.values())
