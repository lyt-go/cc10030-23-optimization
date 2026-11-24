"""坐标修订后的切分重算、分段沿用与路径巡检对账测试。

场景（确定性构造的定位点，不依赖随机采样）：

- 两台终端 T-A / T-B 同批试算；
- 初版切分：T-A 两段（远距离跳变导致断点），T-B 一段；
- 坐标标准化更正三点：仅修正 T-A 的中间点，两段合并成一段；T-B 完全不动；
- 巡检申请在纠偏前打开，保留打开时通过核查的旧分段；
- 纠偏后重算并由切分端独立确认新合并结果；
- 对账：T-A 有里程差且引用被撤销原分段，T-B 差额为 0、原分段沿用，
  跨终端分段不得挂接；已交付首段按原记录查，不被后续版本选择改写。
"""
from __future__ import annotations

import shutil
import tempfile
from pathlib import Path

import pytest


@pytest.fixture(autouse=True)
def setup_data_dir():
    tmpdir = Path(tempfile.mkdtemp(prefix="geo_recalc_"))
    import app.services.base as base_mod
    original_root = base_mod.DATA_ROOT
    base_mod.DATA_ROOT = tmpdir
    for sub in ["geo_points", "coord_transformed", "track_segments", "checkpoints"]:
        (tmpdir / sub).mkdir(parents=True, exist_ok=True)
    yield tmpdir
    base_mod.DATA_ROOT = original_root
    shutil.rmtree(tmpdir, ignore_errors=True)


def _point(pid: int, terminal: str, ts: float, lat: float, lon: float,
           rule_version: str = "v1.0") -> dict:
    return {
        "point_id": pid,
        "terminal": terminal,
        "timestamp": ts,
        "lat": lat,
        "lon": lon,
        "accuracy": 5.0,
        "bearing": 0.0,
        "speed": 10.0,
        "rule_version": rule_version,
    }


def _write_geo_points(batch_key: str, points: list[dict]) -> None:
    """直接落采集点文件，绕开随机采样，保证切分结果可预测。"""
    from app.services.base import BaseModuleService
    svc = BaseModuleService()
    svc._write_jsonl(svc._data_dir("geo_points") / f"{batch_key}.jsonl", points)


def _initial_points() -> list[dict]:
    # T-A: 1,2 近距离成段；2->3 远距离跳变断开；3,4 近距离成第二段
    return [
        _point(1, "T-A", 1000.0, 39.900000, 116.400000),
        _point(2, "T-A", 1010.0, 39.900010, 116.400010),
        _point(3, "T-A", 1020.0, 39.950000, 116.450000),
        _point(4, "T-A", 1030.0, 39.950010, 116.450010),
        # T-B: 四点连续近距离，始终只有一段
        _point(5, "T-B", 1000.0, 31.200000, 121.500000),
        _point(6, "T-B", 1010.0, 31.200010, 121.500010),
        _point(7, "T-B", 1020.0, 31.200020, 121.500020),
        _point(8, "T-B", 1030.0, 31.200030, 121.500030),
    ]


def _corrected_points() -> list[dict]:
    """坐标标准化更正三点：T-A 的 2/3/4 纠偏后四点连续，两段合一段；T-B 原样。"""
    return [
        _point(1, "T-A", 1000.0, 39.900000, 116.400000, rule_version="v1.1"),
        _point(2, "T-A", 1010.0, 39.900010, 116.400010, rule_version="v1.1"),
        _point(3, "T-A", 1020.0, 39.900020, 116.400020, rule_version="v1.1"),
        _point(4, "T-A", 1030.0, 39.900030, 116.400030, rule_version="v1.1"),
        _point(5, "T-B", 1000.0, 31.200000, 121.500000, rule_version="v1.0"),
        _point(6, "T-B", 1010.0, 31.200010, 121.500010, rule_version="v1.0"),
        _point(7, "T-B", 1020.0, 31.200020, 121.500020, rule_version="v1.0"),
        _point(8, "T-B", 1030.0, 31.200030, 121.500030, rule_version="v1.0"),
    ]


class TestSegmentRecalc:
    """切分重算引擎：版本化坐标、沿用识别、合并差额与撤销引用。"""

    def test_initial_segmentation_is_versioned_with_coord_sources(self, setup_data_dir):
        from app.services.coord_transform import CoordTransformService
        from app.services.segment_recalc import SegmentRecalcService

        batch = "RC-BATCH-01"
        _write_geo_points(batch, _initial_points())
        coord = CoordTransformService()
        engine = SegmentRecalcService()

        first = coord.transform_batch(batch)
        assert first["point_version"] == "pt-v1"
        assert engine.latest_point_version(batch) == "pt-v1"

        report = engine.recalculate(batch, interval_sec=300, distance_threshold=500)
        engine.confirm(batch, report["segment_version"])
        segments, version = engine.get_current_segments(batch)

        assert version == report["segment_version"]
        by_terminal = {}
        for seg in segments:
            by_terminal.setdefault(seg["terminal"], []).append(seg)
        assert len(by_terminal["T-A"]) == 2
        assert len(by_terminal["T-B"]) == 1
        # 每个切分都能查坐标来源，且带着坐标版本/规则版本
        for seg in segments:
            sources = engine.get_segment_coordinate_sources(batch, seg["segment_code"])
            assert len(sources["coord_source"]) == seg["point_count"]
            assert all(src["point_version"] == "pt-v1" for src in sources["coord_source"])
            assert {src["point_id"] for src in sources["coord_source"]} == set(seg["point_ids"])

    def test_recalc_merge_two_segments_keeps_other_terminal_unchanged(self, setup_data_dir):
        from app.services.coord_transform import CoordTransformService
        from app.services.segment_recalc import SegmentRecalcService

        batch = "RC-BATCH-02"
        _write_geo_points(batch, _initial_points())
        coord = CoordTransformService()
        engine = SegmentRecalcService()
        coord.transform_batch(batch)
        first_report = engine.recalculate(batch)
        engine.confirm(batch, first_report["segment_version"])

        before, _ = engine.get_current_segments(batch)
        ta_before = sorted(s["segment_code"] for s in before if s["terminal"] == "T-A")
        tb_before = [s for s in before if s["terminal"] == "T-B"]
        assert len(ta_before) == 2
        registry = engine.load_registry(batch)
        ta_mileage_before = round(sum(
            float(registry["segments"][c]["mileage"]) for c in ta_before), 2)
        tb_mileage_before = round(sum(float(s["mileage"]) for s in tb_before), 2)

        # 纠偏三点 → 新坐标版本，旧版本保留
        correction = coord.write_corrected_points(batch, _corrected_points())
        assert correction["point_version"] == "pt-v2"
        assert engine.latest_point_version(batch) == "pt-v2"
        assert engine.points_version_path(batch, "pt-v1").exists()

        trial = engine.recalculate(batch, trial_id="trial-x")
        assert trial["status"] == "trial"
        term_a = trial["terminals"]["T-A"]
        term_b = trial["terminals"]["T-B"]

        # T-A：两段合并为一段，给出里程差和被撤销原分段引用
        assert term_a["change_type"] == "merged"
        merged = [s for s in term_a["new_segments"] if s["change_type"] == "merged"]
        assert len(merged) == 1
        assert sorted(merged[0]["supersedes"]) == ta_before
        assert set(merged[0]["point_ids"]) == {"1", "2", "3", "4"}
        assert term_a["superseded_segments"] == ta_before
        assert term_a["mileage_before"] == ta_mileage_before
        assert term_a["mileage_delta"] == round(
            term_a["mileage_after"] - term_a["mileage_before"], 2)

        # T-B：未受影响，旧分段沿用且差额为 0
        assert term_b["change_type"] == "unchanged"
        assert term_b["mileage_delta"] == 0.0
        assert term_b["reused_segments"] == [tb_before[0]["segment_code"]]
        assert term_b["superseded_segments"] == []

        # 试算不改变已确认数据
        mid_segments, mid_version = engine.get_current_segments(batch)
        assert mid_version == first_report["segment_version"]
        assert len(mid_segments) == 3

        confirmation = engine.confirm(batch, trial["segment_version"])
        assert sorted(confirmation["superseded_segments"]) == ta_before
        assert confirmation["reused_segments"] == [tb_before[0]["segment_code"]]

        after, after_version = engine.get_current_segments(batch)
        assert after_version == trial["segment_version"]
        ta_after = [s for s in after if s["terminal"] == "T-A"]
        tb_after = [s for s in after if s["terminal"] == "T-B"]
        assert len(ta_after) == 1 and ta_after[0]["segment_code"] not in ta_before
        assert len(tb_after) == 1 and tb_after[0]["segment_code"] == tb_before[0]["segment_code"]

        # 撤销链完整：旧分段状态、替换者、引用来源均可回溯
        registry = engine.load_registry(batch)
        for code in ta_before:
            item = registry["segments"][code]
            assert item["status"] == "superseded"
            assert item["superseded_by"] == ta_after[0]["segment_code"]
            assert item["supersede_reason"] == "merged"
        assert registry["segments"][tb_before[0]["segment_code"]]["status"] == "confirmed"
        assert abs(term_b["mileage_after"] - tb_mileage_before) < 0.01

    def test_other_terminal_segment_is_never_attached_to_new_points(self, setup_data_dir):
        """跨终端保护：T-A 的新点不能挂接 T-B 的既有分段。"""
        from app.services.coord_transform import CoordTransformService
        from app.services.segment_recalc import SegmentRecalcService

        batch = "RC-BATCH-03"
        _write_geo_points(batch, _initial_points())
        coord = CoordTransformService()
        engine = SegmentRecalcService()
        coord.transform_batch(batch)
        first = engine.recalculate(batch)
        engine.confirm(batch, first["segment_version"])
        before, _ = engine.get_current_segments(batch)
        tb_code = next(s["segment_code"] for s in before if s["terminal"] == "T-B")

        coord.write_corrected_points(batch, _corrected_points())
        trial = engine.recalculate(batch)
        for term, bucket in trial["terminals"].items():
            for seg in bucket["new_segments"]:
                if term == "T-A":
                    assert tb_code not in seg["supersedes"]
                    assert seg["reused_segment_code"] != tb_code
        # 新合并段的坐标来源全部属于 T-A
        ta_merged = trial["terminals"]["T-A"]["new_segments"][0]
        sources = engine.get_segment_coordinate_sources(
            batch, ta_merged["segment_code"], trial["segment_version"])
        orig_points = {str(p["point_id"]): p for p in _corrected_points()}
        for src in sources["coord_source"]:
            assert orig_points[src["point_id"]]["terminal"] == "T-A"

    def test_reuse_requires_same_terminal_same_points_same_mileage(self, setup_data_dir):
        """沿用判据：同终端 + 同点集 + 里程一致才沿用，点集漂移则视为变更。"""
        from app.services.coord_transform import CoordTransformService
        from app.services.segment_recalc import SegmentRecalcService

        batch = "RC-BATCH-04"
        _write_geo_points(batch, _initial_points())
        coord = CoordTransformService()
        engine = SegmentRecalcService()
        coord.transform_batch(batch)
        first = engine.recalculate(batch)
        engine.confirm(batch, first["segment_version"])

        # 仅轻微挪动 T-B 一个点（仍同点集），里程变化超过容差 → changed 而非 reused
        tweaked = _corrected_points()
        for p in tweaked:
            if p["point_id"] == 7:
                p["lat"] = 31.201000
                p["rule_version"] = "v1.2"
        coord.write_corrected_points(batch, tweaked)
        trial = engine.recalculate(batch)
        tb = trial["terminals"]["T-B"]
        assert tb["change_type"] != "unchanged"
        assert not tb["new_segments"][0]["reused"]
        assert tb["new_segments"][0]["change_type"] == "changed"


class TestRouteInspection:
    """路径巡检：打开留痕、交付基准不可变、重算选择与对账。"""

    def _prepare(self) -> tuple[str, object, object, object]:
        from app.services.coord_transform import CoordTransformService
        from app.services.segment_recalc import SegmentRecalcService
        from app.services.route_inspection import RouteInspectionService

        batch = "INS-BATCH-01"
        _write_geo_points(batch, _initial_points())
        coord = CoordTransformService()
        engine = SegmentRecalcService()
        inspection = RouteInspectionService()
        coord.transform_batch(batch)
        first = engine.recalculate(batch)
        engine.confirm(batch, first["segment_version"])
        return batch, coord, engine, inspection

    def test_open_application_retains_old_segments_through_recalc(self, setup_data_dir):
        batch, coord, engine, inspection = self._prepare()
        before, first_version = engine.get_current_segments(batch)
        old_ta = sorted(s["segment_code"] for s in before if s["terminal"] == "T-A")
        old_tb = next(s["segment_code"] for s in before if s["terminal"] == "T-B")

        app = inspection.open_application(batch, ["T-A", "T-B"], remark="纠偏前打开")
        app_id = app["id"]
        # 打开快照保留旧分段引用
        snap = app["baseline_at_open"]
        assert snap["segment_version"] == first_version
        assert sorted(snap["terminals"]["T-A"], key=lambda x: x["segment_code"]) and \
            {s["segment_code"] for s in snap["terminals"]["T-A"]} == set(old_ta)
        assert [s["segment_code"] for s in snap["terminals"]["T-B"]] == [old_tb]

        # 同批试算期间 T-A 收到新纠偏点，T-B 不动
        coord.write_corrected_points(batch, _corrected_points())
        trial = inspection.recalculate(app_id)
        # 未确认前申请仍保留打开时的旧分段
        detail = inspection.get_application_detail(app_id)
        assert {s["segment_code"]
                for s in detail["baseline_at_open"]["terminals"]["T-A"]} == set(old_ta)

        # 切分端独立确认新的合并结果
        choice = inspection.choose_version(app_id, trial["segment_version"])
        assert choice["new_segments"] and set(choice["superseded_segments"]) == set(old_ta)
        assert old_tb in choice["reused_segments"]

        # 下次仍需复验的旧分段 = 打开时通过核查、现已被撤销的 T-A 两段
        detail = inspection.get_application_detail(app_id)
        assert set(detail["待复验分段"].split(",")) == set(old_ta)

    def test_delivered_first_segment_is_immutable_baseline(self, setup_data_dir):
        batch, coord, engine, inspection = self._prepare()
        before, first_version = engine.get_current_segments(batch)
        old_ta = sorted(s["segment_code"] for s in before if s["terminal"] == "T-A")
        first_ta_segment = next(
            s for s in before if s["terminal"] == "T-A" and s["segment_code"] == old_ta[0])

        app = inspection.open_application(batch, ["T-A", "T-B"])
        app_id = app["id"]
        delivery = inspection.deliver(app_id)
        assert delivery["first_delivery_version"] == first_version

        # 已交接的首段按原记录查：在首版交付的航段版本里仍能查到原分段坐标来源
        original_sources = inspection.get_segment_coordinate_sources(
            app_id, first_ta_segment["segment_code"], first_version)
        assert len(original_sources["coord_source"]) == first_ta_segment["point_count"]

        # 纠偏、重算、再选择
        coord.write_corrected_points(batch, _corrected_points())
        trial = inspection.recalculate(app_id)
        inspection.choose_version(app_id, trial["segment_version"])
        # 再次交付只追加历史，首版交付基准不变
        second_delivery = inspection.deliver(app_id)
        detail = inspection.get_application_detail(app_id)
        assert detail["delivered_baseline"]["segment_version"] == first_version
        assert len(detail["delivery_history"]) == 2
        assert detail["delivery_history"][1]["segment_version"] == \
            second_delivery["delivery"]["segment_version"]

        # 首段在当前版本里已不存在，但原记录仍可按原版本查到，未被选择改写
        with pytest.raises(FileNotFoundError):
            inspection.get_segment_coordinate_sources(
                app_id, first_ta_segment["segment_code"])
        again = inspection.get_segment_coordinate_sources(
            app_id, first_ta_segment["segment_code"], first_version)
        assert again["coord_source"] == original_sources["coord_source"]

    def test_reconciliation_uses_delivered_baseline_and_zero_delta_for_unaffected(
            self, setup_data_dir):
        batch, coord, engine, inspection = self._prepare()
        before, first_version = engine.get_current_segments(batch)
        old_ta = sorted(s["segment_code"] for s in before if s["terminal"] == "T-A")
        old_tb = next(s["segment_code"] for s in before if s["terminal"] == "T-B")
        old_tb_mileage = next(
            float(s["mileage"]) for s in before if s["segment_code"] == old_tb)

        app = inspection.open_application(batch, ["T-A", "T-B"])
        app_id = app["id"]
        inspection.deliver(app_id)
        coord.write_corrected_points(batch, _corrected_points())
        trial = inspection.recalculate(app_id)
        inspection.choose_version(app_id, trial["segment_version"])

        recon = inspection.get_reconciliation(app_id)
        assert recon["baseline_locked"] is True
        assert recon["first_delivery_version"] == first_version
        terms = {t["terminal"]: t for t in recon["terminals"]}

        ta = terms["T-A"]
        assert ta["affected"] is True
        assert ta["superseded_segments"] == old_ta
        assert ta["superseded_refs"] and all(
            ref["supersede_reason"] == "merged" and ref["superseded_by"]
            for ref in ta["superseded_refs"])
        assert ta["mileage_delta"] == round(ta["mileage_after"] - ta["mileage_before"], 2)
        assert ta["mileage_delta"] != 0.0

        tb = terms["T-B"]
        assert tb["affected"] is False
        assert tb["mileage_delta"] == 0.0
        assert tb["reused_segments"] == [old_tb]
        assert abs(tb["mileage_after"] - old_tb_mileage) < 0.01
        assert tb["superseded_segments"] == []
        # 当前 T-B 分段仍挂在原分段编号上，没有被 T-A 的新段顶替
        assert tb["current_segments"][0]["segment_code"] == old_tb

    def test_full_inputs_are_verifiable_from_application_detail(self, setup_data_dir):
        """两边完整输入可核验：坐标版本、航段版本、试算报告、坐标来源都在申请详情链路内。"""
        batch, coord, engine, inspection = self._prepare()
        app = inspection.open_application(batch, ["T-A", "T-B"])
        app_id = app["id"]

        coord.write_corrected_points(batch, _corrected_points())
        trial = inspection.recalculate(app_id)
        merged_code = trial["terminals"]["T-A"]["new_segments"][0]["segment_code"]

        detail = inspection.get_application_detail(app_id)
        assert detail["trials"][0]["report"]["point_version"] == "pt-v2"
        assert detail["trials"][0]["segment_version"] == trial["segment_version"]
        sources = inspection.get_segment_coordinate_sources(
            app_id, merged_code, trial["segment_version"])
        assert {src["point_version"] for src in sources["coord_source"]} == {"pt-v2"}
        assert {src["rule_version"] for src in sources["coord_source"]} == {"v1.1"}
        # 坐标版本清单可枚举，旧版本仍在
        versions = coord.list_point_versions(batch)
        assert [v["point_version"] for v in versions] == ["pt-v1", "pt-v2"]
        assert [v["is_latest"] for v in versions] == [False, True]
