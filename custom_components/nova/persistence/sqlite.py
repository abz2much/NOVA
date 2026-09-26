"""SQLite connection policy and schema upgrades for Nova's own stores.

Two entry points apply the one schema in schema.py:

ensure(conn, *components)
    Idempotent, called by each store's own connect path exactly as before
    (a fresh or older database gets its tables and columns on first use).
    Raises on a genuine failure; the owning module keeps its existing
    fail-open or raise behaviour around it.

upgrade_existing(config_dir)
    Run once per setup, in the executor, before anything reads the stores.
    Each database that already exists is upgraded inside one BEGIN IMMEDIATE
    transaction, serialised per file within the process, verified against
    the real schema (PRAGMA, never the ledger alone), and only then stamped
    in its nova_schema_components ledger. A failure rolls the whole file
    back, is logged, and degrades that one store: setup continues, the
    store keeps its existing first-use behaviour, and the next setup
    retries. Missing databases are left for their owners to create lazily,
    as today. Older releases ignore the ledger table and the added columns.
"""
from __future__ import annotations

import logging
import sqlite3
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from .schema import COMPONENTS, LEDGER_DDL, LEDGER_TABLE, STORES, Component, Store

_LOGGER = logging.getLogger(__name__)

BUSY_TIMEOUT_MS = 10000


@dataclass(frozen=True)
class StoreStatus:
    """Outcome of one store's setup upgrade. state is one of: absent (no
    file yet, nothing to do), current, upgraded, failed."""
    store: str
    state: str
    error: str = ""

    @property
    def ok(self) -> bool:
        return self.state != "failed"


_path_locks: dict[str, threading.Lock] = {}
_path_locks_guard = threading.Lock()
_last_upgrade: dict[str, StoreStatus] = {}


def connect(path, *, timeout: float = 5.0,
            busy_timeout_ms: Optional[int] = BUSY_TIMEOUT_MS,
            wal: bool = True, row_factory: bool = True,
            mkdir: bool = True) -> sqlite3.Connection:
    """Open a store with Nova's usual pragmas. Arguments reproduce each
    owner's existing choices (some stores never used WAL or a busy
    timeout pragma, and that is preserved)."""
    if mkdir:
        Path(path).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path), timeout=timeout)
    if wal:
        conn.execute("PRAGMA journal_mode=WAL")
    if busy_timeout_ms is not None:
        conn.execute(f"PRAGMA busy_timeout={int(busy_timeout_ms)}")
    if row_factory:
        conn.row_factory = sqlite3.Row
    return conn


def _columns(conn, table: str) -> set:
    return {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}


def ensure_column(conn, table: str, column: str, ddl: str) -> bool:
    """Add one nullable or defaulted column if it is missing. Returns True
    when this call added it. A concurrent connection adding the same column
    first is success, proven by a second PRAGMA; any other failure raises."""
    if column in _columns(conn, table):
        return False
    try:
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {ddl}")
    except sqlite3.OperationalError:
        if column not in _columns(conn, table):
            raise
        return False
    _LOGGER.info("Nova storage: added %s.%s", table, column)
    return True


def _apply(conn, component: Component) -> None:
    for sql in component.statements:
        conn.execute(sql)
    for col in component.columns:
        ensure_column(conn, col.table, col.name, col.ddl)
    for sql in component.post:
        conn.execute(sql)


def ensure(conn, *names: str) -> None:
    """Apply the named components' schema to an open connection."""
    for name in names:
        _apply(conn, COMPONENTS[name])


def _table_exists(conn, table: str) -> bool:
    return conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,),
    ).fetchone() is not None


def verify(conn, *names: str) -> list[str]:
    """What the real schema is missing for these components (empty when
    complete). Reads PRAGMA/sqlite_master, never the ledger."""
    missing = []
    for name in names:
        comp = COMPONENTS[name]
        for table in comp.tables:
            if not _table_exists(conn, table):
                missing.append(table)
        for col in comp.columns:
            if col.name not in _columns(conn, col.table):
                missing.append(f"{col.table}.{col.name}")
    return missing


def ledger(conn) -> dict[str, int]:
    """Recorded component versions, {} when the ledger does not exist."""
    if not _table_exists(conn, LEDGER_TABLE):
        return {}
    return {row[0]: row[1] for row in conn.execute(
        f"SELECT component, version FROM {LEDGER_TABLE}")}


def _lock_for(path: str) -> threading.Lock:
    with _path_locks_guard:
        return _path_locks.setdefault(path, threading.Lock())


def upgrade_store(path, store: Store) -> StoreStatus:
    """Upgrade one existing database file in a single transaction."""
    path = str(path)
    if not Path(path).is_file():
        return StoreStatus(store.key, "absent")
    with _lock_for(path):
        conn = None
        try:
            conn = sqlite3.connect(path, timeout=BUSY_TIMEOUT_MS / 1000,
                                   isolation_level=None)
            conn.execute(f"PRAGMA busy_timeout={BUSY_TIMEOUT_MS}")
            if store.wal:
                conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("BEGIN IMMEDIATE")
            try:
                recorded = ledger(conn)
                ensure(conn, *store.components)
                conn.execute(LEDGER_DDL)
                missing = verify(conn, *store.components)
                if missing:
                    raise sqlite3.OperationalError(
                        "schema incomplete after upgrade: " + ", ".join(missing))
                stamp = time.strftime("%Y-%m-%dT%H:%M:%S")
                changed = False
                for name in store.components:
                    version = COMPONENTS[name].version
                    if recorded.get(name) != version:
                        changed = True
                        conn.execute(
                            f"INSERT OR REPLACE INTO {LEDGER_TABLE} "
                            "(component, version, applied_at) VALUES (?, ?, ?)",
                            (name, version, stamp))
                conn.execute("COMMIT")
            except BaseException:
                if conn.in_transaction:
                    conn.execute("ROLLBACK")
                raise
            return StoreStatus(store.key, "upgraded" if changed else "current")
        except Exception as exc:
            _LOGGER.warning(
                "Nova storage: %s upgrade failed, store left unchanged "
                "and retried next start: %s", store.key, exc)
            return StoreStatus(store.key, "failed", f"{type(exc).__name__}: {exc}")
        finally:
            if conn is not None:
                conn.close()


def upgrade_existing(config_dir) -> dict[str, StoreStatus]:
    """Setup-time upgrade of every existing store. Never raises."""
    results: dict[str, StoreStatus] = {}
    for store in STORES:
        if not store.upgrade_at_setup:
            continue
        try:
            results[store.key] = upgrade_store(Path(config_dir) / store.relpath, store)
        except Exception as exc:  # defensive: upgrade_store already catches
            results[store.key] = StoreStatus(store.key, "failed", str(exc))
    _last_upgrade.clear()
    _last_upgrade.update(results)
    return results


def last_upgrade() -> dict[str, StoreStatus]:
    """The most recent setup upgrade's per-store outcome (internal)."""
    return dict(_last_upgrade)
