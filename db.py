"""
Unified database for the Jackpot Tracker.

Schema is source-aware: every row carries a `source` column
('zitobox' or 'carnivalciti') so the dashboard can display both
providers in the same UI while keeping their data isolated.
"""
from __future__ import annotations

import os
import sqlite3
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Iterable, Optional

HERE = Path(__file__).resolve().parent
DB_NAME = HERE / "data" / "tracker.db"
BACKUP_DIR = HERE / "data" / "backups"
MAX_BACKUPS = 24


def _ensure_dirs() -> None:
    DB_NAME.parent.mkdir(parents=True, exist_ok=True)
    BACKUP_DIR.mkdir(parents=True, exist_ok=True)


@contextmanager
def get_conn():
    """Thread-safe connection context manager."""
    _ensure_dirs()
    conn = sqlite3.connect(str(DB_NAME), timeout=10)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def init_db() -> None:
    _ensure_dirs()
    with get_conn() as c:
        c.executescript(
            """
            CREATE TABLE IF NOT EXISTS games (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                source TEXT NOT NULL,
                code TEXT NOT NULL,
                name TEXT NOT NULL,
                url TEXT,
                jackpot_display TEXT,
                enabled INTEGER NOT NULL DEFAULT 1,
                order_index INTEGER NOT NULL DEFAULT 0,
                UNIQUE(source, code)
            );

            CREATE TABLE IF NOT EXISTS jackpot_values (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                source TEXT NOT NULL,
                game_code TEXT NOT NULL,
                game_name TEXT NOT NULL,
                timestamp TEXT NOT NULL,
                jackpot_value REAL NOT NULL,
                jackpot_name TEXT
            );

            CREATE INDEX IF NOT EXISTS idx_values_src_code
                ON jackpot_values(source, game_code, id);

            CREATE TABLE IF NOT EXISTS jackpot_hits (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                source TEXT NOT NULL,
                game_code TEXT NOT NULL,
                jackpot_name TEXT NOT NULL,
                amount REAL NOT NULL,
                player TEXT,
                hit_timestamp TEXT NOT NULL,
                recorded_at TEXT NOT NULL
            );
            """
        )
        # Migration: add order_index column to existing tables that were
        # created before this column existed. SQLite doesn't have
        # "ADD COLUMN IF NOT EXISTS" so we try and ignore the error.
        try:
            c.execute("ALTER TABLE games ADD COLUMN order_index INTEGER NOT NULL DEFAULT 0")
        except Exception:
            pass  # column already exists

        # Backfill order_index for rows that have it as 0.
        try:
            carnival_order = {
                "G321": 1, "G267": 2, "G182": 3, "G164": 4,
                "G242": 5, "G240": 6, "G325": 7,
            }
            for code, idx in carnival_order.items():
                c.execute(
                    "UPDATE games SET order_index = ? WHERE source = 'carnivalciti' AND code = ?",
                    (idx, code),
                )
            c.execute(
                """UPDATE games
                   SET order_index = (
                       SELECT COUNT(*) FROM games AS g2
                       WHERE g2.source = games.source AND g2.id <= games.id
                   )
                   WHERE source = 'zitobox' AND order_index = 0"""
            )
        except Exception:
            pass


def upsert_game(source: str, code: str, name: str, url: str = "", jackpot_display: str = "", order_index: int = 0) -> None:
    with get_conn() as c:
        c.execute(
            """
            INSERT INTO games (source, code, name, url, jackpot_display, order_index)
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(source, code) DO UPDATE SET
                name = excluded.name,
                url = excluded.url,
                jackpot_display = excluded.jackpot_display,
                order_index = excluded.order_index
            """,
            (source, code, name, url, jackpot_display, order_index),
        )


def list_games(source: Optional[str] = None) -> list[dict]:
    with get_conn() as c:
        if source:
            cur = c.execute(
                "SELECT source, code, name, url, jackpot_display, enabled FROM games "
                "WHERE source = ? ORDER BY order_index, name",
                (source,),
            )
        else:
            cur = c.execute(
                "SELECT source, code, name, url, jackpot_display, enabled FROM games "
                "ORDER BY source, order_index, name"
            )
        return [dict(r) for r in cur.fetchall()]


def save_jackpot(
    source: str,
    game_code: str,
    game_name: str,
    value: float,
    jackpot_name: str = "",
    ts: Optional[str] = None,
) -> None:
    # Round to whole units — the RGS returns cents, and after
    # dividing by 100 the values can have .xx decimals. We don't need
    # cent-level precision for a tracker, so store and display as
    # integers.
    rounded = int(round(float(value)))
    with get_conn() as c:
        c.execute(
            "INSERT INTO jackpot_values "
            "(source, game_code, game_name, timestamp, jackpot_value, jackpot_name) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (
                source,
                game_code,
                game_name,
                ts or datetime.now().isoformat(),
                rounded,
                jackpot_name,
            ),
        )
    print(f"  [{datetime.now().isoformat()}] [{source}] {game_name} -> {jackpot_name}: {rounded:,}")


def get_latest_values(source: Optional[str] = None) -> list[dict]:
    """Return the most recent jackpot reading per game, with sparkline data."""
    with get_conn() as c:
        if source:
            cur = c.execute(
                """
                SELECT v.source, v.game_code, v.game_name, v.jackpot_name,
                       v.jackpot_value, v.timestamp
                FROM jackpot_values v
                INNER JOIN (
                    SELECT game_code, MAX(id) AS max_id
                    FROM jackpot_values WHERE source = ?
                    GROUP BY game_code
                ) m ON v.id = m.max_id
                ORDER BY v.game_name
                """,
                (source,),
            )
        else:
            cur = c.execute(
                """
                SELECT v.source, v.game_code, v.game_name, v.jackpot_name,
                       v.jackpot_value, v.timestamp
                FROM jackpot_values v
                INNER JOIN (
                    SELECT source, game_code, MAX(id) AS max_id
                    FROM jackpot_values GROUP BY source, game_code
                ) m ON v.id = m.max_id
                ORDER BY v.source, v.game_name
                """
            )
        return [dict(r) for r in cur.fetchall()]


def get_history(
    source: str,
    game_code: str,
    limit: int = 500,
    ts_from: Optional[str] = None,
    ts_to: Optional[str] = None,
    min_value: Optional[float] = None,
    max_value: Optional[float] = None,
) -> list[dict]:
    """Return historical readings for a (source, game_code), optionally
    filtered by timestamp range and value range. ts_from / ts_to are
    ISO 8601 strings (inclusive lower, exclusive upper). min/max_value
    are numeric thresholds in display units (SC or USD, depends on source).
    """
    clauses = ["source = ?", "game_code = ?"]
    params: list = [source, game_code]
    if ts_from:
        clauses.append("timestamp >= ?")
        params.append(ts_from)
    if ts_to:
        clauses.append("timestamp < ?")
        params.append(ts_to)
    if min_value is not None:
        clauses.append("jackpot_value >= ?")
        params.append(min_value)
    if max_value is not None:
        clauses.append("jackpot_value <= ?")
        params.append(max_value)
    where = " AND ".join(clauses)
    params.append(limit)
    with get_conn() as c:
        cur = c.execute(
            f"SELECT id, timestamp, jackpot_value, jackpot_name FROM jackpot_values "
            f"WHERE {where} ORDER BY id ASC LIMIT ?",
            params,
        )
        rows = list(cur.fetchall())

    # Detect hits (value drops by more than 1.0 from the previous record)
    out: list[dict] = []
    prev: Optional[float] = None
    for r in rows:
        val = r["jackpot_value"]
        is_hit = prev is not None and val < (prev - 1.0)
        ts_raw = r["timestamp"]
        ts_display = ts_raw.replace("T", " ")[:19] if "T" in ts_raw else ts_raw
        out.append({
            "id": r["id"],
            "timestamp": ts_display,
            "timestamp_raw": ts_raw,
            "value": round(val, 2),
            "jackpot_name": r["jackpot_name"] or "",
            "is_hit": is_hit,
        })
        prev = val
    return out


def get_export_rows(
    source: str,
    ts_from: Optional[str] = None,
    ts_to: Optional[str] = None,
    min_value: Optional[float] = None,
    max_value: Optional[float] = None,
    games_filter: Optional[list[str]] = None,
) -> list[dict]:
    """Return rows across all games of a source, with filters applied.
    Used by the export/copy endpoint. Returns dicts ordered by timestamp ASC.
    """
    clauses = ["source = ?"]
    params: list = [source]
    if ts_from:
        clauses.append("timestamp >= ?")
        params.append(ts_from)
    if ts_to:
        clauses.append("timestamp < ?")
        params.append(ts_to)
    if min_value is not None:
        clauses.append("jackpot_value >= ?")
        params.append(min_value)
    if max_value is not None:
        clauses.append("jackpot_value <= ?")
        params.append(max_value)
    if games_filter:
        placeholders = ",".join("?" for _ in games_filter)
        clauses.append(f"game_code IN ({placeholders})")
        params.extend(games_filter)
    where = " AND ".join(clauses)
    with get_conn() as c:
        cur = c.execute(
            f"SELECT id, source, game_code, game_name, jackpot_name, jackpot_value, timestamp "
            f"FROM jackpot_values WHERE {where} ORDER BY timestamp ASC",
            params,
        )
        return [dict(r) for r in cur.fetchall()]


def format_export_tsv(rows: list[dict]) -> str:
    """Render rows as TSV (tab-separated) for easy paste into Excel/Sheets.
    Header line first; values are tab-separated; rows separated by \\n."""
    header = ["timestamp", "source", "game_code", "game_name", "jackpot_name", "value"]
    out = ["\t".join(header)]
    for r in rows:
        ts = (r.get("timestamp") or "").replace("T", " ")[:19]
        out.append("\t".join([
            ts,
            r.get("source") or "",
            r.get("game_code") or "",
            r.get("game_name") or "",
            r.get("jackpot_name") or "",
            str(int(r.get("jackpot_value") or 0)),
        ]))
    return "\n".join(out)


def get_sparkline(source: str, game_code: str, points: int = 30) -> list[dict]:
    with get_conn() as c:
        cur = c.execute(
            "SELECT jackpot_value, timestamp FROM jackpot_values "
            "WHERE source = ? AND game_code = ? ORDER BY id DESC LIMIT ?",
            (source, game_code, points),
        )
        rows = list(cur.fetchall())
    rows.reverse()
    return [{"value": r["jackpot_value"], "timestamp": r["timestamp"]} for r in rows]


def get_winners(source: str, game_code: str) -> list[dict]:
    with get_conn() as c:
        cur = c.execute(
            "SELECT jackpot_name, amount, player, hit_timestamp FROM jackpot_hits "
            "WHERE source = ? AND game_code = ? ORDER BY hit_timestamp DESC",
            (source, game_code),
        )
        return [dict(r) for r in cur.fetchall()]


def save_hit(
    source: str,
    game_code: str,
    jackpot_name: str,
    amount: float,
    player: str,
    hit_timestamp: str,
) -> None:
    with get_conn() as c:
        c.execute(
            "SELECT id FROM jackpot_hits WHERE source = ? AND game_code = ? "
            "AND jackpot_name = ? AND hit_timestamp = ?",
            (source, game_code, jackpot_name, hit_timestamp),
        )
        if c.fetchone():
            return
        c.execute(
            "INSERT INTO jackpot_hits "
            "(source, game_code, jackpot_name, amount, player, hit_timestamp, recorded_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (source, game_code, jackpot_name, amount, player, hit_timestamp, datetime.now().isoformat()),
        )


def get_stats() -> dict:
    with get_conn() as c:
        cur = c.execute("SELECT COUNT(*) AS n FROM jackpot_values")
        total_values = cur.fetchone()["n"]
        cur = c.execute("SELECT COUNT(DISTINCT source || '-' || game_code) AS n FROM jackpot_values")
        unique_games = cur.fetchone()["n"]
        cur = c.execute("SELECT source, COUNT(*) AS n FROM jackpot_values GROUP BY source")
        per_source = {r["source"]: r["n"] for r in cur.fetchall()}
        cur = c.execute("SELECT MAX(timestamp) AS m FROM jackpot_values")
        last_value_at = cur.fetchone()["m"]
        cur = c.execute("SELECT COUNT(*) AS n FROM jackpot_hits")
        total_hits = cur.fetchone()["n"]
    return {
        "total_values": total_values,
        "unique_games": unique_games,
        "per_source": per_source,
        "last_value_at": last_value_at,
        "total_hits": total_hits,
    }


def clear_source(source: str) -> int:
    with get_conn() as c:
        cur = c.execute("DELETE FROM jackpot_values WHERE source = ?", (source,))
        deleted_v = cur.rowcount
        cur = c.execute("DELETE FROM jackpot_hits WHERE source = ?", (source,))
        deleted_h = cur.rowcount
    return deleted_v + deleted_h


def delete_value_ids(ids: Iterable[int]) -> int:
    ids = [int(i) for i in ids]
    if not ids:
        return 0
    placeholders = ",".join("?" * len(ids))
    with get_conn() as c:
        cur = c.execute(f"DELETE FROM jackpot_values WHERE id IN ({placeholders})", ids)
        return cur.rowcount


def create_backup() -> str:
    _ensure_dirs()
    import shutil
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    dest = BACKUP_DIR / f"tracker_backup_{ts}.db"
    shutil.copy2(str(DB_NAME), str(dest))
    # Prune
    files = sorted(BACKUP_DIR.glob("tracker_backup_*.db"))
    while len(files) > MAX_BACKUPS:
        files.pop(0).unlink(missing_ok=True)
    return str(dest)
