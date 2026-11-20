"""Exercise Redis Lock's real wait behavior with an in-memory transport, no site IO."""
import threading
from concurrent.futures import ThreadPoolExecutor

import frappe
import pytest
from redis.lock import Lock

from budget.integrations import cn_approval_source as source


class MemoryLock(Lock):
	def register_scripts(self):
		pass

	def do_acquire(self, token):
		acquired = self.redis.mutex.acquire(blocking=False)
		if not acquired:
			self.redis.waiting.set()
		return acquired

	def do_release(self, token):
		self.redis.mutex.release()


class MemoryCache:
	def __init__(self, value=None):
		self.value = value
		self.mutex = threading.Lock()
		self.waiting = threading.Event()
		self.lock_names = []
		self.writes = []

	def get_value(self, key, *, use_local_cache=True):
		# A Frappe request may have cached None or an old snapshot before waiting.
		assert use_local_cache is False
		return self.value

	def make_key(self, key):
		return f"test-site|{key}".encode()

	def set_value(self, key, value, **kwargs):
		self.value = value
		self.writes.append((key, kwargs))

	def lock(self, key, **kwargs):
		self.lock_names.append(key)
		return MemoryLock(self, key, **kwargs)


@pytest.fixture(autouse=True)
def runtime(monkeypatch):
	monkeypatch.setattr(source, "configuration", lambda: ("unused", []))
	monkeypatch.setattr(source, "_", lambda text: text)
	monkeypatch.setattr(source, "SNAPSHOT_WAIT_SECONDS", 3)
	monkeypatch.setattr(frappe, "throw", lambda text: (_ for _ in ()).throw(ValueError(text)))


@pytest.mark.parametrize("refresh", [False, True])
def test_concurrent_readers_share_one_completed_scan_even_after_one_second(monkeypatch, refresh):
	previous = {"rows": ["old"], "snapshot_id": "previous"} if refresh else None
	cache = MemoryCache(previous)
	started, finish = threading.Event(), threading.Event()
	reads = []
	def read(*args):
		reads.append(1)
		started.set()
		assert finish.wait(5)
		return {"rows": ["new"], "fetched_at": "2026-11-20"}
	monkeypatch.setattr(frappe, "cache", cache)
	monkeypatch.setattr(source, "load_snapshot", read)
	with ThreadPoolExecutor(2) as pool:
		first = pool.submit(source.get_snapshot, refresh=refresh)
		try:
			assert started.wait(2)
			second = pool.submit(source.get_snapshot, refresh=refresh)
			assert cache.waiting.wait(2)
			# The old one-second lock timeout used to fail the second reader here.
			assert not threading.Event().wait(1.1)
			assert not second.done()
		finally:
			finish.set()
		result = first.result(timeout=3)
		assert second.result(timeout=3) == result
	assert result["rows"] == ["new"] and result["snapshot_id"] != "previous"
	assert reads == [1] and len(cache.writes) == 1
	assert cache.writes[0][1]["expires_in_sec"] == source.TTL
	assert all(name.startswith(b"test-site|") for name in cache.lock_names)


def test_failed_concurrent_refresh_keeps_old_cache_without_reporting_it_as_fresh(monkeypatch):
	previous = {"rows": ["old"], "snapshot_id": "previous"}
	cache = MemoryCache(previous)
	started, finish = threading.Event(), threading.Event()
	def read(*args):
		started.set()
		assert finish.wait(5)
		raise RuntimeError("source unavailable")
	monkeypatch.setattr(frappe, "cache", cache)
	monkeypatch.setattr(source, "load_snapshot", read)
	with ThreadPoolExecutor(2) as pool:
		first = pool.submit(source.get_snapshot, refresh=True)
		try:
			assert started.wait(2)
			second = pool.submit(source.get_snapshot, refresh=True)
			assert cache.waiting.wait(2)
		finally:
			finish.set()
		with pytest.raises(RuntimeError):
			first.result(timeout=3)
		with pytest.raises(ValueError, match="did not complete"):
			second.result(timeout=3)
	assert cache.value is previous and cache.writes == []
	assert source.get_snapshot() is previous


def test_lock_wait_is_bounded_and_does_not_release_another_owner(monkeypatch):
	cache = MemoryCache()
	monkeypatch.setattr(frappe, "cache", cache)
	monkeypatch.setattr(source, "SNAPSHOT_WAIT_SECONDS", 0.01)
	monkeypatch.setattr(source, "load_snapshot", lambda *args: pytest.fail("scan without lock"))
	cache.mutex.acquire()
	try:
		with pytest.raises(ValueError, match="being refreshed"):
			source.get_snapshot()
		assert cache.mutex.locked() and cache.writes == []
	finally:
		cache.mutex.release()
