"""Single-currency bank books on the existing, permission-checked native cash journal.

Book, source and operation records are private service metadata. Each write takes
one book row lock before touching native entries; Frappe commits the HTTP POST.
"""

from __future__ import annotations

import json
import re
from contextlib import contextmanager
from datetime import date, timedelta
from decimal import Decimal

import frappe
from babel.numbers import get_currency_precision, is_currency
from frappe.desk.reportview import get_match_cond

from budget.services.bank_journal_contract import (
	SOURCE,
	TEXT,
	as_date,
	batch_digest,
	clean_row,
	digest,
	money,
	month_bounds,
)
from budget.services.bank_journal_schema import BOOK_FIELD, ENTRY, EXTRA, SEQ, VOID, managed
from budget.services.bank_journal_access import is_import_user
from budget.services.bank_journal_access import journal_companies as allowed_companies

BOOK = "CN Bank Journal Book"
OP = "CN Bank Journal Operation"
ORIGIN = "CN Bank Journal Source"
CORE = ("posting_date", "direction", "amount", "currency", "summary", "dingtalk_approval_number", "reference_number")
ROW_FIELDS = ["name", *CORE, EXTRA, SEQ, VOID]
MAX_BATCH = 10000
REPLACE_RANGE = "Replace Range"


def _json(x, default=None):
	if x in (None, ""):
		return default
	return json.loads(x) if isinstance(x, str) else x


def _dump(value):
	return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def _doctype_permission(permission):
	if not frappe.has_permission(ENTRY, ptype=permission):
		frappe.throw("没有这项银行日记账权限。", frappe.PermissionError)


def authorize_book(book, permission="read", *, lock=False):
	_doctype_permission("read")
	if permission != "read":
		_doctype_permission(permission)
	doc = frappe.get_doc(BOOK, book, for_update=True) if lock else frappe.get_doc(BOOK, book)
	if doc.company not in allowed_companies():
		frappe.throw("无此公司银行日记账的访问权限。", frappe.PermissionError)
	return doc


def authorize_replacement():
	if is_import_user():
		frappe.throw("流水导入专用账号没有覆盖账簿权限。", frappe.PermissionError)
	for permission in ("create", "write", "delete"):
		_doctype_permission(permission)


def book_currency(doc):
	"""Books and native entries created before the currency field always used CNY."""
	return (doc.get("currency") if isinstance(doc, dict) else getattr(doc, "currency", None)) or "CNY"


def _bank_offset(doc, ledger_book=None):
	if ledger_book:
		if ledger_book != doc.name:
			frappe.throw("此文件的账面余额来自其他账簿。")
		return Decimal(0)
	return Decimal(str(getattr(doc, "bank_balance_offset", 0) or 0))


def _initial_bank_offset(settings, review):
	if not review or "file_opening" not in review:
		return "0.00"
	if money(review["book_opening"]) != money(settings.opening) or review["currency"] != book_currency(settings):
		frappe.throw("确认的期初调整与目标账簿不符。")
	difference = Decimal(money(settings.opening)) - Decimal(money(review["file_opening"]))
	if "difference" in review and Decimal(str(review["difference"])) != difference:
		frappe.throw("确认的期初差额与银行和账簿期初余额不符。")
	return format(difference, ".2f")


def _supported_currency(row):
	return is_currency(row.name) and get_currency_precision(row.name) == 2 and row.fraction_units in (None, 0, 100)


def supported_currencies():
	rows = frappe.get_all("Currency", filters={"enabled": 1}, fields=["name", "fraction_units"],
		order_by="name", limit_page_length=0)
	return [row.name for row in rows if _supported_currency(row)]


def _check_currency(doc, declared):
	if declared != book_currency(doc):
		frappe.throw(f"该账户账簿的币种是 {book_currency(doc)}，与所选币种不一致。")


def require_oa_currency(doc):
	if book_currency(doc) != "CNY":
		frappe.throw("钉钉支出同步的付款金额为人民币（CNY），仅可同步到 CNY 账簿。")


@frappe.whitelist()
def get_options():
	_doctype_permission("read")
	companies = sorted(allowed_companies())
	perms = {
		kind: bool(frappe.has_permission(ENTRY, ptype=kind))
		for kind in ("create", "write", "delete", "export")
	}
	if is_import_user():
		perms["write"] = False
	books = (
		frappe.get_all(
			BOOK,
			filters={"company": ["in", companies]},
			fields=["name", "company", "account", "currency"],
			order_by="company, account",
			limit_page_length=0,
		)
		if companies
		else []
	)
	# A count-only query preserves access to pre-existing native entries that
	# have never been assigned to a bank book.
	legacy = (
		frappe.get_list(
			ENTRY,
			filters={BOOK_FIELD: ["is", "not set"], "company": ["in", companies]},
			fields=[{"COUNT": "name", "as": "n"}],
			limit_page_length=1,
		)
		if companies
		else []
	)
	return {
		"companies": companies,
		"books": [frappe._dict(book, currency=book_currency(book)) for book in books],
		"currencies": supported_currencies(),
		"month": frappe.utils.today()[:7],
		"legacy_count": int(legacy[0]["n"] or 0) if legacy else 0,
		"can_sync": perms["create"]
		and (
			frappe.session.user == "Administrator"
			or bool(set(frappe.get_roles()) & {"System Manager", "Accounts Manager"})
		),
		**{"can_" + k: v for k, v in perms.items()},
	}


def _lock_company_book(company, name):
	frappe.get_doc("Company", company, for_update=True).check_permission("read")
	return frappe.db.get_value(
		BOOK, name, ["name", "opening", "opening_date", "currency"], as_dict=True, for_update=True
	)


def authorize_book_creation(company):
	_doctype_permission("read")
	_doctype_permission("create")
	if not isinstance(company, str) or not company.strip():
		frappe.throw("请选择银行账簿所属公司。")
	if company not in allowed_companies():
		frappe.throw("没有此公司的权限。", frappe.PermissionError)


def book_settings(company, account, opening, opening_date, currency="CNY"):
	issues = []
	if not isinstance(account, str) or not account.strip() or len(account.strip()) > 140:
		issues.append({"field": "account", "message": "请填写银行账户的账簿名称。"})
	account = account.strip() if isinstance(account, str) else ""
	currency = currency.strip().upper() if isinstance(currency, str) else ""
	if not currency:
		issues.append({"field": "currency", "message": "请选择账簿币种。"})
	elif currency not in supported_currencies():
		issues.append({"field": "currency", "message": "请选择 ERP 中已启用、使用两位小数的币种。"})
	try:
		amount = money(opening)
	except ValueError:
		amount = None
		issues.append({"field": "opening", "message": "请提供明确的账簿期初余额，最多保留两位小数。"})
	try:
		start = as_date(opening_date).isoformat()
	except ValueError:
		start = None
		issues.append({"field": "opening_date", "message": "请填写有效的账簿启用日期（YYYY-MM-DD）。"})
	return frappe._dict(
		name=digest([company, account]), company=company, account=account, currency=currency,
		opening=amount, opening_date=start, revision=0
	), issues


def book_exists(name):
	return bool(frappe.db.exists(BOOK, name))


def _insert_book(settings):
	currency = book_currency(settings)
	master = frappe.db.get_value("Currency", {"name": currency, "enabled": 1}, ["name", "fraction_units"],
		as_dict=True, for_update=True)
	if not master or not _supported_currency(master):
		frappe.throw("账簿币种已停用或精度不受支持，请刷新后重新选择。")
	frappe.get_doc(dict(
		doctype=BOOK, company=settings.company, account=settings.account, currency=currency, opening=settings.opening,
		bank_balance_offset=format(_bank_offset(settings), ".2f"), opening_date=settings.opening_date,
		revision=0, next_sequence=0,
	)).insert(ignore_permissions=True, set_name=settings.name)


@frappe.whitelist(methods=["POST"])
def create_book(company=None, account=None, opening=None, opening_date=None, currency="CNY"):
	authorize_book_creation(company)
	settings, issues = book_settings(company, account, opening, opening_date, currency)
	if issues:
		frappe.throw(issues[0]["message"])
	with _transaction():
		existing = _lock_current_snapshot(lambda: _lock_company_book(company, settings.name))
		if existing:
			_check_currency(existing, settings.currency)
			if money(existing.opening) != settings.opening or str(existing.opening_date) != settings.opening_date:
				frappe.throw("该账户已有账簿，期初设置不同。")
		else:
			_insert_book(settings)
	return {"name": settings.name, "book": settings.name, "account": settings.account,
		"company": company, "currency": settings.currency}


def _wire(native):
	extra = _json(native.get(EXTRA), {})
	direct, amount = native.get("direction"), money(native.get("amount"), positive=True)
	values = {k: extra.get(k, "") for k in TEXT}
	values.update(
		name=native.get("name"),
		date=str(native.get("posting_date")),
		currency=book_currency(native),
		summary=native.get("summary") or "",
		income=amount if direct == "income" else None,
		expense=amount if direct == "expense" else None,
		refs=extra.get("refs", []),
		links=extra.get("links", {}),
		source=extra.get("source", "Manual"),
		bank_ref=native.get("reference_number") or "",
	)
	if "raw_cells" in extra:
		values["raw_cells"] = extra["raw_cells"]
	return values


def _entry_scope(book):
	return {BOOK_FIELD: book.name, VOID: 0, "currency": book_currency(book)}


def _rows(book, start=None, end=None):
	filters = [[key, "=", value] for key, value in _entry_scope(book).items()]
	if start:
		filters.append(["posting_date", ">=", start])
	if end:
		filters.append(["posting_date", "<", end])
	return [
		_wire(r)
		for r in frappe.get_list(
			ENTRY,
			fields=ROW_FIELDS,
			filters=filters,
			order_by=f"posting_date asc, {SEQ} asc, name asc",
			limit_page_length=0,
		)
	]


def _prior(book, before):
	if before <= as_date(str(book.opening_date)):
		return Decimal(str(book.opening))
	# Aggregate the previous movements in SQL. Do not load past months' rows
	# when opening the page. Use native list scope for the same authorized set.
	result = frappe.db.sql(
		f"""select sum(case when direction='income' then amount else -amount end)
              from `tab{ENTRY}` where {BOOK_FIELD}=%s and currency=%s and {VOID}=0 and posting_date < %s"""
		+ get_match_cond(ENTRY),
		(book.name, book_currency(book), before),
	)
	return Decimal(str(book.opening)) + Decimal(str(result[0][0] or 0))


def _period(book, month, full=False):
	if full:
		rows, opening = _rows(book), Decimal(str(book.opening))
	else:
		month, start, end = month_bounds(month, frappe.utils.today())
		rows = _rows(book, start, end)
		opening = _prior(book, start) if end > as_date(str(book.opening_date)) else Decimal(0)
	income = expense = Decimal(0)
	for r in rows:
		income += Decimal(r["income"] or 0)
		expense += Decimal(r["expense"] or 0)
		r["balance"] = format(opening + income - expense, ".2f")
	return dict(
		book=book.name,
		company=book.company,
		account=book.account,
		currency=book_currency(book),
		month=month,
		version=int(book.revision or 0),
		opening=format(opening, ".2f"),
		income=format(income, ".2f"),
		expense=format(expense, ".2f"),
		closing=format(opening + income - expense, ".2f"),
		rows=rows,
		opening_date=str(book.opening_date),
	)


@frappe.whitelist()
def get_month(book, month=None):
	doc = authorize_book(book)
	result = _period(doc, month)
	result["rows"].reverse()
	for row in result["rows"]:
		row.pop("raw_cells", None)
	result["undo_operation"] = None
	if doc.last_operation:
		last = frappe.db.get_value(OP, doc.last_operation, ["actor", "operation", "result_json"], as_dict=True)
		if last and last.actor == frappe.session.user:
			result["undo_operation"] = doc.last_operation
			if last.operation == REPLACE_RANGE:
				result["undo_range"] = _json(last.result_json)["impact"]
	return result


def export_rows(book, month=None):
	doc = authorize_book(book, "export")
	return {**_period(doc, month, full=month is None), "template": _json(doc.template_json),
		"bank_balance_offset": format(_bank_offset(doc), ".2f")}


def book_ref_rows(book, refs):
	doc = authorize_book(book)
	refs = set(refs)
	if not refs:
		return []
	# Native Data stores up to 140 characters. For longer multi-reference
	# cells the first code fits there and every full number is in JSON.
	names = set()
	for offset in range(0, len(refs), 200):
		group = sorted(refs)[offset : offset + 200]
		names.update(
			frappe.get_all(
				"CN Bank Journal Reference",
				filters={"book": doc.name, "approval_number": ["in", group]},
				pluck="entry",
				limit_page_length=0,
			)
		)
	if not names:
		return []
	return [
		_wire(r)
		for r in frappe.get_list(
			ENTRY,
			filters={**_entry_scope(doc), "name": ["in", sorted(names)]},
			fields=ROW_FIELDS,
			limit_page_length=0,
		)
	]


def _source(source):
	if not isinstance(source, str) or source.lower() not in SOURCE:
		frappe.throw("未知流水来源。")
	return SOURCE[source.lower()]


def _source_id(book, source, key):
	return digest([book, source, key])


def known_source_keys(book, source, keys):
	doc = authorize_book(book)
	source = _source(source)
	ids = [_source_id(doc.name, source, k) for k in keys]
	found = set()
	for start in range(0, len(ids), 500):
		found.update(
			frappe.get_all(
				ORIGIN,
				filters={"name": ["in", ids[start : start + 500]]},
				pluck="source_key",
				limit_page_length=0,
			)
		)
	return found


@contextmanager
def _transaction():
	point = "bank_journal_change"
	frappe.db.savepoint(point)
	try:
		yield
	except frappe.QueryDeadlockError:
		# A MariaDB transaction victim has already lost every savepoint.
		raise
	except Exception:
		frappe.db.rollback(save_point=point)
		raise


def _lock_current_snapshot(acquire):
	# MariaDB snapshot isolation can reject a locking read if another request
	# committed after this transaction began. Retry only before any writes.
	for attempt in range(3):
		clean_transaction = not frappe.db.transaction_writes
		try:
			return acquire()
		except frappe.QueryDeadlockError:
			if not clean_transaction or attempt == 2:
				raise
			frappe.db.rollback()
			frappe.db.savepoint("bank_journal_change")


def _begin(book, permission, version, request_id, kind, payload, *, replay_only=False):
	if not isinstance(request_id, str) or not re.fullmatch(r"[0-9A-Za-z_-]{8,100}", request_id):
		frappe.throw("操作标识无效，请刷新页面后重试。")
	doc = _lock_current_snapshot(lambda: authorize_book(book, permission, lock=True))
	ident = digest([doc.name, frappe.session.user, request_id])
	fingerprint = digest([kind, payload])
	previous = frappe.db.get_value(OP, ident, ["digest", "result_json"], as_dict=True, for_update=True)
	if previous:
		if previous.digest != fingerprint:
			frappe.throw("同一操作标识已用于其他内容。")
		return doc, ident, fingerprint, _json(previous.result_json)
	if replay_only:
		frappe.throw("该账户已有账簿，请选择该账户后导入银行流水。", frappe.TimestampMismatchError)
	if str(version) != str(doc.revision or 0):
		frappe.throw("账簿已更新，请刷新后重新核对。", frappe.TimestampMismatchError)
	return doc, ident, fingerprint, None


def _finish(doc, ident, fingerprint, kind, changes, *, result=None, new_last=None):
	prior = doc.last_operation or ""
	revision = int(doc.revision or 0) + int(bool(changes))
	response = {"operation": ident, "version": revision, **(result or {})}
	frappe.get_doc(
		dict(
			doctype=OP,
			book=doc.name,
			actor=frappe.session.user,
			operation=kind,
			digest=fingerprint,
			prior_operation=prior,
			changes=_dump(changes),
			result_json=_dump(response),
		)
	).insert(ignore_permissions=True, set_name=ident)
	if changes:
		doc.revision = revision
		doc.last_operation = ident if new_last is None else new_last
		# Private DocTypes have no direct user write roles.
		doc.save(ignore_permissions=True)
	return response


def _snapshot(doc):
	return {
		key: str(doc.get(key)) if key in ("posting_date", "amount") else doc.get(key)
		for key in (*CORE, EXTRA, VOID)
	}


def _authorized_entry(book, name, permission):
	doc = frappe.get_doc(ENTRY, name, for_update=True)
	if doc.get(BOOK_FIELD) != book.name or doc.company != book.company or doc.currency != book_currency(book):
		frappe.throw("流水不属于当前账簿。", frappe.PermissionError)
	doc.check_permission(permission)
	return doc


def _check_date(row, book):
	if as_date(row["date"]) < as_date(str(book.opening_date)):
		frappe.throw("流水日期早于账簿启用日。")


def _reindex(book, name, refs):
	ref_table = "CN Bank Journal Reference"
	frappe.db.delete(ref_table, {"book": book, "entry": name})
	if refs:
		for ref in refs:
			frappe.get_doc(dict(doctype=ref_table, book=book, entry=name, approval_number=ref)).insert(
				ignore_permissions=True, set_name=digest([book, name, ref])
			)


def _write(doc, row, book, *, extra, new):
	_check_date(row, book)
	references = "\n".join(row["refs"])
	doc.update(
		dict(
			posting_date=row["date"],
			amount=row["income"] or row["expense"],
			direction="income" if row["income"] else "expense",
			summary=row.get("summary", ""),
			dingtalk_approval_number=references if len(references) <= 140 else row["refs"][0],
		)
	)
	extra.update({key: row[key] for key in (*TEXT, "refs") if key != "summary" and key in row})
	doc.set(EXTRA, _dump(extra))
	if new:
		book.next_sequence = int(book.next_sequence or 0) + 1
		doc.update({BOOK_FIELD: book.name, SEQ: book.next_sequence, VOID: 0,
			"company": book.company, "currency": book_currency(book)})
		doc.insert()
	else:
		doc.save()
	_reindex(book.name, doc.name, row["refs"])


def _links(company, refs):
	if not refs:
		return {}
	from budget.services.bank_journal_oa import resolve_refs

	return resolve_refs(company, refs)


@frappe.whitelist(methods=["POST"])
def save_row(book, row, version, request_id):
	raw = _json(row)
	values = clean_row(raw)
	name = raw.get("name")
	with _transaction(), managed():
		doc, ident, fp, done = _begin(
			book, "write" if name else "create", version, request_id, "Manual", [name, values]
		)
		if raw.get("currency") not in (None, ""):
			_check_currency(doc, raw["currency"])
		if done:
			return done
		current = _authorized_entry(doc, name, "write") if name else frappe.new_doc(ENTRY)
		if current.get(VOID):
			frappe.throw("流水已取消。")
		before = _snapshot(current) if name else None
		extra = _json(current.get(EXTRA), {}) if name else {"source": "Manual"}
		previous_refs = set(extra.get("refs", []))
		added = [r for r in values["refs"] if r not in previous_refs]
		new_links = _links(doc.company, added)
		if any(ref not in new_links for ref in added):
			frappe.throw("钉钉单号没有唯一对应到当前公司的审批原单，请核对后重试。")
		extra["links"] = {
			r: url for r in values["refs"] if (url := (new_links.get(r) or extra.get("links", {}).get(r)))
		}
		_write(current, values, doc, extra=extra, new=not name)
		return _finish(
			doc,
			ident,
			fp,
			"Manual",
			[{"entry": current.name, "before": before}],
			result={"name": current.name},
		)


@frappe.whitelist(methods=["POST"])
def cancel_row(book, name, version, request_id):
	with _transaction(), managed():
		doc, ident, fp, done = _begin(book, "delete", version, request_id, "Cancel", name)
		if done:
			return done
		entry = _authorized_entry(doc, name, "delete")
		if entry.get(VOID):
			frappe.throw("流水已取消。")
		before = _snapshot(entry)
		entry.set(VOID, 1)
		entry.save()
		return _finish(doc, ident, fp, "Cancel", [{"entry": entry.name, "before": before}])


def _matching_entry(book, source, row, claimed):
	"""Reconcile proven same movement across sources without collapsing bank installments."""
	matches = book_ref_rows(book.name, row["refs"]) if row["refs"] else []
	if row.get("bank_ref"):
		native = frappe.get_list(
			ENTRY,
			filters={**_entry_scope(book), "reference_number": row["bank_ref"]},
			fields=ROW_FIELDS,
			limit_page_length=0,
		)
		matches.extend(_wire(r) for r in native)
	# An OA/manual row reconciled to a prior bank transaction already has
	# an Import binding even though its primary displayed origin is unchanged.
	# A distinct bank movement must not attach to that same entry again.
	foreign = {r["name"]: r for r in matches if r["source"] != source and r["name"] not in claimed}
	assigned = (
		set(
			frappe.get_all(
				ORIGIN,
				filters={"book": book.name, "source": source, "entry": ["in", list(foreign)]},
				pluck="entry",
				limit_page_length=0,
			)
		)
		if foreign
		else set()
	)
	relevant = [r for name, r in foreign.items() if name not in assigned]
	if not relevant:
		return None
	exact = [
		r
		for r in relevant
		if r["date"] == row["date"] and r["income"] == row["income"] and r["expense"] == row["expense"]
	]
	if len(exact) == 1:
		return exact[0]["name"]
	frappe.throw(f"{row['date']} 的流水与已有流水使用相同单号，但日期或金额无法唯一核实。")


def _legacy_import_issue(row):
	return {"row": row["source_row"], "field": "summary",
		"message": f"原表第 {row['source_row']} 行与旧版导入回执相符，请核对已有流水，需替换时选择按日期覆盖。"}

def legacy_import_issues(book, rows):
	keys = list({key for row in rows for key in row.get("_legacy_source_keys", ())})
	existing = set()
	for offset in range(0, len(keys), 500):
		existing.update(frappe.get_all(ORIGIN, filters={"book": book.name, "source": "Import",
			"source_key": ["in", keys[offset:offset + 500]]}, pluck="source_key", limit_page_length=0))
	return [_legacy_import_issue(row) for row in rows if existing.intersection(row.get("_legacy_source_keys", ()))]


def _source_record(book, source, key):
	name = _source_id(book.name, source, key)
	return name, frappe.db.get_value(ORIGIN, name, ["entry", "digest"], as_dict=True, for_update=True)


def _register(book, source, key, name, entry, fingerprint):
	frappe.get_doc(
		dict(doctype=ORIGIN, book=book.name, source=source, source_key=key, entry=entry, digest=fingerprint)
	).insert(ignore_permissions=True, set_name=name)


def _range_entries(doc, start, end):
	filters = {BOOK_FIELD: doc.name, VOID: 0, "posting_date": ["between", [start, end]]}
	entries = frappe.get_list(ENTRY, filters={**filters, "currency": book_currency(doc)}, fields=["name", "direction", "amount"],
		order_by=f"posting_date asc, {SEQ} asc, name asc", limit_page_length=0)
	if len(entries) != frappe.db.count(ENTRY, filters):
		frappe.throw("所选日期范围内有无权访问或币种不符的流水，无法覆盖。", frappe.PermissionError)
	return entries


def ledger_export_matches(doc, rows):
	"""Check the complete exported date range without changing existing bank source bindings."""
	if not rows:
		return False
	first = as_date(min(row["date"] for row in rows))
	last = as_date(max(row["date"] for row in rows))
	native = _rows(doc, first, last + timedelta(days=1) if last < date.max else None)
	if len(native) != len(rows):
		return False
	prior = _prior(doc, first)
	for stored, raw in zip(native, rows, strict=True):
		prior += Decimal(stored["income"] or 0) - Decimal(stored["expense"] or 0)
		if raw.get("balance") is None or prior != Decimal(raw["balance"]):
			return False
		values = clean_row(raw)
		if any(stored.get(key) != values.get(key) for key in ("date", "income", "expense", "refs")):
			return False
		if any((stored.get(key) or "") != (values.get(key) or "") for key in TEXT):
			return False
	return True


def replacement_impact(doc, rows, opening, *, ledger_book=None):
	"""Use reviewed transaction dates, including both ends and intervening empty file days."""
	offset = _bank_offset(doc, ledger_book)
	start = min(row["date"] for row in rows)
	end = max(row["date"] for row in rows)
	entries = _range_entries(doc, start, end)
	prior = _prior(doc, as_date(start))
	old = prior + sum(Decimal(str(r.amount)) * (1 if r.direction == "income" else -1) for r in entries)
	running = prior
	issues = []
	if opening is not None and Decimal(opening) + offset != prior:
		issues.append({"row": rows[0]["source_row"], "field": "opening", "message": "文件期初余额与覆盖日前的账簿余额不符。"})
	for row in rows:
		running += Decimal(row["income"] or 0) - Decimal(row["expense"] or 0)
		if row.get("balance") is not None and running != Decimal(row["balance"]) + offset:
			issues.append({"row": row["source_row"], "field": "balance", "message": "文件余额与按覆盖日前余额重算的结果不符。"})
	return dict(start=start, end=end, old_count=len(entries), new_count=len(rows),
		opening=format(prior, ".2f"), old_closing=format(old, ".2f"), new_closing=format(running, ".2f"),
		difference=format(running - old, ".2f")), entries, issues


def _retire_entries(doc, entries, changes):
	# Keep the original native rows, sequence and complete metadata for undo.
	# Release only bank-file keys. A previously synchronized DingTalk payment
	# retains its receipt so the next sync cannot recreate a retired expense.
	for offset in range(0, len(entries), 500):
		chunk = entries[offset:offset + 500]
		names = [row.name for row in chunk]
		filters = {"book": doc.name, "source": "Import", "entry": ["in", names]}
		origins = frappe.get_all(ORIGIN, filters=filters, fields=["name", "source", "source_key", "entry", "digest"],
			limit_page_length=0)
		if origins:
			changes.append({"detached_sources": origins})
			frappe.db.delete(ORIGIN, filters)
		for item in chunk:
			entry = _authorized_entry(doc, item.name, "delete")
			entry.check_permission("write")
			changes.append({"entry": entry.name, "before": {VOID: 0}})
			entry.set(VOID, 1)
			entry.save()


def _import_balances(book, checks):
	if not checks:
		return
	# Verify evidence against the resulting native timeline, including
	# intervening stored rows and ordering of same-day installments.
	first, last = min(day for day, _, _ in checks), max(day for day, _, _ in checks)
	before = _prior(book, first)
	fields = ["name", "direction", "amount"]
	rows = frappe.get_list(
		ENTRY,
		filters={**_entry_scope(book), "posting_date": ["between", [first, last]]},
		fields=fields,
		order_by=f"posting_date asc, {SEQ} asc, name asc",
		limit_page_length=0,
	)
	expected = {name: amount for _, name, amount in checks}
	for r in rows:
		amount = Decimal(str(r.amount))
		before += amount if r.direction == "income" else -amount
		if r.name in expected and before != expected.pop(r.name):
			frappe.throw("银行流水余额与账簿不一致，请核对期初余额、日期及同日流水顺序。")
	if expected:
		frappe.throw("银行流水与当前用户可见的账簿明细不一致。")


def batch_replay(book, source, version, request_id, request_facts):
	"""Lock and replay a trusted source selection before reading a moving OA snapshot."""
	return _begin(book, "create", version, request_id, _source(source), request_facts)[3]


def apply_batch(
	book,
	rows,
	source,
	version,
	request_id,
	*,
	opening=None,
	opening_date=None,
	template=None,
	request_facts=None,
	new_book=None,
	replace_range=False,
	import_review=None,
	ledger_book=None,
):
	source = _source(source)
	if not isinstance(rows, list) or len(rows) > MAX_BATCH:
		frappe.throw("单次最多可导入一万笔流水。")
	# Trusted callers compute source IDs from source records, never from a
	# client-supplied preview or a client-submitted association URL.
	if opening is not None:
		opening = money(opening)
	payload = [rows, opening, str(opening_date or ""), template] if request_facts is None else request_facts
	if import_review is not None:
		payload = [payload, import_review]
	if ledger_book:
		payload = [payload, ledger_book]
	kind = source
	if replace_range:
		if source != "Import" or new_book is not None or not rows:
			frappe.throw("请选择已有账簿和含有效流水的文件。")
		authorize_replacement()
		kind = REPLACE_RANGE
	with _transaction(), managed():
		replay_only = False
		if new_book is not None:
			# Lock in the same order as manual creation. Only the original receipt
			# may replay once another request has created the proposed book.
			existing = _lock_current_snapshot(lambda: _lock_company_book(new_book.company, book))
			replay_only = bool(existing)
			if existing:
				_check_currency(existing, book_currency(new_book))
			else:
				new_book.bank_balance_offset = _initial_bank_offset(new_book, import_review)
				_insert_book(new_book)
		doc, ident, fp, done = _begin(book, "create", version, request_id, kind, payload, replay_only=replay_only)
		offset = _bank_offset(doc, ledger_book) if source == "Import" else Decimal(0)
		if source == "DingTalk":
			require_oa_currency(doc)
		if done:
			return done
		changes, checks, claimed = [], [], set()
		counts = dict(created=0, matched=0, skipped=0)
		if import_review is not None:
			counts["import_review"] = import_review
		# Map once per import so a 10k-row statement does not hit OA per row.
		clean = [clean_row(raw) for raw in rows]
		for raw in rows:
			if raw.get("currency") not in (None, ""):
				_check_currency(doc, raw["currency"])
			if "ledger_balance" in raw and (source != "Import" or new_book is None):
				frappe.throw("仅可按已确认的新账簿期初余额重算流水余额。")
			if "ledger_balance" in raw and Decimal(money(raw["ledger_balance"])) != Decimal(money(raw["balance"])) + offset:
				frappe.throw("流水余额与账簿的期初调整不一致。")
		if replace_range:
			impact, entries, issues = replacement_impact(doc, rows, opening, ledger_book=ledger_book)
			if issues:
				frappe.throw(issues[0]["message"])
			counts["impact"] = impact
		if ledger_book and ledger_export_matches(doc, rows):
			counts["skipped"] = len(rows)
			return _finish(doc, ident, fp, kind, [], result=counts)
		if ledger_book and not replace_range:
			frappe.throw("账面导出与现有流水不同，请核对或选择按日期覆盖。")
		if replace_range:
			_retire_entries(doc, entries, changes)
		lookup = list(dict.fromkeys(ref for row in clean for ref in row["refs"]))
		all_links = _links(doc.company, lookup)
		# Never fabricate an approval identity. Historic imports preserve the
		# literal number even if its matching OA instance is no longer available.
		if opening is not None and clean:
			first = min(as_date(r["date"]) for r in clean)
			if first >= as_date(str(doc.opening_date)) and not any(
				r.get("balance") is not None for r in rows
			):
				if _prior(doc, first) != Decimal(opening) + offset:
					frappe.throw("文件列示的期初余额与账簿不一致。")
		for raw, row in zip(rows, clean, strict=True):
			key = raw.get("source_key")
			if not isinstance(key, str) or not key.strip() or len(key) > 500:
				frappe.throw("流水缺少有效的来源标识。")
			row["bank_ref"] = str(raw.get("bank_ref") or "")
			if len(row["bank_ref"]) > 140:
				frappe.throw("银行流水号过长。")
			origin_id, receipt = _source_record(doc, source, key)
			if not receipt and not replace_range and source == "Import":
				for previous in raw.get("_legacy_source_keys", ()):
					_, old_receipt = _source_record(doc, source, previous)
					if old_receipt:
						frappe.throw(_legacy_import_issue(raw)["message"])
			fingerprint = batch_digest(row)
			review = raw.get("file_review") if source == "Import" else None
			if source == "Import" and raw.get("balance") not in (None, "") and (
				row["bank_ref"] or (review and "balance" in review.get("original", {}))
			):
				fingerprint = digest([fingerprint, money(raw["balance"])])
			if raw.get("weak_file_proof"):
				fingerprint = digest([fingerprint, raw["weak_file_proof"]])
			if receipt:
				if replace_range:
					frappe.throw(f"{row['date']} 的来源流水号已被覆盖范围以外或已取消的流水使用，请核对。")
				if receipt.digest != fingerprint:
					if raw.get("weak_file_proof"):
						frappe.throw(
							f"{row['date']} 有相同的日期、摘要和金额，但缺少银行流水号或余额，无法核实两份文件是否为同笔流水。"
						)
					frappe.throw(f"{row['date']} 的原流水已发生变化，请核对后再导入。")
				counts["skipped"] += 1
				continue
			name = _matching_entry(doc, source, row, claimed)
			if name:
				current = _authorized_entry(doc, name, "write")
				before = _snapshot(current)
				prior = _wire(current)
				# Preserve prior bank and manual fields; only add previously
				# missing citations/data from a proven matching source row.
				for key_field in TEXT:
					if prior[key_field]:
						row[key_field] = prior[key_field]
				row["refs"] = list(dict.fromkeys(prior["refs"] + row["refs"]))
				extra = _json(current.get(EXTRA), {})
				counts["matched"] += 1
			else:
				current = frappe.new_doc(ENTRY)
				before, extra = None, {"source": source}
				counts["created"] += 1
			if row["bank_ref"] and not current.reference_number:
				current.reference_number = row["bank_ref"]
			if review:
				extra.setdefault("file_review", {**review, "confirmed_by": frappe.session.user})
			if source == "Import" and raw.get("raw_cells") is not None:
				cells = raw["raw_cells"]
				if (
					not isinstance(cells, list)
					or len(cells) not in (14, 15)
					or not all(isinstance(v, str) for v in cells)
				):
					frappe.throw("交易行原始列无效。")
				extra.setdefault("raw_cells", cells)
			extra["links"] = {
				**extra.get("links", {}),
				**{r: all_links[r] for r in row["refs"] if r in all_links},
			}
			_write(current, row, doc, extra=extra, new=name is None)
			claimed.add(current.name)
			changes.append(dict(entry=current.name, before=before, origin=origin_id))
			_register(doc, source, key, origin_id, current.name, fingerprint)
			if raw.get("balance") not in (None, ""):
				# Source identity uses the bank's original balance. Only the
				# comparison to the native ledger uses its confirmed opening offset.
				checks.append((as_date(row["date"]), current.name, Decimal(money(raw["balance"])) + offset))
		_import_balances(doc, checks)
		if template and not doc.template_json and not replace_range:
			changes.append({"template_before": doc.template_json})
			doc.template_json = _dump(template)
		return _finish(doc, ident, fp, kind, changes, result=counts)


@frappe.whitelist(methods=["POST"])
def undo(book, operation, version, request_id):
	with _transaction(), managed():
		doc, ident, fp, done = _begin(book, "read", version, request_id, "Undo", operation)
		if done:
			return done
		if not operation or doc.last_operation != operation:
			frappe.throw("该操作之后已有其他修改，请刷新账簿。")
		prior = frappe.get_doc(OP, operation)
		if prior.book != book or prior.actor != frappe.session.user:
			frappe.throw("只可撤销自己的最近一次操作。", frappe.PermissionError)
		if prior.operation == REPLACE_RANGE:
			authorize_replacement()
		steps = _json(prior.changes, [])
		for step in reversed(steps):
			if "detached_sources" in step:
				for old in step["detached_sources"]:
					_register(doc, old["source"], old["source_key"], old["name"], old["entry"], old["digest"])
				continue
			if "template_before" in step:
				doc.template_json = step["template_before"]
				continue
			before = step["before"]
			entry = _authorized_entry(doc, step["entry"], "delete" if before is None else "write")
			if before is None:
				entry.set(VOID, 1)
			else:
				entry.update(before)
			entry.save()
			refs = [] if entry.get(VOID) else _json(entry.get(EXTRA), {}).get("refs", [])
			_reindex(doc.name, entry.name, refs)
			# An explicit undo restores the pre-import bindings. The request
			# receipt remains permanent: replaying the old request is harmless.
			if step.get("origin"):
				frappe.db.delete(ORIGIN, {"name": step["origin"], "book": doc.name})
		return _finish(doc, ident, fp, "Undo", [{"undone": operation}], new_last=prior.prior_operation or "")
