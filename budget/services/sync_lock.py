"""跨进程同步锁：Redis ``SET NX`` + token 校验删除。

为什么不用 ``frappe.utils.synchronization.filelock``：它基于 ``flock``，
在 Docker Desktop 的 macOS 挂载卷上实测**不互斥**——两个 bench 进程会同时进入
临界区，额度子表一度被插成 950 行（应为 475）。容器本地盘 ``/tmp`` 上则正常，
说明问题出在挂载卷的文件锁语义上，而 bench 目录恰好就挂在挂载卷里。

Redis 锁不依赖文件系统，跨进程 / 跨机器都有效，也是生产多 worker 部署下唯一
可靠的方案。缓存不可用时直接抛 ``SyncLockError``（fail-closed），不降级成无锁
写库——同步任务宁可这一轮不跑，也不能并发写坏数据。
"""

from __future__ import annotations

from contextlib import contextmanager
from typing import Iterator

import frappe

LOCK_TTL = 900

RELEASE_SCRIPT = (
	"if redis.call('get', KEYS[1]) == ARGV[1] then "
	"return redis.call('del', KEYS[1]) else return 0 end"
)


class SyncLockError(frappe.ValidationError):
	"""锁不可用（缓存故障或加锁失败）。"""


def _cache_client():
	cache = frappe.cache
	return cache() if callable(cache) else cache


def lock_key(name: str) -> str:
	"""按站点隔离，避免多站点共用 redis db 时互相抢锁。"""
	site = getattr(frappe.local, "site", None) or "unknown"
	return f"{site}:sync_lock:{name}"


@contextmanager
def sync_lock(name: str, *, ttl: int = LOCK_TTL) -> Iterator[bool]:
	"""拿到锁 yield ``True``；被别的进程占着 yield ``False``。

	拿不到锁不是错误，由调用方决定跳过还是提示；只有缓存本身不可用才抛错。
	"""
	try:
		cache = _cache_client()
	except Exception as error:
		raise SyncLockError(f"缓存不可用，无法加锁：{error}") from error

	key = lock_key(name)
	token = frappe.generate_hash(length=20)
	try:
		acquired = cache.set(key, token, nx=True, ex=ttl)
	except Exception as error:
		raise SyncLockError(f"加锁失败：{error}") from error

	if not acquired:
		yield False
		return

	try:
		yield True
	finally:
		try:
			cache.eval(RELEASE_SCRIPT, 1, key, token)
		except Exception:
			frappe.log_error(
				title=f"释放同步锁失败：{name}", message=frappe.get_traceback()
			)


def is_locked(name: str) -> bool:
	"""当前是否有同步在跑（给状态接口用）。"""
	try:
		# RedisWrapper.exists adds its own database prefix; set/eval above use raw,
		# already site-scoped keys. GET uses that same raw key without another prefix.
		return bool(_cache_client().get(lock_key(name)))
	except Exception:
		return False
