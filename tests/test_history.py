"""历史版本功能测试：旧库回填、不可追溯、单版证据、草稿隔离与持久化。"""
import io
import json
import sqlite3

import pytest

from app import create_app, storage
from app.engine import analyze_all
from app.parser import parse_handling_csv, parse_temperature_csv

TEMP_CSV = (
    "timestamp_utc,celsius\n"
    "2026-11-18T08:00:00Z,5.0\n"
    "2026-11-18T08:05:00Z,6.0\n"
    "2026-11-18T08:10:00Z,9.0\n"
    "2026-11-18T08:15:00Z,7.0\n"
    "2026-11-18T08:40:00Z,6.0\n"
    "2026-11-18T08:45:00Z,4.0\n"
).encode()

HANDLING_CSV = (
    "box,loaded_at,unloaded_at\n"
    "BOX-1,2026-11-18T08:00:00Z,2026-11-18T09:00:00Z\n"
    "BOX-2,2026-11-18T08:05:00Z,2026-11-18T08:45:00Z\n"
).encode()

# 历史功能上线前的旧 schema（整趟替换、无 trip_revision 表）
LEGACY_SCHEMA = """
CREATE TABLE trip (
    id TEXT PRIMARY KEY, name TEXT NOT NULL, revision INTEGER NOT NULL,
    temp_limit REAL NOT NULL,
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
    confirmed_at TEXT
);
CREATE TABLE trip_reading (trip_id TEXT NOT NULL, revision INTEGER NOT NULL,
    ts INTEGER NOT NULL, celsius REAL NOT NULL);
CREATE TABLE trip_ride (trip_id TEXT NOT NULL, revision INTEGER NOT NULL,
    box TEXT NOT NULL, loaded_at INTEGER NOT NULL, unloaded_at INTEGER NOT NULL,
    PRIMARY KEY (trip_id, revision, box));
CREATE TABLE trip_result (trip_id TEXT NOT NULL, revision INTEGER NOT NULL,
    box TEXT NOT NULL, hot_seconds INTEGER NOT NULL, unknown_seconds INTEGER NOT NULL,
    total_seconds INTEGER NOT NULL, hot_segments TEXT NOT NULL,
    unknown_segments TEXT NOT NULL, PRIMARY KEY (trip_id, revision, box));
CREATE TABLE trip_draft (trip_id TEXT PRIMARY KEY, expected_revision INTEGER NOT NULL,
    name TEXT NOT NULL, temp_limit REAL NOT NULL, readings TEXT NOT NULL,
    rides TEXT NOT NULL, results TEXT NOT NULL,
    updated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')));
CREATE TABLE trip_draft_source (trip_id TEXT PRIMARY KEY, temp_csv TEXT NOT NULL,
    handling_csv TEXT NOT NULL);
"""


@pytest.fixture
def app(tmp_path):
    app = create_app(tmp_path / "test.db")
    app.config.update(TESTING=True)
    return app


@pytest.fixture(autouse=True)
def _app_context(app):
    with app.app_context():
        yield


@pytest.fixture
def client(app):
    return app.test_client()


def _upload(client, trip_temp=TEMP_CSV, trip_handling=HANDLING_CSV, limit="8.0"):
    return client.post(
        "/trips",
        data={
            "name": "历史测试", "temp_limit": limit,
            "temp_file": (io.BytesIO(trip_temp), "temp.csv"),
            "handling_file": (io.BytesIO(trip_handling), "handling.csv"),
        },
        content_type="multipart/form-data",
        follow_redirects=True,
    )


def _confirm_n_revisions(client, n):
    """确认 n 个版本，每版改温度上限，返回 trip_id。"""
    page = _upload(client)
    trip_id = page.request.path.split("/")[-2]
    client.post(f"/trips/{trip_id}/confirm")
    for limit in (7.0, 6.0, 5.0, 4.0, 3.0, 2.0):
        if storage.get_trip(trip_id)["revision"] >= n:
            break
        client.post(
            f"/trips/{trip_id}/revise",
            data={"name": "历史测试", "temp_limit": str(limit)},
            content_type="multipart/form-data",
        )
        client.post(f"/trips/{trip_id}/confirm")
    assert storage.get_trip(trip_id)["revision"] == n
    return trip_id


# ---------- 旧库一次性回填 ----------

def test_legacy_backfill_only_current_revision_traceable(tmp_path):
    db_path = tmp_path / "legacy.db"
    readings = parse_temperature_csv(TEMP_CSV)
    rides = parse_handling_csv(HANDLING_CSV)
    results = analyze_all(readings, rides, 8.0)

    # 手工构造一个"已经整趟替换到 rev 3"的旧库，明细只存 rev 3
    conn = sqlite3.connect(str(db_path))
    conn.executescript(LEGACY_SCHEMA)
    conn.execute(
        "INSERT INTO trip(id, name, revision, temp_limit, confirmed_at) "
        "VALUES('trip123','旧趟次',3,8.0,'2026-11-01T00:00:00.000Z')"
    )
    conn.executemany(
        "INSERT INTO trip_reading VALUES('trip123',3,?,?)", readings
    )
    conn.executemany(
        "INSERT INTO trip_ride VALUES('trip123',3,?,?,?)",
        [(box, lo, ul) for box, lo, ul in rides],
    )
    for box, r in results.items():
        conn.execute(
            "INSERT INTO trip_result VALUES('trip123',3,?,?,?,?,?,?)",
            (box, r["hot_seconds"], r["unknown_seconds"], r["total_seconds"],
             json.dumps(r["hot_segments"]), json.dumps(r["unknown_segments"])),
        )
    conn.commit()
    conn.close()

    # 用新代码打开旧库 -> 触发一次性回填
    app = create_app(db_path)
    with app.app_context():
        revs = storage.list_revisions("trip123")
        assert [r["revision"] for r in revs] == [1, 2, 3]
        assert [r["traceable"] for r in revs] == [False, False, True]

        rev3 = storage.get_trip_revision("trip123", 3)
        assert rev3["traceable"] is True
        assert rev3["readings"] == readings  # 现存正式版原样回填
        assert {ride["box"] for ride in rev3["rides"]} == {"BOX-1", "BOX-2"}
        assert rev3["temp_limit"] == 8.0

        # 更早版本：不可追溯，无任何证据字段，且不会拿新版数据重算
        for old_rev in (1, 2):
            old = storage.get_trip_revision("trip123", old_rev)
            assert old["traceable"] is False
            assert old["readings"] is None and old["rides"] is None
            assert old["results"] is None and old["note"]


def test_legacy_backfill_is_idempotent(tmp_path):
    db_path = tmp_path / "legacy2.db"
    conn = sqlite3.connect(str(db_path))
    conn.executescript(LEGACY_SCHEMA)
    conn.execute(
        "INSERT INTO trip(id, name, revision, temp_limit, confirmed_at) "
        "VALUES('t1','x',1,8.0,'2026-11-01T00:00:00.000Z')"
    )
    conn.commit()
    conn.close()

    app = create_app(db_path)
    with app.app_context():
        storage.init_db(db_path)  # 再次初始化不应重复插入或报错
        revs = storage.list_revisions("t1")
        assert len(revs) == 1 and revs[0]["traceable"] is True


# ---------- 新库：每次确认追加一版完整证据 ----------

def test_each_confirm_appends_independent_evidence_snapshot(client):
    trip_id = _confirm_n_revisions(client, 4)
    revs = storage.list_revisions(trip_id)
    assert [r["revision"] for r in revs] == [1, 2, 3, 4]
    assert all(r["traceable"] for r in revs)

    # 各版上限不同（8/7/6/5），证据各自独立、后续确认不改变旧版结论
    limits = [storage.get_trip_revision(trip_id, r)["temp_limit"] for r in range(1, 5)]
    assert limits == [8.0, 7.0, 6.0, 5.0]

    rev1 = storage.get_trip_revision(trip_id, 1)
    rev4 = storage.get_trip_revision(trip_id, 4)
    assert rev1["confirmed_at"] != rev4["confirmed_at"]
    # 同一箱号在两版下结论随上限不同而不同，但 rev 1 不被重算
    r1 = next(x for x in rev1["results"] if x["box"] == "BOX-1")
    r4 = next(x for x in rev4["results"] if x["box"] == "BOX-1")
    assert r1["hot_seconds"] == 300
    assert r4["hot_seconds"] > r1["hot_seconds"]
    # 读数在两版中一致（未重传温度文件）
    assert rev1["readings"] == rev4["readings"]


def test_revision_page_and_json_show_only_one_complete_version(client):
    trip_id = _confirm_n_revisions(client, 3)

    # 列表页
    page = client.get(f"/trips/{trip_id}/revisions")
    assert page.status_code == 200
    assert b"rev 1" in page.data and b"rev 2" in page.data and b"rev 3" in page.data

    # 单版页面：只呈现该修订号一版证据（列表卡片之外的证据卡片）
    page2 = client.get(f"/trips/{trip_id}/revisions?rev=2")
    assert page2.status_code == 200
    html = page2.data.decode()
    assert "rev 2 证据" in html
    evidence = html.split("rev 2 证据", 1)[1]
    assert "7.0 ℃" in evidence  # rev 2 上限
    assert "8.0 ℃" not in evidence and "6.0 ℃" not in evidence  # 不混入其他版上限
    assert "rev 1" not in evidence and "rev 3" not in evidence

    # 单版 JSON：结构完整且只含该修订号
    resp = client.get(f"/trips/{trip_id}/revisions/2/api")
    assert resp.status_code == 200 and resp.mimetype == "application/json"
    data = resp.get_json()
    assert data["revision"] == 2 and data["traceable"] is True
    assert data["temp_limit"] == 7.0
    assert len(data["readings"]) == 6 and len(data["rides"]) == 2
    assert {r["box"] for r in data["results"]} == {"BOX-1", "BOX-2"}
    for r in data["results"]:
        assert {"hot_seconds", "unknown_seconds", "total_seconds",
                "hot_segments", "unknown_segments"} <= set(r)

    # 最新正式版 JSON 规则照旧
    latest = client.get(f"/trips/{trip_id}/api/results").get_json()
    assert latest["revision"] == 3


def test_unknown_revision_returns_404(client):
    trip_id = _confirm_n_revisions(client, 2)
    assert client.get(f"/trips/{trip_id}/revisions/99/api").status_code == 404
    assert client.get(f"/trips/{trip_id}/revisions?rev=99").status_code == 404
    assert client.get("/trips/nonexistent/revisions").status_code == 404


def test_untraceable_revision_page_and_json_behavior(tmp_path):
    db_path = tmp_path / "legacy3.db"
    conn = sqlite3.connect(str(db_path))
    conn.executescript(LEGACY_SCHEMA)
    conn.execute(
        "INSERT INTO trip(id, name, revision, temp_limit, confirmed_at) "
        "VALUES('leg','旧',2,8.0,'2026-11-01T00:00:00.000Z')"
    )
    conn.commit()
    conn.close()

    app = create_app(db_path)
    client = app.test_client()
    with app.app_context():
        page = client.get("/trips/leg/revisions?rev=1")
        assert page.status_code == 200
        assert "不可追溯".encode() in page.data
        assert "不会用新版数据重算".encode() in page.data
        # 不可追溯版不提供 JSON 证据入口
        assert b"/revisions/1/api" not in page.data

        # 直接请求该版 JSON：明确返回不可追溯元信息，证据字段为 null
        resp = client.get("/trips/leg/revisions/1/api")
        assert resp.status_code == 200 and resp.mimetype == "application/json"
        data = resp.get_json()
        assert data["traceable"] is False and data["revision"] == 1
        assert data["readings"] is None and data["results"] is None
        assert data["note"]


def test_unconfirmed_draft_never_enters_history(client):
    page = _upload(client)
    trip_id = page.request.path.split("/")[-2]
    # 只有草稿、尚未确认
    assert storage.list_trips() == []
    assert client.get(f"/trips/{trip_id}/revisions").status_code == 404

    client.post(f"/trips/{trip_id}/confirm")  # rev 1
    # 再做一次修订但不确认
    client.post(
        f"/trips/{trip_id}/revise",
        data={"name": "历史测试", "temp_limit": "3.0"},
        content_type="multipart/form-data",
    )
    assert storage.get_draft(trip_id) is not None
    revs = storage.list_revisions(trip_id)
    assert [r["revision"] for r in revs] == [1]  # 草稿不入历史
    assert client.get(f"/trips/{trip_id}/revisions/2/api").status_code == 404


def test_history_persists_across_restart(tmp_path):
    db_path = tmp_path / "persist.db"
    app = create_app(db_path)
    with app.app_context():
        client = app.test_client()
        page = _upload(client)
        trip_id = page.request.path.split("/")[-2]
        client.post(f"/trips/{trip_id}/confirm")
        client.post(
            f"/trips/{trip_id}/revise",
            data={"name": "历史测试", "temp_limit": "4.0"},
            content_type="multipart/form-data",
        )
        client.post(f"/trips/{trip_id}/confirm")
        snapshot = storage.get_trip_revision(trip_id, 1)

    # 模拟进程重启：重新 create_app 打开同一个数据库文件
    app2 = create_app(db_path)
    with app2.app_context():
        revs = storage.list_revisions(trip_id)
        assert [r["revision"] for r in revs] == [1, 2]
        again = storage.get_trip_revision(trip_id, 1)
        assert again == snapshot  # 重启后旧版证据逐字节等价可回查
        assert storage.get_trip(trip_id)["revision"] == 2


def test_failed_confirm_keeps_previous_version_intact(client):
    trip_id = _confirm_n_revisions(client, 2)
    readings = parse_temperature_csv(TEMP_CSV)
    rides = parse_handling_csv(HANDLING_CSV)
    # 构造基于过期 rev 0 的草稿，确认必须被拒绝
    storage.upsert_draft(
        trip_id, 0, "过期", 8.0, readings, rides,
        analyze_all(readings, rides, 8.0),
    )
    resp = client.post(f"/trips/{trip_id}/confirm", follow_redirects=True)
    assert "确认失败".encode() in resp.data
    # 正式版与历史均保持旧状态，没有产生半截 rev 3
    assert storage.get_trip(trip_id)["revision"] == 2
    assert [r["revision"] for r in storage.list_revisions(trip_id)] == [1, 2]
    assert storage.get_draft(trip_id) is not None  # 草稿也未被清掉


def test_interleaved_reads_each_returns_single_consistent_version(client):
    """查看旧版与新确认交错时，每次读取都只返回一版完整证据。"""
    trip_id = _confirm_n_revisions(client, 3)

    seen = set()
    for rev in (1, 3, 2, 3, 1):
        data = client.get(f"/trips/{trip_id}/revisions/{rev}/api").get_json()
        # 单次响应内部自洽：修订号、上限、箱号结论同属一版
        assert data["revision"] == rev
        boxes_rides = {r["box"] for r in data["rides"]}
        boxes_results = {r["box"] for r in data["results"]}
        assert boxes_rides == boxes_results
        expected_limit = {1: 8.0, 2: 7.0, 3: 6.0}[rev]
        assert data["temp_limit"] == expected_limit
        seen.add(rev)
    assert seen == {1, 2, 3}
