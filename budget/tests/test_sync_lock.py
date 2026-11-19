from types import SimpleNamespace

from budget.services import sync_lock as locks


def test_status_reads_same_raw_site_key_as_acquisition_and_release(monkeypatch):
	storage = {}
	class Cache:
		def set(self, key, token, **kwargs):
			if key in storage:
				return False
			storage[key] = token
			return True
		def get(self, key):
			return storage.get(key)
		def exists(self, key):
			# Frappe RedisWrapper prefixes EXISTS but not raw SET/GET/EVAL.
			return f"database|{key}" in storage
		def eval(self, script, count, key, token):
			if storage.get(key) == token:
				del storage[key]
	monkeypatch.setattr(locks, "_cache_client", Cache)
	monkeypatch.setattr(locks.frappe, "local", SimpleNamespace(site="site-a"))
	monkeypatch.setattr(locks.frappe, "generate_hash", lambda **kw: "lock-token")
	assert not locks.is_locked("budget-ledger-sync")
	with locks.sync_lock("budget-ledger-sync") as acquired:
		assert acquired and locks.is_locked("budget-ledger-sync")
		with locks.sync_lock("budget-ledger-sync") as second:
			assert not second
		monkeypatch.setattr(locks.frappe, "local", SimpleNamespace(site="site-b"))
		assert not locks.is_locked("budget-ledger-sync")
		monkeypatch.setattr(locks.frappe, "local", SimpleNamespace(site="site-a"))
	assert not locks.is_locked("budget-ledger-sync")
