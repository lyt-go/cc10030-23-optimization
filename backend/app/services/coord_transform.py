"""坐标标准化领域服务：维护坐标系转换规则、偏移参数与越界处理，并对采集批次执行坐标归一化。

转换结果按坐标版本落盘（{batch}_points_{version}.jsonl），并维护当前版本指针：
坐标标准化更正（纠偏点）以新版本写入，旧版本保留可回溯，切分重算按实际版本取数。
"""
from __future__ import annotations

import re
from typing import Any

from app.services.base import BaseModuleService
from app.services.segment_recalc import SegmentRecalcService
from app.store import store


class CoordTransformService(BaseModuleService):
    MODULE = "coord_transform"
    ENTITY = "转换规则"
    OPERATION_TERM = "转换"
    LIST_FIELDS = ["规则编号", "源坐标系", "目标坐标系", "偏移参数", "精度位数", "越界处理", "规则版本", "生效批次"]
    REQUIRED_FIELDS = ["规则编号", "源坐标系", "目标坐标系"]
    STATUS_ORDER = ["草稿", "待校验", "已生效", "已废弃"]
    ACTION_RULES = {"提交校验": "待校验", "确认生效": "已生效", "标记废弃": "已废弃"}
    NEGATIVE_ACTIONS = []

    def __init__(self) -> None:
        self.engine = SegmentRecalcService()

    def _next_point_version(self, batch_key: str) -> str:
        """按现有版本文件推导下一个坐标版本号 pt-vN。"""
        latest = self.engine.latest_point_version(batch_key)
        if latest:
            match = re.fullmatch(r"pt-v(\d+)", latest)
            seq = int(match.group(1)) + 1 if match else 1
            return f"pt-v{seq}"
        pattern = re.compile(rf"^{re.escape(batch_key)}_points_(pt-v\d+)\.jsonl$")
        used = []
        for path in self.engine._points_dir().glob(f"{batch_key}_points_pt-v*.jsonl"):
            match = pattern.match(path.name)
            if match:
                used.append(int(match.group(1).replace("pt-v", "")))
        return f"pt-v{max(used, default=0) + 1}"

    def list_point_versions(self, batch_key: str) -> list[dict[str, Any]]:
        """列出批次全部坐标版本及其文件、点数，标注当前版本。"""
        pattern = re.compile(rf"^{re.escape(batch_key)}_points_(pt-v\d+)\.jsonl$")
        latest = self.engine.latest_point_version(batch_key)
        versions = []
        for path in sorted(self.engine._points_dir().glob(f"{batch_key}_points_pt-v*.jsonl")):
            match = pattern.match(path.name)
            if not match:
                continue
            version = match.group(1)
            points = self._read_jsonl(path)
            versions.append({
                "point_version": version,
                "is_latest": version == latest,
                "point_count": len(points),
                "rule_versions": sorted({
                    str(p.get("rule_version")) for p in points if p.get("rule_version") is not None
                }),
                "file": str(path),
            })
        return versions

    def transform_batch(self, batch_key: str, rule_id: int = 1,
                        point_version: str | None = None) -> dict[str, Any]:
        """读取采集批次定位点文件，解析原始坐标并执行坐标系转换，生成带版本的转换后数据集。"""
        points_file = self._data_dir("geo_points") / f"{batch_key}.jsonl"
        if not points_file.exists():
            raise FileNotFoundError(f"批次 {batch_key} 的定位点数据文件不存在，请先接入采集批次")
        points = self._read_jsonl(points_file)

        # 获取转换规则
        rule = store.find(self.MODULE, rule_id)
        offset_lat = 0.0
        offset_lon = 0.0
        precision = 6
        if rule:
            try:
                offset_params = rule.get("偏移参数", "0.0,0.0")
                parts = offset_params.split(",")
                offset_lat = float(parts[0]) if len(parts) > 0 else 0.0
                offset_lon = float(parts[1]) if len(parts) > 1 else 0.0
                precision = int(rule.get("精度位数", "6"))
            except (ValueError, IndexError):
                pass

        transformed = []
        for p in points:
            new_lat = round(p["lat"] + offset_lat, precision)
            new_lon = round(p["lon"] + offset_lon, precision)
            # 越界处理
            if new_lat > 90 or new_lat < -90:
                new_lat = max(-90, min(90, new_lat))
            if new_lon > 180 or new_lon < -180:
                new_lon = max(-180, min(180, new_lon))
            transformed.append({
                "point_id": p["point_id"],
                "batch_key": batch_key,
                "orig_lat": p["lat"],
                "orig_lon": p["lon"],
                "trans_lat": new_lat,
                "trans_lon": new_lon,
                "timestamp": p.get("timestamp"),
                "terminal": p.get("terminal"),
                "accuracy": p.get("accuracy"),
                "bearing": p.get("bearing"),
                "speed": p.get("speed"),
                "rule_version": rule.get("规则版本", "v1.0") if rule else "v1.0",
            })

        resolved_version = point_version or self._next_point_version(batch_key)
        write_result = self.engine.write_points_version(
            batch_key, resolved_version, transformed, make_latest=True)

        # 更新记录条数
        if rule:
            rule["生效批次"] = batch_key

        return {
            "batch_key": batch_key,
            "input_points": len(points),
            "transformed_points": write_result["points_written"],
            "rule_id": rule_id,
            "point_version": resolved_version,
            "output_file": write_result["points_file"],
        }

    def write_corrected_points(
        self,
        batch_key: str,
        corrected_points: list[dict[str, Any]],
        *,
        point_version: str | None = None,
        make_latest: bool = True,
        rule_version: str | None = None,
    ) -> dict[str, Any]:
        """坐标标准化更正：把纠偏后的定位点写成新坐标版本，不覆盖旧版本。"""
        normalized = []
        resolved_version = point_version or self._next_point_version(batch_key)
        for p in corrected_points:
            row = dict(p)
            row.setdefault("batch_key", batch_key)
            row.setdefault("point_version", resolved_version)
            # 入参可以是采集点形状（lat/lon），也可以已是转换点形状（trans_lat/trans_lon）
            if "trans_lat" not in row:
                if "lat" not in row or "lon" not in row:
                    raise ValueError("纠偏点必须包含 lat/lon 或 trans_lat/trans_lon 字段")
                row["orig_lat"] = row["lat"]
                row["orig_lon"] = row["lon"]
                row["trans_lat"] = row.pop("lat")
                row["trans_lon"] = row.pop("lon")
            if rule_version is not None:
                row["rule_version"] = rule_version
            normalized.append(row)
        result = self.engine.write_points_version(
            batch_key, resolved_version, normalized, make_latest=make_latest)
        result["corrected_points"] = result.pop("points_written")
        return result

    def get_transformed_points(self, batch_key: str,
                                point_version: str | None = None) -> tuple[list[dict[str, Any]], str]:
        """读取指定坐标版本（默认当前版本）的转换后定位点记录和文件路径。"""
        points = self.engine.read_points(batch_key, point_version)
        if point_version is None:
            point_version = self.engine.latest_point_version(batch_key) or "pt-v1"
        path = self.engine.points_version_path(batch_key, point_version)
        if not path.exists():
            path = self.engine.legacy_points_path(batch_key)
        return points, str(path)

    def apply_rule_to_all(self, rule_id: int) -> dict[str, Any]:
        """对已接入的全部采集批次应用指定转换规则，批量生成坐标标准化数据集。"""
        rule = store.find(self.MODULE, rule_id)
        if rule is None:
            return {"error": f"转换规则 {rule_id} 不存在"}
        geo_svc = self._safe_import("app.services.geo_source", "GeoSourceService")()
        active_batches = geo_svc.list_active_batches()
        results = {}
        for batch in active_batches:
            batch_key = batch["批次编号"]
            try:
                results[batch_key] = self.transform_batch(batch_key, rule_id)
            except Exception as e:
                results[batch_key] = {"error": str(e)}
        return {"rule_id": rule_id, "batches_processed": len(results), "results": results}
