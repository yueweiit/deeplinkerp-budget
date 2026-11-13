"""PostgreSQL 只读连接入口：预算 app 内所有数据源共用。

设计要点：

1. **请求内复用**：按 ``(host, port, dbname, user)`` 缓存连接，同一请求里的多次
   查询（例如一个详情弹窗要读台账 3 张表 + 审批 5 张表）共用一条连接，不再
   每次 ``_fetch`` 都重新建连、认证、关闭。
2. **请求结束统一关闭**：``budget.hooks.after_request`` 注册 ``close_all``，
   Frappe 在响应返回后调用，避免连接泄漏到下一次请求。
3. **口径统一**：``connect_timeout`` / ``statement_timeout`` 由调用方传入的
   kwargs 决定，各数据源统一取 3s / 15s（见各 ``*_source.py`` 的 dataclass 默认值）。
4. **失败可诊断**：查询失败丢弃坏连接后重试一次，仍失败写 Error Log 并抛出，
   不再像以前那样静默降级成空数据。
"""

from __future__ import annotations

from typing import Any

import frappe

DEFAULT_CONNECT_TIMEOUT = 3
DEFAULT_STATEMENT_TIMEOUT_MS = 15000
DEFAULT_RETRIES = 1
DEFAULT_APPLICATION_NAME = "budget"

_POOL_ATTR = "budget_pg_connections"


def _default_connect(kwargs: dict) -> Any:
	import psycopg
	from psycopg.rows import dict_row

	return psycopg.connect(**kwargs, row_factory=dict_row)


def _pool() -> dict[str, Any] | None:
	"""当前请求的连接池；没有 Frappe 上下文（如单测）时返回 None，退化为不复用。"""
	try:
		pool = getattr(frappe.local, _POOL_ATTR, None)
		if pool is None:
			pool = {}
			setattr(frappe.local, _POOL_ATTR, pool)
		return pool
	except Exception:  # 无请求上下文时不影响单测
		return None


def _connection_key(kwargs: dict) -> str:
	return "|".join(
		str(kwargs.get(field) or "")
		for field in ("host", "port", "dbname", "user", "application_name")
	)


def _is_closed(connection: Any) -> bool:
	try:
		return bool(getattr(connection, "closed", False))
	except Exception:
		return True


def connection(kwargs: dict, *, connect=None) -> Any:
	"""取（或创建）本请求内复用的连接。

	显式传入 ``connect``（单测替身）时不走缓存，避免上一个用例的连接串到下一个用例。
	"""
	pool = _pool() if connect is None else None
	if pool is None:
		return (connect or _default_connect)(kwargs)
	key = _connection_key(kwargs)
	cached = pool.get(key)
	if cached is not None and not _is_closed(cached):
		return cached
	created = (connect or _default_connect)(kwargs)
	pool[key] = created
	return created


def discard(kwargs: dict, *, connect=None) -> None:
	"""丢弃指定目标上的缓存连接（查询失败后调用，下次重建）。"""
	pool = _pool()
	if pool is None:
		return
	key = _connection_key(kwargs)
	cached = pool.pop(key, None)
	if cached is not None:
		try:
			cached.close()
		except Exception:  # 关闭失败忽略
			pass


def close_all(**_kwargs: Any) -> None:
	"""``after_request`` 钩子：关闭本请求打开的所有连接。"""
	pool = _pool()
	if not pool:
		return
	for cached in list(pool.values()):
		try:
			cached.close()
		except Exception:
			pass
	pool.clear()


def fetch(
	kwargs: dict,
	sql: str,
	params: Any = None,
	*,
	connect=None,
	retries: int = DEFAULT_RETRIES,
	label: str = "budget",
) -> list[dict]:
	"""执行只读查询；连接失效/网络抖动时重建并重试一次。"""
	attempts = max(int(retries), 0) + 1
	last_error: Exception | None = None
	for _ in range(attempts):
		created_here = False
		pool = _pool() if connect is None else None
		owned = pool is None
		conn = None
		try:
			conn = connection(kwargs, connect=connect)
			created_here = owned
			with conn.cursor() as cursor:
				cursor.execute(sql, tuple(params or ()))
				rows = list(cursor.fetchall())
			# 只读事务及时收尾，避免复用连接长期挂着事务
			try:
				conn.commit()
			except Exception:  # 单测 fake 连接没有 commit
				pass
			return rows
		except Exception as exc:  # 重试期间保留最后一次错误
			last_error = exc
			try:
				conn.rollback()
			except Exception:
				pass
			if connect is None:
				discard(kwargs)
		finally:
			if created_here and conn is not None:
				try:
					conn.close()
				except Exception:
					pass

	if last_error is not None:
		try:
			frappe.log_error(message=frappe.get_traceback(), title=f"{label} 查询失败")
		except Exception:  # 日志失败不能盖住原始异常
			pass
		raise last_error
	return []
