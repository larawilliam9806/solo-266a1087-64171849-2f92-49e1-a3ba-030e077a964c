"""端到端流程 + 版本发布/历史保留 + 并发确认测试。"""
import io
import threading
import time

import pytest

from app import create_app, storage
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


def _upload(client, temp=TEMP_CSV, handling=HANDLING_CSV, limit="8.0", name="测试趟次"):
    return client.post(
        "/trips",
        data={
            "name": name, "temp_limit": limit,
            "temp_file": (io.BytesIO(temp), "temp.csv"),
            "handling_file": (io.BytesIO(handling), "handling.csv"),
        },
        content_type="multipart/form-data",
        follow_redirects=True,
    )


def test_full_flow_preview_then_confirm(client):
    page = _upload(client)
    assert page.status_code == 200
    assert "修订预览".encode() in page.data
    assert b"BOX-1" in page.data and b"BOX-2" in page.data

    # 确认前不存在正式结论
    trips = storage.list_trips()
    assert trips == []

    # 从预览页拿到 trip_id
    trip_id = page.request.path.split("/")[-2]
    resp = client.post(f"/trips/{trip_id}/confirm", follow_redirects=True)
    assert resp.status_code == 200
    assert b"rev 1" in resp.data

    trip = storage.get_trip(trip_id)
    assert trip["revision"] == 1
    by_box = {r["box"]: r for r in trip["results"]}
    # BOX-1: 超温 5 分钟；未知 = 缺口 15 分钟 + 08:45 后 15 分钟
    assert by_box["BOX-1"]["hot_seconds"] == 300
    assert by_box["BOX-1"]["unknown_seconds"] == 1800
    # BOX-2 在车 08:05-08:45：未知仅缺口 15 分钟
    assert by_box["BOX-2"]["unknown_seconds"] == 900
    # 草稿已清除
    assert storage.get_draft(trip_id) is None


def test_bad_csv_flashes_error_and_no_trip(client):
    bad = b"timestamp_utc,celsius\nnot-a-time,5\n"
    page = _upload(client, temp=bad)
    assert "时间格式无法解析".encode() in page.data
    assert storage.list_trips() == []


def test_revision_publishes_new_evidence_but_keeps_history(client):
    page = _upload(client)
    trip_id = page.request.path.split("/")[-2]
    client.post(f"/trips/{trip_id}/confirm")

    # 修订：装卸文件删掉 BOX-2，只留 BOX-1，并收紧温度上限
    new_handling = (
        "box,loaded_at,unloaded_at\n"
        "BOX-1,2026-11-18T08:00:00Z,2026-11-18T09:00:00Z\n"
    ).encode()
    resp = client.post(
        f"/trips/{trip_id}/revise",
        data={
            "name": "测试趟次", "temp_limit": "4.5",
            "handling_file": (io.BytesIO(new_handling), "handling.csv"),
        },
        content_type="multipart/form-data",
        follow_redirects=True,
    )
    assert resp.status_code == 200 and "修订预览".encode() in resp.data
    client.post(f"/trips/{trip_id}/confirm", follow_redirects=True)

    trip = storage.get_trip(trip_id)
    assert trip["revision"] == 2
    boxes = {r["box"] for r in trip["results"]}
    assert boxes == {"BOX-1"}  # 新版不再包含删除的箱号

    # 新版（rev 2）明细中没有 BOX-2
    with storage.db_cursor() as conn:
        current = conn.execute(
            "SELECT COUNT(*) AS n FROM trip_result WHERE trip_id=? AND revision=2 AND box='BOX-2'",
            (trip_id,),
        ).fetchone()["n"]
        # 但 rev 1 的历史证据完整保留，没有被删除或重算
        old_ride = conn.execute(
            "SELECT COUNT(*) AS n FROM trip_ride WHERE trip_id=? AND revision=1 AND box='BOX-2'",
            (trip_id,),
        ).fetchone()["n"]
        old_result = conn.execute(
            "SELECT COUNT(*) AS n FROM trip_result WHERE trip_id=? AND revision=1 AND box='BOX-2'",
            (trip_id,),
        ).fetchone()["n"]
    assert current == 0
    assert old_ride == 1 and old_result == 1

    # 上限 4.5 后 BOX-1 出现更多超温段（rev 2 新证据）
    r = trip["results"][0]
    assert r["hot_seconds"] > 300

    # 历史接口可回查 rev 1 当时的完整证据
    rev1 = storage.get_trip_revision(trip_id, 1)
    assert rev1["traceable"] is True and rev1["revision"] == 1
    rev1_boxes = {ride["box"] for ride in rev1["rides"]}
    assert rev1_boxes == {"BOX-1", "BOX-2"}
    rev1_limits = {x["box"]: x for x in rev1["results"]}
    assert rev1_limits["BOX-1"]["hot_seconds"] == 300  # 旧上限 8.0 下的原始结论


def test_concurrent_confirmation_only_one_succeeds(app, client):
    page = _upload(client)
    trip_id = page.request.path.split("/")[-2]
    client.post(f"/trips/{trip_id}/confirm")

    # 基于 rev 1 造两个相同 expected_revision 的草稿（模拟两个会话同时点确认）
    readings = parse_temperature_csv(TEMP_CSV)
    rides = parse_handling_csv(HANDLING_CSV)
    from app.engine import analyze_all
    results = analyze_all(readings, rides, 8.0)
    storage.upsert_draft(trip_id, 1, "测试趟次", 8.0, readings, rides, results)

    outcomes: list[str] = []
    barrier = threading.Barrier(2)

    def worker():
        barrier.wait()
        with app.app_context():
            try:
                storage.commit_revision(trip_id, 1, "测试趟次", 8.0, readings, rides, results)
                outcomes.append("ok")
            except storage.RevisionConflict:
                outcomes.append("conflict")
            except Exception as exc:  # noqa: BLE001
                outcomes.append(f"error:{exc!r}")

    t1 = threading.Thread(target=worker)
    t2 = threading.Thread(target=worker)
    t1.start(); t2.start(); t1.join(); t2.join()

    assert sorted(outcomes) == ["conflict", "ok"]
    assert storage.get_trip(trip_id)["revision"] == 2


def test_stale_draft_confirmation_rejected(client):
    page = _upload(client)
    trip_id = page.request.path.split("/")[-2]
    client.post(f"/trips/{trip_id}/confirm")  # rev 1

    # 直接构造一个基于 rev 0 的过期草稿
    readings = parse_temperature_csv(TEMP_CSV)
    rides = parse_handling_csv(HANDLING_CSV)
    from app.engine import analyze_all
    storage.upsert_draft(
        trip_id, 0, "旧", 8.0, readings, rides, analyze_all(readings, rides, 8.0)
    )
    resp = client.post(f"/trips/{trip_id}/confirm", follow_redirects=True)
    assert "确认失败".encode() in resp.data
    assert storage.get_trip(trip_id)["revision"] == 1  # 正式版未变


def test_query_always_sees_complete_version(app, client):
    page = _upload(client)
    trip_id = page.request.path.split("/")[-2]
    client.post(f"/trips/{trip_id}/confirm")

    stop = threading.Event()
    errors: list[str] = []

    def reader():
        with app.app_context():
            while not stop.is_set():
                trip = storage.get_trip(trip_id)
                boxes_ride = {r["box"] for r in trip["rides"]}
                boxes_res = {r["box"] for r in trip["results"]}
                if boxes_ride != boxes_res:
                    errors.append("读到半成品：rides 与 results 箱号不一致")
                    return

    th = threading.Thread(target=reader)
    th.start()
    time.sleep(0.05)

    # 连续发布 5 个新版本，每版箱号集合都不同（旧版明细按修订号保留）
    for i in range(5):
        handling = (
            "box,loaded_at,unloaded_at\n"
            f"BOX-{i},2026-11-18T08:00:00Z,2026-11-18T09:00:00Z\n"
        ).encode()
        client.post(
            f"/trips/{trip_id}/revise",
            data={"name": "t", "temp_limit": "8.0",
                  "handling_file": (io.BytesIO(handling), "h.csv")},
            content_type="multipart/form-data",
        )
        client.post(f"/trips/{trip_id}/confirm")
    stop.set()
    th.join()
    assert errors == []
    assert storage.get_trip(trip_id)["revision"] == 6


def test_json_api(client):
    page = _upload(client)
    trip_id = page.request.path.split("/")[-2]
    client.post(f"/trips/{trip_id}/confirm")
    resp = client.get(f"/trips/{trip_id}/api/results")
    assert resp.status_code == 200
    assert resp.mimetype == "application/json"
    data = resp.get_json()
    assert data["revision"] == 1 and len(data["results"]) == 2
