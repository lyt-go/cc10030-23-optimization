"""路径巡检专用路由的 ASGI 级集成测试（不引入 httpx 依赖，直接驱动 ASGI 应用）。"""
from __future__ import annotations

import asyncio
import json
import shutil
import tempfile
from pathlib import Path

import pytest


@pytest.fixture(autouse=True)
def setup_data_dir():
    tmpdir = Path(tempfile.mkdtemp(prefix="geo_router_"))
    import app.services.base as base_mod
    original_root = base_mod.DATA_ROOT
    base_mod.DATA_ROOT = tmpdir
    for sub in ["geo_points", "coord_transformed", "track_segments", "checkpoints"]:
        (tmpdir / sub).mkdir(parents=True, exist_ok=True)
    yield tmpdir
    base_mod.DATA_ROOT = original_root
    shutil.rmtree(tmpdir, ignore_errors=True)


class AsgiCaller:
    """最小 ASGI 调用器：覆盖本服务用到的 GET/POST + JSON 响应。"""

    def __init__(self, app):
        self.app = app

    def request(self, method: str, path: str, payload: dict | None = None) -> tuple[int, dict]:
        body = json.dumps(payload).encode("utf-8") if payload is not None else b""
        headers = [(b"content-type", b"application/json")] if payload is not None else []
        if "?" in path:
            raw_path, query_string = path.split("?", 1)
        else:
            raw_path, query_string = path, ""
        scope = {
            "type": "http",
            "http_version": "1.1",
            "method": method,
            "scheme": "http",
            "path": raw_path,
            "raw_path": raw_path.encode(),
            "query_string": query_string.encode(),
            "headers": headers,
            "client": ("testclient", 50000),
            "server": ("testserver", 80),
        }
        chunks: list[bytes] = []
        response: dict = {}

        async def receive():
            return {"type": "http.request", "body": body, "more_body": False}

        async def send(message):
            if message["type"] == "http.response.start":
                response["status"] = message["status"]
            elif message["type"] == "http.response.body":
                chunks.append(message.get("body", b""))

        loop = asyncio.new_event_loop()
        try:
            loop.run_until_complete(self.app(scope, receive, send))
        finally:
            loop.close()
        return response["status"], json.loads(b"".join(chunks) or b"{}")


def _seed_initial_world(batch: str) -> None:
    from app.services.base import BaseModuleService
    from app.services.coord_transform import CoordTransformService
    from app.services.segment_recalc import SegmentRecalcService

    def point(pid, terminal, ts, lat, lon, rule="v1.0"):
        return {"point_id": pid, "terminal": terminal, "timestamp": ts,
                "lat": lat, "lon": lon, "accuracy": 5.0, "bearing": 0.0,
                "speed": 10.0, "rule_version": rule}

    points = [
        point(1, "T-A", 1000.0, 39.900000, 116.400000),
        point(2, "T-A", 1010.0, 39.900010, 116.400010),
        point(3, "T-A", 1020.0, 39.950000, 116.450000),
        point(4, "T-A", 1030.0, 39.950010, 116.450010),
        point(5, "T-B", 1000.0, 31.200000, 121.500000),
        point(6, "T-B", 1010.0, 31.200010, 121.500010),
        point(7, "T-B", 1020.0, 31.200020, 121.500020),
        point(8, "T-B", 1030.0, 31.200030, 121.500030),
    ]
    svc = BaseModuleService()
    svc._write_jsonl(svc._data_dir("geo_points") / f"{batch}.jsonl", points)
    coord = CoordTransformService()
    engine = SegmentRecalcService()
    coord.transform_batch(batch)
    report = engine.recalculate(batch)
    engine.confirm(batch, report["segment_version"])


def _corrected(batch: str) -> list[dict]:
    from app.services.coord_transform import CoordTransformService
    coord = CoordTransformService()
    points = [
        {"point_id": 1, "terminal": "T-A", "timestamp": 1000.0,
         "lat": 39.900000, "lon": 116.400000, "rule_version": "v1.1"},
        {"point_id": 2, "terminal": "T-A", "timestamp": 1010.0,
         "lat": 39.900010, "lon": 116.400010, "rule_version": "v1.1"},
        {"point_id": 3, "terminal": "T-A", "timestamp": 1020.0,
         "lat": 39.900020, "lon": 116.400020, "rule_version": "v1.1"},
        {"point_id": 4, "terminal": "T-A", "timestamp": 1030.0,
         "lat": 39.900030, "lon": 116.400030, "rule_version": "v1.1"},
        {"point_id": 5, "terminal": "T-B", "timestamp": 1000.0,
         "lat": 31.200000, "lon": 121.500000, "rule_version": "v1.0"},
        {"point_id": 6, "terminal": "T-B", "timestamp": 1010.0,
         "lat": 31.200010, "lon": 121.500010, "rule_version": "v1.0"},
        {"point_id": 7, "terminal": "T-B", "timestamp": 1020.0,
         "lat": 31.200020, "lon": 121.500020, "rule_version": "v1.0"},
        {"point_id": 8, "terminal": "T-B", "timestamp": 1030.0,
         "lat": 31.200030, "lon": 121.500030, "rule_version": "v1.0"},
    ]
    result = coord.write_corrected_points(batch, points)
    return result


def test_route_inspection_full_flow_over_http(setup_data_dir):
    from app.main import app

    http = AsgiCaller(app)
    batch = "API-BATCH-01"
    _seed_initial_world(batch)

    # 打开巡检申请
    status, body = http.request("POST", "/api/route-inspection/applications", {
        "batch_key": batch, "terminals": ["T-A", "T-B"], "remark": "HTTP 全流程"})
    assert status == 200 and body["ok"] is True
    app_id = body["entry"]["id"]
    first_version = body["entry"]["baseline_at_open"]["segment_version"]

    # 首版交付锁定基准
    status, body = http.request("POST",
                                f"/api/route-inspection/applications/{app_id}/deliver")
    assert status == 200 and body["first_delivery_version"] == first_version

    # 纠偏点入库 + 试算重算
    _corrected(batch)
    status, trial = http.request("POST",
                                 f"/api/route-inspection/applications/{app_id}/recalculate",
                                 {"interval_sec": 300, "distance_threshold": 500})
    assert status == 200
    seg_version = trial["segment_version"]
    assert trial["terminals"]["T-A"]["change_type"] == "merged"
    assert trial["terminals"]["T-B"]["mileage_delta"] == 0.0

    # 选择版本并由切分端确认
    status, choice = http.request("POST",
                                  f"/api/route-inspection/applications/{app_id}/choose",
                                  {"segment_version": seg_version})
    assert status == 200 and choice["segment_version"] == seg_version
    assert len(choice["superseded_segments"]) == 2

    # 对账：首版交付版本不变，T-A 有差额与撤销引用，T-B 差额为 0
    status, recon = http.request(
        "GET", f"/api/route-inspection/applications/{app_id}/reconciliation")
    assert status == 200
    assert recon["first_delivery_version"] == first_version
    terms = {t["terminal"]: t for t in recon["terminals"]}
    assert terms["T-A"]["mileage_delta"] != 0.0 and terms["T-A"]["superseded_refs"]
    assert terms["T-B"]["mileage_delta"] == 0.0 and terms["T-B"]["reused_segments"]

    # 申请详情可查完整输入
    status, detail = http.request(
        "GET", f"/api/route-inspection/applications/{app_id}")
    assert status == 200
    assert detail["delivered_baseline"]["segment_version"] == first_version
    assert detail["trials"][0]["report"]["segment_version"] == seg_version
    assert set(detail["待复验分段"].split(",")) == set(choice["superseded_segments"])

    # 未知申请返回 404，通用资源路由不受专用路由影响
    status, body = http.request(
        "GET", "/api/route-inspection/applications/999999/reconciliation")
    assert status == 404
    status, body = http.request("GET", "/api/track_segment?page=1&size=5")
    assert status == 200 and body["total"] >= 3
