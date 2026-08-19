from __future__ import annotations

import asyncio
import sqlite3
from collections.abc import Callable, Iterable, Sequence
from pathlib import Path
from typing import Any, TypeVar

from .schema import MIGRATIONS
from .timeutil import iso, utcnow

T = TypeVar("T")


class Database:
    """Small async wrapper around sqlite3.

    A fresh connection is opened per operation. Writes are serialized with a lock;
    WAL + busy_timeout allow safe concurrent reads. This keeps the project dependency
    footprint small and makes transactions explicit.
    """

    def __init__(self, path: Path | str):
        self.path = str(path)
        self._write_lock = asyncio.Lock()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path, timeout=10, isolation_level=None)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute("PRAGMA busy_timeout = 10000")
        conn.execute("PRAGMA synchronous = FULL")
        return conn

    async def current_schema_version(self) -> int:
        path = Path(self.path)
        if not path.exists() or path.stat().st_size == 0:
            return 0

        def _op() -> int:
            with self._connect() as conn:
                exists = conn.execute(
                    "SELECT 1 FROM sqlite_master WHERE type='table' AND name='schema_migrations'"
                ).fetchone()
                if not exists:
                    return 0
                row = conn.execute("SELECT COALESCE(MAX(version), 0) FROM schema_migrations").fetchone()
                return int(row[0] if row else 0)

        return await asyncio.to_thread(_op)

    async def initialize(self) -> None:
        Path(self.path).parent.mkdir(parents=True, exist_ok=True)

        def _init() -> None:
            with self._connect() as conn:
                conn.execute("PRAGMA journal_mode = WAL")
                conn.execute(
                    "CREATE TABLE IF NOT EXISTS schema_migrations (version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL)"
                )
                applied = {
                    int(row[0])
                    for row in conn.execute("SELECT version FROM schema_migrations")
                }
                for version, sql in MIGRATIONS:
                    if version in applied:
                        continue
                    stamp = iso(utcnow()).replace("'", "''")
                    # executescript performs an implicit commit before executing the script,
                    # therefore the migration carries its own explicit transaction.
                    script = (
                        "BEGIN IMMEDIATE;\n"
                        + sql
                        + f"\nINSERT INTO schema_migrations(version, applied_at) VALUES ({int(version)}, '{stamp}');\n"
                        + "COMMIT;"
                    )
                    try:
                        conn.executescript(script)
                    except Exception:
                        if conn.in_transaction:
                            conn.rollback()
                        raise

        async with self._write_lock:
            await asyncio.to_thread(_init)

    async def fetchone(self, sql: str, params: Sequence[Any] = ()) -> sqlite3.Row | None:
        def _op():
            with self._connect() as conn:
                return conn.execute(sql, params).fetchone()

        return await asyncio.to_thread(_op)

    async def fetchall(self, sql: str, params: Sequence[Any] = ()) -> list[sqlite3.Row]:
        def _op():
            with self._connect() as conn:
                return conn.execute(sql, params).fetchall()

        return await asyncio.to_thread(_op)

    async def execute(self, sql: str, params: Sequence[Any] = ()) -> int:
        def _op():
            with self._connect() as conn:
                conn.execute("BEGIN IMMEDIATE")
                try:
                    cur = conn.execute(sql, params)
                    lastrowid = int(cur.lastrowid or 0)
                    conn.commit()
                    return lastrowid
                except Exception:
                    conn.rollback()
                    raise

        async with self._write_lock:
            return await asyncio.to_thread(_op)

    async def executemany(self, sql: str, params: Iterable[Sequence[Any]]) -> None:
        rows = list(params)

        def _op():
            with self._connect() as conn:
                conn.execute("BEGIN IMMEDIATE")
                try:
                    conn.executemany(sql, rows)
                    conn.commit()
                except Exception:
                    conn.rollback()
                    raise

        async with self._write_lock:
            await asyncio.to_thread(_op)

    async def transaction(self, fn: Callable[[sqlite3.Connection], T]) -> T:
        def _op() -> T:
            with self._connect() as conn:
                conn.execute("BEGIN IMMEDIATE")
                try:
                    result = fn(conn)
                    conn.commit()
                    return result
                except Exception:
                    conn.rollback()
                    raise

        async with self._write_lock:
            return await asyncio.to_thread(_op)

    async def integrity_check(self) -> tuple[bool, str]:
        """Run SQLite's integrity check on the live database."""

        def _op() -> tuple[bool, str]:
            with self._connect() as conn:
                rows = conn.execute("PRAGMA integrity_check").fetchall()
                messages = [str(row[0]) for row in rows]
                ok = len(messages) == 1 and messages[0].lower() == "ok"
                return ok, "; ".join(messages)

        return await asyncio.to_thread(_op)

    async def backup_to(self, destination: Path | str) -> Path:
        """Create a consistent SQLite online backup.

        sqlite3.Connection.backup copies a coherent database snapshot even when the
        source uses WAL mode. The application write lock prevents our own writes
        from racing with the backup and makes the resulting checkpoint predictable.
        """
        target = Path(destination)
        target.parent.mkdir(parents=True, exist_ok=True)

        def _op() -> None:
            with self._connect() as source:
                with sqlite3.connect(target) as destination_conn:
                    source.backup(destination_conn)
                    check = destination_conn.execute("PRAGMA integrity_check").fetchone()
                    if not check or str(check[0]).lower() != "ok":
                        raise RuntimeError("Backup integrity check failed")

        async with self._write_lock:
            await asyncio.to_thread(_op)
        return target

    async def file_size(self) -> int:
        def _op() -> int:
            path = Path(self.path)
            return path.stat().st_size if path.exists() else 0

        return await asyncio.to_thread(_op)

