"""Explicit management classifications; never infer company or budget revisions from prose."""
import json

CATEGORIES = {
	"salary": "Salaries", "social": "Social insurance and housing fund", "benefits": "Employee benefits",
	"bonus": "Bonuses", "rent": "Office rent", "utilities": "Utilities", "internet": "Internet and communications",
	"office": "Office expenses", "it": "IT and software", "travel": "Travel expenses", "vehicle": "Vehicle expenses",
	"marketing": "Marketing expenses", "sales": "Sales expenses", "tax": "Taxes and fees",
	"finance": "Finance expenses", "service": "Professional services", "mro": "Maintenance and consumables",
	"inventory": "Inventory purchases", "investment": "Investment purchases", "other": "Other classified expenses",
}
HR_CATEGORIES = {"salary", "social", "benefits", "bonus"}
# Exact historical option values from the supplied forms. New options must be
# mapped explicitly; substring matches would silently merge different expenses.
ALIASES = {
	"salary": ("工资Salario", "工资中国Salario en China", "工资salario", "工资", "Salario", "Salaries"),
	"social": ("公司社保公积金Seguro social y fondo de vivienda de la empresa", "社保公积金", "社保中国Seguro social en China"),
	"benefits": ("职工福利费 Gastos de beneficios laborales", "职工福利费", "员工福利"),
	"bonus": ("奖金 Bonificaciones", "奖金", "Bonificaciones"),
	"rent": ("办公场地租金Alquiler de local de oficinas", "办公场地租金", "房租", "Alquiler de local de oficinas"),
	"utilities": ("电费factura de electricidad", "电费", "水费", "水电费"),
	"internet": ("网费internet", "网费", "Internet"),
	"office": ("办公费", "办公用品", "Gastos de oficina"),
	"it": ("AI的Token费用", "AI调用Token费用Costo de Token de Grandes Modelos de IA", "电脑租赁费用"),
	"vehicle": ("车辆使用费gastos de uso de vehículo", "车辆使用费"),
	"tax": ("税费Impuestos", "税费"),
	"finance": ("财务相关费用Gastos relacionados con finanzas",),
	"investment": ("投资采购Compra de inversión",),
}


def defaults():
	return {"warning_percent": 90, "category_mappings": []}


def clean(value):
	if isinstance(value, str):
		value = json.loads(value or "{}")
	if not isinstance(value, dict) or set(value) - set(defaults()):
		raise ValueError("Invalid CN management rules.")
	result = {**defaults(), **value}
	threshold = result["warning_percent"]
	if isinstance(threshold, bool) or not isinstance(threshold, (int, float)) or not 1 <= threshold <= 100:
		raise ValueError("The budget warning percentage must be between 1 and 100.")
	for field in ("category_mappings",):
		if not isinstance(result[field], list) or len(result[field]) > 1000:
			raise ValueError("Invalid CN management rules.")
	seen, mappings = set(), []
	for row in result["category_mappings"]:
		if not isinstance(row, dict):
			raise ValueError("Invalid CN category mapping.")
		kind, path, category = row.get("kind"), row.get("source_path"), row.get("category")
		if isinstance(path, str):
			path = json.loads(path)
		if (kind not in ("budget", "operating", "purchase") or not isinstance(path, list) or not 1 <= len(path) <= 8
			or any(not isinstance(p, str) or not p.strip() or len(p) > 500 for p in path)
			or category not in CATEGORIES):
			raise ValueError("Invalid CN category mapping.")
		path = [p.strip() for p in path]
		identity = (kind, tuple(path))
		if identity in seen:
			raise ValueError("Duplicate CN category mapping.")
		seen.add(identity)
		mappings.append({"kind": kind, "source_path": path, "category": category})
	result["category_mappings"] = sorted(mappings, key=lambda row: (row["kind"], row["source_path"]))
	return result


def classify(kind, path, rules):
	path = [str(part).strip() for part in path if part]
	for row in rules["category_mappings"]:
		if kind == row["kind"] and path == row["source_path"]:
			return row["category"]
	# Only the terminal option is compared: a generic parent must not disguise
	# an unknown or more specific child as a fully classified expense.
	for code, values in ALIASES.items():
		if path and path[-1] in values:
			return code
	return ""
