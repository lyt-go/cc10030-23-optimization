"""轨迹切分领域服务：按时间间隔与距离阈值将定位点序列切分为轨迹分段数据集。

坐标修订后的重算、分段沿用与撤销引用统一委托给 ``SegmentRecalcService``：
本服务保留对外既有的批次切分/读取接口，切分结果按航段版本落盘并登记台账。
"""
from __future__ import annotations

from typing import Any

from app.services.base import BaseModuleService
from app.services.segment_recalc import SegmentRecalcService


class TrackSegmentService(BaseModuleService):
    MODULE = "track_segment"
    ENTITY = "轨迹分段"
    OPERATION_TERM = "切分"
    LIST_FIELDS = ["分段编号", "终端编号", "起始时间", "结束时间", "点数", "间隔阈值", "里程", "切分策略"]
    REQUIRED_FIELDS = ["分段编号", "终端编号", "起始时间"]
    STATUS_ORDER = ["草稿", "待切分", "已切分", "切分异常"]
    ACTION_RULES = {"启动切分": "待切分", "确认切分": "已切分", "标记异常": "切分异常"}
    NEGATIVE_ACTIONS = []

    def __init__(self) -> None:
        # 组合而非继承：切分重算引擎独立演进，本服务只做门面。
        self.engine = SegmentRecalcService()

    def segment_from_transformed(self, batch_key: str, interval_sec: float = 300.0,
                                  distance_threshold: float = 500.0) -> dict[str, Any]:
        """读取坐标转换后的定位点（实际坐标版本），切分并立即确认为当前航段版本。"""
        try:
            points = self.engine.read_points(batch_key)
        except FileNotFoundError:
            raise FileNotFoundError(f"批次 {batch_key} 的转换数据不存在，请先执行坐标标准化")

        if not points:
            return {"batch_key": batch_key, "segments": 0, "total_points": 0}

        report = self.engine.recalculate(
            batch_key,
            interval_sec=interval_sec,
            distance_threshold=distance_threshold,
            triggered_by="segment_from_transformed",
        )
        confirmation = self.engine.confirm(batch_key, report["segment_version"])
        total_mileage = round(
            sum(bucket["mileage_after"] for bucket in report["terminals"].values()), 2)
        return {
            "batch_key": batch_key,
            "total_points": report["total_points"],
            "segments": report["segments"],
            "total_mileage": total_mileage,
            "segment_version": report["segment_version"],
            "point_version": report["point_version"],
            "reused_segments": confirmation["reused_segments"],
            "superseded_segments": confirmation["superseded_segments"],
            "output_file": self.engine.segments_version_path(
                batch_key, report["segment_version"]).__str__(),
        }

    def get_segments(self, batch_key: str) -> tuple[list[dict[str, Any]], str]:
        """读取批次当前已确认航段版本的轨迹分段数据集，返回分段记录和版本/文件路径。"""
        segments, version = self.engine.get_current_segments(batch_key)
        if version == "legacy":
            return segments, str(self.engine.legacy_segments_path(batch_key))
        return segments, str(self.engine.segments_version_path(batch_key, version))
