"""SQLite 存储层。

并发/一致性设计：
- 每个 trip 有单调递增 revision；确认提交采用条件更新
  UPDATE trip SET revision=? WHERE id=? AND revision=?，配合 BEGIN IMMEDIATE
  写事务，同一 revision 的并发确认只有一个事务能成功（其余提交 0 行 -> 409）。
- 每次确认在单个事务内：写入 trip_revision 历史快照 + 追加该版
  readings/rides/results 明细，任何一步失败全部回滚；查询永远只能看到
  完整旧版或完整新版，历史证据只增不改、不做整趟删除。
- 历史版本功能首次启用时，对旧库做一次性回填：旧库每趟只留存当前正式版
  的明细，故仅当前 revision 可追溯，更早修订号登记为"不可追溯"占位，
  绝不以新版数据重算充数。
- WAL 模式 + busy_timeout，兼顾读写并发与重启后的持久化。
"""
from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS trip (
    id              TEXT PRIMARY KEY,
    name            TEXT NOT NULL,
    revision        INTEGER NOT NULL,
    temp_limit      REAL NOT NULL,
    created_at      TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
    confirmed_at    TEXT
);
CREATE TABLE IF NOT EXISTS trip_reading (
    trip_id     TEXT NOT NULL,
    revision    INTEGER NOT NULL,
    ts          INTEGER NOT NULL,
    celsius     REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS trip_ride (
    trip_id     TEXT NOT NULL,
    revision    INTEGER NOT NULL,
    box         TEXT NOT NULL,
    loaded_at   INTEGER NOT NULL,
    unloaded_at INTEGER NOT NULL,
    PRIMARY KEY (trip_id, revision, box)
);
CREATE TABLE IF NOT EXISTS trip_result (
    trip_id         TEXT NOT NULL,
    revision        INTEGER NOT NULL,
    box             TEXT NOT NULL,
    hot_seconds     INTEGER NOT NULL,
    unknown_seconds INTEGER NOT NULL,
    total_seconds   INTEGER NOT NULL,
    hot_segments    TEXT NOT NULL,
    unknown_segments TEXT NOT NULL,
    PRIMARY KEY (trip_id, revision, box)
);
CREATE INDEX IF NOT EXISTS idx_ride_trip ON trip_ride(trip_id, revision);
CREATE INDEX IF NOT EXISTS idx_result_trip ON trip_result(trip_id, revision);
CREATE INDEX IF NOT EXISTS idx_reading_trip ON trip_reading(trip_id, revision);
-- 历史版本：每次确认追加一行，只增不改。traceable=0 表示该修订号的证据
-- 在历史功能启用前未留存，readings/rides/results 为 NULL，禁止重算补造。
CREATE TABLE IF NOT EXISTS trip_revision (
    trip_id      TEXT NOT NULL,
    revision     INTEGER NOT NULL,
    name         TEXT NOT NULL,
    temp_limit   REAL NOT NULL,
    confirmed_at TEXT NOT NULL,
    traceable    INTEGER NOT NULL,
    readings     TEXT,
    rides        TEXT,
    results      TEXT,
    PRIMARY KEY (trip_id, revision)
);
CREATE TABLE IF NOT EXISTS trip_draft (
    trip_id TEXT PRIMARY KEY, expected_revision INTEGER NOT NULL,
    name TEXT NOT NULL, temp_limit REAL NOT NULL,
    readings TEXT NOT NULL, rides TEXT NOT NULL, results TEXT NOT NULL,
    updated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
);
CREATE TABLE IF NOT EXISTS trip_draft_source (
    trip_id TEXT PRIMARY KEY, temp_csv TEXT NOT NULL, handling_csv TEXT NOT NULL
);
"""

UNTRACEABLE_NOTE = (
    "该修订号的证据在历史版本功能启用前未留存，不可追溯；"
    "未以任何新版数据重算或补造。"
)


class RevisionConflict(RuntimeError):
    """同一 revision 的确认已被其他事务抢先提交。"""


class NotFound(LookupError):
    pass


def get_db_path() -> Path:
    from flask import current_app

    return Path(current_app.config["DATABASE"])


def _connect(db_path: str | Path) -> sqlite3.Connection:
    # isolation_level=None：全程手动事务控制，避免 SELECT 隐式开启的事务
    # 让同一连接上的多条语句停留在旧快照，读到跨版本的混合数据
    conn = sqlite3.connect(str(db_path), timeout=30, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA busy_timeout=30000")
    conn.execute("PRAGMA synchronous=NORMAL")
    return conn


def init_db(db_path: str | Path) -> None:
    conn = _connect(db_path)
    try:
        conn.executescript(SCHEMA)
        # 一次性回填旧库现存正式版（幂等：已登记的修订号跳过）
        conn.execute("BEGIN IMMEDIATE")
        try:
            _backfill_legacy_revisions(conn)
            conn.commit()
        except Exception:
            conn.rollback()
            raise
    finally:
        conn.close()


def _backfill_legacy_revisions(conn: sqlite3.Connection) -> None:
    """历史版本功能首次启用时回填旧库。

    旧库采用整趟替换，每个 trip 只留存当前 revision 的明细，因此：
    - 更早的修订号 1..revision-1 只能登记为不可追溯占位（traceable=0），
      证据列留 NULL，绝不拿新版读数重算；
    - 当前 revision 用库中实际留存的 readings/rides/results 回填为可追溯。
    """
    trips = conn.execute(
        "SELECT id, name, temp_limit, revision, confirmed_at FROM trip"
    ).fetchall()
    for t in trips:
        trip_id, revision = t["id"], t["revision"]
        already = conn.execute(
            "SELECT COUNT(*) AS n FROM trip_revision WHERE trip_id=?", (trip_id,)
        ).fetchone()["n"]
        if already:
            continue

        # 更早修订号：无证据可回填，标明不可追溯
        for rev in range(1, revision):
            conn.execute(
                "INSERT OR IGNORE INTO trip_revision(trip_id, revision, name, "
                "temp_limit, confirmed_at, traceable, readings, rides, results) "
                "VALUES(?,?,?,?,?,0,NULL,NULL,NULL)",
                (trip_id, rev, t["name"], t["temp_limit"], t["confirmed_at"]),
            )

        # 当前正式版：以库中真实留存的证据回填，不做任何重新计算
        readings = conn.execute(
            "SELECT ts, celsius FROM trip_reading WHERE trip_id=? AND revision=? "
            "ORDER BY ts",
            (trip_id, revision),
        ).fetchall()
        rides = conn.execute(
            "SELECT box, loaded_at, unloaded_at FROM trip_ride "
            "WHERE trip_id=? AND revision=? ORDER BY box",
            (trip_id, revision),
        ).fetchall()
        result_rows = conn.execute(
            "SELECT * FROM trip_result WHERE trip_id=? AND revision=? ORDER BY box",
            (trip_id, revision),
        ).fetchall()
        results_dict = {
            r["box"]: {
                "hot_seconds": r["hot_seconds"],
                "unknown_seconds": r["unknown_seconds"],
                "total_seconds": r["total_seconds"],
                "hot_segments": json.loads(r["hot_segments"]),
                "unknown_segments": json.loads(r["unknown_segments"]),
            }
            for r in result_rows
        }
        conn.execute(
            "INSERT OR IGNORE INTO trip_revision(trip_id, revision, name, temp_limit, "
            "confirmed_at, traceable, readings, rides, results) "
            "VALUES(?,?,?,?,?,1,?,?,?)",
            (
                trip_id, revision, t["name"], t["temp_limit"], t["confirmed_at"],
                json.dumps([[r["ts"], r["celsius"]] for r in readings],
                           separators=(",", ":")),
                json.dumps([[r["box"], r["loaded_at"], r["unloaded_at"]] for r in rides],
                           separators=(",", ":")),
                json.dumps(results_dict, separators=(",", ":")),
            ),
        )


@contextmanager
def db_cursor(commit: bool = False):
    conn = _connect(get_db_path())
    try:
        if commit:
            # 写事务立即拿写锁，配合条件更新保证同修订号并发确认只成功一次
            conn.execute("BEGIN IMMEDIATE")
        else:
            # 显式只读事务：一次查询内的多条 SELECT 共享同一快照，
            # 绝不可能读到"新表头 + 旧明细"之类的跨版本混合状态
            conn.execute("BEGIN")
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


# ---------- 草稿（待确认的修订预览） ----------

def upsert_draft(
    trip_id: str,
    expected_revision: int,
    name: str,
    temp_limit: float,
    readings: list[tuple[int, float]],
    rides: list[tuple[str, int, int]],
    results: dict[str, dict],
) -> None:
    """保存草稿（同一 trip 反复修正只保留最新一份）。

    草稿带 expected_revision：确认时若 trip.revision 已前进则拒绝。
    草稿不属于正式版本，不会写入 trip_revision 历史。
    """
    with db_cursor(commit=True) as conn:
        conn.execute(
            "INSERT INTO trip_draft(trip_id, expected_revision, name, temp_limit, "
            "readings, rides, results) VALUES(?,?,?,?,?,?,?) "
            "ON CONFLICT(trip_id) DO UPDATE SET expected_revision=excluded.expected_revision, "
            "name=excluded.name, temp_limit=excluded.temp_limit, readings=excluded.readings, "
            "rides=excluded.rides, results=excluded.results, "
            "updated_at=strftime('%Y-%m-%dT%H:%M:%fZ','now')",
            (
                trip_id, expected_revision, name, temp_limit,
                json.dumps(readings, separators=(",", ":")),
                json.dumps(rides, separators=(",", ":")),
                json.dumps(results, separators=(",", ":")),
            ),
        )


def get_draft(trip_id: str) -> dict | None:
    with db_cursor() as conn:
        row = conn.execute(
            "SELECT * FROM trip_draft WHERE trip_id=?", (trip_id,)
        ).fetchone()
    if row is None:
        return None
    return {
        "trip_id": row["trip_id"],
        "expected_revision": row["expected_revision"],
        "name": row["name"],
        "temp_limit": row["temp_limit"],
        "readings": json.loads(row["readings"]),
        "rides": json.loads(row["rides"]),
        "results": json.loads(row["results"]),
    }


def delete_draft(conn: sqlite3.Connection, trip_id: str) -> None:
    conn.execute("DELETE FROM trip_draft WHERE trip_id=?", (trip_id,))


# ---------- 正式版本（已确认结论，历史只增不改） ----------

def commit_revision(
    trip_id: str,
    expected_revision: int,
    name: str,
    temp_limit: float,
    readings: list[tuple[int, float]],
    rides: list[tuple[str, int, int]],
    results: dict[str, dict],
) -> int:
    """原子提交一个新正式版本。返回新 revision；expected_revision 过期则抛冲突。

    新 trip（expected_revision=0 且不存在）创建为 revision 1。
    所有写入（trip 指针、trip_revision 历史快照、该版明细、草稿清理）在
    同一个 BEGIN IMMEDIATE 事务内完成：要么全部成功，要么全部回滚并保持
    旧版完整状态，不存在"发布成功但证据缺失"的中间态。
    """
    with db_cursor(commit=True) as conn:
        trip = conn.execute("SELECT revision FROM trip WHERE id=?", (trip_id,)).fetchone()
        current = trip["revision"] if trip else 0
        if current != expected_revision:
            raise RevisionConflict(
                f"修订号已过期：页面基于 rev {expected_revision}，当前为 rev {current}"
            )
        new_revision = current + 1
        confirmed_at = conn.execute(
            "SELECT strftime('%Y-%m-%dT%H:%M:%fZ','now') AS now"
        ).fetchone()["now"]

        if trip is None:
            conn.execute(
                "INSERT INTO trip(id, name, revision, temp_limit, confirmed_at) "
                "VALUES(?,?,?,?,?)",
                (trip_id, name, new_revision, temp_limit, confirmed_at),
            )
        else:
            updated = conn.execute(
                "UPDATE trip SET revision=?, name=?, temp_limit=?, confirmed_at=? "
                "WHERE id=? AND revision=?",
                (new_revision, name, temp_limit, confirmed_at, trip_id, current),
            ).rowcount
            if updated != 1:  # 并发下另一事务已推进 revision
                raise RevisionConflict("同一修订号的并发确认只允许一次")

        # 历史快照：整版证据一并落库，旧版行保留不动
        conn.execute(
            "INSERT INTO trip_revision(trip_id, revision, name, temp_limit, "
            "confirmed_at, traceable, readings, rides, results) "
            "VALUES(?,?,?,?,?,1,?,?,?)",
            (
                trip_id, new_revision, name, temp_limit, confirmed_at,
                json.dumps(readings, separators=(",", ":")),
                json.dumps(rides, separators=(",", ":")),
                json.dumps(results, separators=(",", ":")),
            ),
        )

        # 该版明细按修订号追加，不再删除任何旧版数据
        conn.executemany(
            "INSERT INTO trip_reading(trip_id, revision, ts, celsius) VALUES(?,?,?,?)",
            [(trip_id, new_revision, ts, c) for ts, c in readings],
        )
        conn.executemany(
            "INSERT INTO trip_ride(trip_id, revision, box, loaded_at, unloaded_at) "
            "VALUES(?,?,?,?,?)",
            [(trip_id, new_revision, box, lo, ul) for box, lo, ul in rides],
        )
        conn.executemany(
            "INSERT INTO trip_result(trip_id, revision, box, hot_seconds, "
            "unknown_seconds, total_seconds, hot_segments, unknown_segments) "
            "VALUES(?,?,?,?,?,?,?,?)",
            [
                (
                    trip_id, new_revision, box, r["hot_seconds"], r["unknown_seconds"],
                    r["total_seconds"],
                    json.dumps(r["hot_segments"], separators=(",", ":")),
                    json.dumps(r["unknown_segments"], separators=(",", ":")),
                )
                for box, r in results.items()
            ],
        )
        # 草稿及其 CSV 原文与正式发布在同一事务内清理
        delete_draft(conn, trip_id)
        conn.execute("DELETE FROM trip_draft_source WHERE trip_id=?", (trip_id,))
        return new_revision


def get_trip(trip_id: str) -> dict:
    """读取最新正式版（单一只读事务、同一快照，保证只见一版完整证据）。"""
    with db_cursor() as conn:
        trip = conn.execute("SELECT * FROM trip WHERE id=?", (trip_id,)).fetchone()
        if trip is None:
            raise NotFound(f"运输趟次不存在：{trip_id}")
        revision = trip["revision"]
        rides = conn.execute(
            "SELECT box, loaded_at, unloaded_at FROM trip_ride "
            "WHERE trip_id=? AND revision=? ORDER BY box", (trip_id, revision)
        ).fetchall()
        results = conn.execute(
            "SELECT * FROM trip_result WHERE trip_id=? AND revision=? ORDER BY box",
            (trip_id, revision),
        ).fetchall()
        readings = conn.execute(
            "SELECT ts, celsius FROM trip_reading WHERE trip_id=? AND revision=? ORDER BY ts",
            (trip_id, revision),
        ).fetchall()
    return {
        "id": trip["id"],
        "name": trip["name"],
        "revision": revision,
        "temp_limit": trip["temp_limit"],
        "created_at": trip["created_at"],
        "confirmed_at": trip["confirmed_at"],
        "readings": [(r["ts"], r["celsius"]) for r in readings],
        "rides": [dict(r) for r in rides],
        "results": [
            {
                "box": r["box"],
                "hot_seconds": r["hot_seconds"],
                "unknown_seconds": r["unknown_seconds"],
                "total_seconds": r["total_seconds"],
                "hot_segments": json.loads(r["hot_segments"]),
                "unknown_segments": json.loads(r["unknown_segments"]),
            }
            for r in results
        ],
    }


def list_trips() -> list[dict]:
    with db_cursor() as conn:
        rows = conn.execute(
            "SELECT t.id, t.name, t.revision, t.temp_limit, t.confirmed_at, "
            "(SELECT COUNT(*) FROM trip_ride r WHERE r.trip_id=t.id AND r.revision=t.revision) AS boxes "
            "FROM trip t ORDER BY t.created_at DESC"
        ).fetchall()
    return [dict(r) for r in rows]


# ---------- 历史版本 ----------

def list_revisions(trip_id: str) -> list[dict]:
    """列出某趟次全部已确认修订号（不含未确认草稿）。趟次不存在抛 NotFound。"""
    with db_cursor() as conn:
        trip = conn.execute("SELECT id FROM trip WHERE id=?", (trip_id,)).fetchone()
        if trip is None:
            raise NotFound(f"运输趟次不存在：{trip_id}")
        rows = conn.execute(
            "SELECT trip_id, revision, name, temp_limit, confirmed_at, traceable "
            "FROM trip_revision WHERE trip_id=? ORDER BY revision",
            (trip_id,),
        ).fetchall()
    return [
        {
            "trip_id": r["trip_id"],
            "revision": r["revision"],
            "name": r["name"],
            "temp_limit": r["temp_limit"],
            "confirmed_at": r["confirmed_at"],
            "traceable": bool(r["traceable"]),
        }
        for r in rows
    ]


def get_trip_revision(trip_id: str, revision: int) -> dict:
    """读取指定修订号的完整证据快照。

    可追溯版本返回该版当时的读数、装卸时段、温度上限、确认时间与逐箱结论；
    不可追溯版本只返回元信息与 traceable=False，证据字段为 None。
    不存在的 (trip_id, revision) 抛 NotFound。
    """
    with db_cursor() as conn:
        row = conn.execute(
            "SELECT * FROM trip_revision WHERE trip_id=? AND revision=?",
            (trip_id, revision),
        ).fetchone()
        if row is None:
            raise NotFound(f"运输趟次 {trip_id} 不存在修订号 rev {revision}")
        out = {
            "id": row["trip_id"],
            "revision": row["revision"],
            "name": row["name"],
            "temp_limit": row["temp_limit"],
            "confirmed_at": row["confirmed_at"],
            "traceable": bool(row["traceable"]),
        }
        if not row["traceable"]:
            out.update(readings=None, rides=None, results=None,
                       note=UNTRACEABLE_NOTE)
            return out

        readings = json.loads(row["readings"])
        rides_raw = json.loads(row["rides"])
        results_raw = json.loads(row["results"])
        out["readings"] = [(ts, c) for ts, c in readings]
        out["rides"] = [
            {"box": box, "loaded_at": lo, "unloaded_at": ul}
            for box, lo, ul in rides_raw
        ]
        out["results"] = [
            {"box": box, **results_raw[box]} for box in sorted(results_raw)
        ]
        return out
