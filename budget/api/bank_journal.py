"""Monthly RMB bank books on the existing, permission-checked native cash journal.

Book, source and operation records are private service metadata. Each write takes
one book row lock before touching native entries; Frappe commits the HTTP POST.
"""

from __future__ import annotations

import json
import re
from contextlib import contextmanager
from decimal import Decimal

import frappe
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
CORE = ("posting_date", "direction", "amount", "summary", "dingtalk_approval_number", "reference_number")
ROW_FIELDS = ["name", *CORE, EXTRA, SEQ, VOID]
MAX_BATCH = 10000


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
			fields=["name", "company", "account"],
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
		"books": books,
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
		BOOK, name, ["name", "opening", "opening_date"], as_dict=True, for_update=True
	)


@frappe.whitelist(methods=["POST"])
def create_book(company=None, account=None, opening=None, opening_date=None):
	_doctype_permission("create")
	if not isinstance(company, str) or not company.strip():
		frappe.throw("请选择银行账簿所属公司。")
	if company not in allowed_companies():
		frappe.throw("没有此公司的权限。", frappe.PermissionError)
	if not isinstance(account, str) or not account.strip() or len(account.strip()) > 140:
		frappe.throw("请填写银行账户的账簿名称。")
	account = account.strip()
	try:
		amount = money(opening)
	except ValueError:
		frappe.throw("请提供明确的人民币期初余额，最多保留两位小数。")
	try:
		start = as_date(opening_date)
	except ValueError:
		frappe.throw("请填写有效的账簿启用日期（YYYY-MM-DD）。")
	name = digest([company, account])
	with _transaction():
		existing = _lock_current_snapshot(lambda: _lock_company_book(company, name))
		if existing:
			if money(existing.opening) != amount or str(existing.opening_date) != str(start):
				frappe.throw("该账户已有账簿，期初设置不同。")
		else:
			frappe.get_doc(
				dict(
					doctype=BOOK,
					company=company,
					account=account,
					opening=amount,
					opening_date=start,
					revision=0,
					next_sequence=0,
				)
			).insert(ignore_permissions=True, set_name=name)
	return {"name": name, "book": name, "account": account, "company": company}


def _wire(native):
	extra = _json(native.get(EXTRA), {})
	direct, amount = native.get("direction"), money(native.get("amount"), positive=True)
	values = {k: extra.get(k, "") for k in TEXT}
	values.update(
		name=native.get("name"),
		date=str(native.get("posting_date")),
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


def _rows(book, start=None, end=None):
	filters = [[BOOK_FIELD, "=", book.name], [VOID, "=", 0]]
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
              from `tab{ENTRY}` where {BOOK_FIELD}=%s and {VOID}=0 and posting_date < %s"""
		+ get_match_cond(ENTRY),
		(book.name, before),
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
	if doc.last_operation and frappe.db.get_value(OP, doc.last_operation, "actor") == frappe.session.user:
		result["undo_operation"] = doc.last_operation
	return result


def export_rows(book, month=None):
	doc = authorize_book(book, "export")
	return {**_period(doc, month, full=month is None), "template": _json(doc.template_json)}


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
			filters={BOOK_FIELD: doc.name, VOID: 0, "name": ["in", sorted(names)]},
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


def _begin(book, permission, version, request_id, kind, payload):
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
	if doc.get(BOOK_FIELD) != book.name or doc.company != book.company or doc.currency != "CNY":
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
	extra.update({key: row[key] for key in (*TEXT, "refs") if key != "summary"})
	doc.set(EXTRA, _dump(extra))
	if new:
		book.next_sequence = int(book.next_sequence or 0) + 1
		doc.update({BOOK_FIELD: book.name, SEQ: book.next_sequence, VOID: 0})
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
		if not name:
			current.update(dict(company=doc.company, currency="CNY"))
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
			filters={BOOK_FIELD: book.name, VOID: 0, "reference_number": row["bank_ref"]},
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


def _source_record(book, source, key):
	name = _source_id(book.name, source, key)
	return name, frappe.db.get_value(ORIGIN, name, ["entry", "digest"], as_dict=True, for_update=True)


def _register(book, source, key, name, entry, fingerprint):
	frappe.get_doc(
		dict(doctype=ORIGIN, book=book.name, source=source, source_key=key, entry=entry, digest=fingerprint)
	).insert(ignore_permissions=True, set_name=name)


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
		filters=[[BOOK_FIELD, "=", book.name], [VOID, "=", 0], ["posting_date", "between", [first, last]]],
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
):
	source = _source(source)
	if not isinstance(rows, list) or len(rows) > MAX_BATCH:
		frappe.throw("单次最多可导入一万笔流水。")
	# Trusted callers compute source IDs from source records, never from a
	# client-supplied preview or a client-submitted association URL.
	if opening is not None:
		opening = money(opening)
	payload = [rows, opening, str(opening_date or ""), template] if request_facts is None else request_facts
	with _transaction(), managed():
		doc, ident, fp, done = _begin(book, "create", version, request_id, source, payload)
		if done:
			return done
		changes, checks, claimed = [], [], set()
		counts = dict(created=0, matched=0, skipped=0)
		# Map once per import so a 10k-row statement does not hit OA per row.
		clean = [clean_row(raw) for raw in rows]
		lookup = list(dict.fromkeys(ref for row in clean for ref in row["refs"]))
		all_links = _links(doc.company, lookup)
		# Never fabricate an approval identity. Historic imports preserve the
		# literal number even if its matching OA instance is no longer available.
		if opening is not None and clean:
			first = min(as_date(r["date"]) for r in clean)
			if first >= as_date(str(doc.opening_date)) and not any(
				r.get("balance") is not None for r in rows
			):
				if _prior(doc, first) != Decimal(opening):
					frappe.throw("文件列示的期初余额与账簿不一致。")
		for raw, row in zip(rows, clean, strict=True):
			key = raw.get("source_key")
			if not isinstance(key, str) or not key.strip() or len(key) > 500:
				frappe.throw("流水缺少有效的来源标识。")
			row["bank_ref"] = str(raw.get("bank_ref") or "")
			if len(row["bank_ref"]) > 140:
				frappe.throw("银行流水号过长。")
			origin_id, receipt = _source_record(doc, source, key)
			fingerprint = batch_digest(row)
			if source == "Import" and row["bank_ref"] and raw.get("balance") not in (None, ""):
				fingerprint = digest([fingerprint, money(raw["balance"])])
			if raw.get("weak_file_proof"):
				fingerprint = digest([fingerprint, raw["weak_file_proof"]])
			if receipt:
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
				current.update(dict(company=doc.company, currency="CNY"))
				before, extra = None, {"source": source}
				counts["created"] += 1
			if row["bank_ref"] and not current.reference_number:
				current.reference_number = row["bank_ref"]
			if source == "Import" and raw.get("raw_cells") is not None:
				cells = raw["raw_cells"]
				if (
					not isinstance(cells, list)
					or len(cells) != 14
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
				checks.append((as_date(row["date"]), current.name, Decimal(money(raw["balance"]))))
		_import_balances(doc, checks)
		if template and not doc.template_json:
			changes.append({"template_before": doc.template_json})
			doc.template_json = _dump(template)
		return _finish(doc, ident, fp, source, changes, result=counts)


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
		steps = _json(prior.changes, [])
		for step in reversed(steps):
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
