"""轨迹切分重算与分段沿用核心域。

坐标标准化修订后，本模块负责：

1. 读取实际坐标版本（带版本号的转换后定位点），按时间/距离阈值重新切分；
2. 为每个新切分记录完整的坐标来源（point_id、坐标版本、规则版本、经纬度）；
3. 以「同终端 + 同点集 + 同里程」为沿用判据，识别可沿用的已确认分段，
   绝不把另一终端的既有分段挂到新点上；
4. 两段合并为一段时给出受影响终端的里程差额和被撤销的原分段引用，
   未受影响终端差额为 0；
5. 试算结果只落版本文件与登记表的 trial 区，确认后才晋升为当前切分版本，
   原分段进入 superseded（已撤销）状态并保留引用链。

文件布局（DATA_ROOT 下）：

- coord_transformed/{batch}_points_{point_version}.jsonl     版本化定位点
- coord_transformed/{batch}_points_latest.json               当前坐标版本指针
- coord_transformed/{batch}_transformed.jsonl                兼容旧消费方的当前版本副本
- track_segments/{batch}_segments_{segment_version}.jsonl    版本化切分结果
- track_segments/{batch}_segments_latest.json                当前切分版本指针
- track_segments/{batch}_segments.jsonl                      兼容旧消费方的当前版本副本
- track_segments/{batch}_registry.json                       分段登记/撤销/试算台账
"""
from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

from app.services.base import BaseModuleService
from app.store import store

MILEAGE_TOLERANCE = 0.01


class SegmentRecalcService(BaseModuleService):
    """切分重算服务：无状态切分引擎 + 每批次一份的分段台账。"""

    MODULE = "track_segment"
    ENTITY = "轨迹分段"
    OPERATION_TERM = "切分"
    # 确认晋升时会向轨迹分段表登记新行，沿用 TrackSegmentService 的字段与状态配置。
    LIST_FIELDS = ["分段编号", "终端编号", "起始时间", "结束时间", "点数", "间隔阈值", "里程", "切分策略"]
    REQUIRED_FIELDS = ["分段编号", "终端编号", "起始时间"]
    STATUS_ORDER = ["草稿", "待切分", "已切分", "切分异常"]
    ACTION_RULES = {"启动切分": "待切分", "确认切分": "已切分", "标记异常": "切分异常"}
    NEGATIVE_ACTIONS = []

    # ------------------------------------------------------------------
    # 路径与文件 IO
    # ------------------------------------------------------------------

    def _points_dir(self) -> Path:
        return self._data_dir("coord_transformed")

    def _segments_dir(self) -> Path:
        return self._data_dir("track_segments")

    def points_version_path(self, batch_key: str, point_version: str) -> Path:
        return self._points_dir() / f"{batch_key}_points_{point_version}.jsonl"

    def points_pointer_path(self, batch_key: str) -> Path:
        return self._points_dir() / f"{batch_key}_points_latest.json"

    def legacy_points_path(self, batch_key: str) -> Path:
        return self._points_dir() / f"{batch_key}_transformed.jsonl"

    def segments_version_path(self, batch_key: str, segment_version: str) -> Path:
        return self._segments_dir() / f"{batch_key}_segments_{segment_version}.jsonl"

    def segments_pointer_path(self, batch_key: str) -> Path:
        return self._segments_dir() / f"{batch_key}_segments_latest.json"

    def legacy_segments_path(self, batch_key: str) -> Path:
        return self._segments_dir() / f"{batch_key}_segments.jsonl"

    def registry_path(self, batch_key: str) -> Path:
        return self._segments_dir() / f"{batch_key}_registry.json"

    @staticmethod
    def _write_jsonl_overwrite(filepath: Path, records: list[dict[str, Any]]) -> int:
        """覆盖写入 JSONL（版本文件不允许追加，避免重算后混入旧记录）。"""
        filepath.parent.mkdir(parents=True, exist_ok=True)
        with open(filepath, "w", encoding="utf-8") as f:
            for rec in records:
                f.write(json.dumps(rec, ensure_ascii=False, default=str) + "\n")
        return len(records)

    def _write_json(self, filepath: Path, payload: dict[str, Any]) -> None:
        filepath.parent.mkdir(parents=True, exist_ok=True)
        with open(filepath, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, indent=2, default=str)

    def _read_json(self, filepath: Path) -> dict[str, Any] | None:
        if not filepath.exists():
            return None
        with open(filepath, "r", encoding="utf-8") as f:
            return json.load(f)

    # ------------------------------------------------------------------
    # 坐标版本读写
    # ------------------------------------------------------------------

    def latest_point_version(self, batch_key: str) -> str | None:
        pointer = self._read_json(self.points_pointer_path(batch_key))
        if pointer and pointer.get("point_version"):
            return str(pointer["point_version"])
        return None

    def write_points_version(
        self,
        batch_key: str,
        point_version: str,
        points: list[dict[str, Any]],
        *,
        make_latest: bool = True,
    ) -> dict[str, Any]:
        """落一份版本化转换点（纠偏点入库走这里），并按需刷新当前版本指针与兼容副本。"""
        versioned = []
        for p in points:
            row = dict(p)
            row.setdefault("batch_key", batch_key)
            row.setdefault("point_version", point_version)
            versioned.append(row)
        target = self.points_version_path(batch_key, point_version)
        written = self._write_jsonl_overwrite(target, versioned)
        if make_latest:
            self._write_json(self.points_pointer_path(batch_key), {
                "batch_key": batch_key,
                "point_version": point_version,
                "updated_at": time.time(),
            })
            # 兼容仍直接读取 {batch}_transformed.jsonl 的下游服务
            self._write_jsonl_overwrite(self.legacy_points_path(batch_key), versioned)
        return {
            "batch_key": batch_key,
            "point_version": point_version,
            "points_written": written,
            "points_file": str(target),
            "latest": make_latest,
        }

    def read_points(self, batch_key: str, point_version: str | None = None) -> list[dict[str, Any]]:
        """读取指定坐标版本的转换点；缺省读当前版本指针，再退回首版兼容文件。"""
        if point_version is None:
            point_version = self.latest_point_version(batch_key)
        if point_version is not None:
            path = self.points_version_path(batch_key, point_version)
            if not path.exists():
                raise FileNotFoundError(
                    f"批次 {batch_key} 的坐标版本 {point_version} 数据不存在")
            return self._read_jsonl(path)
        legacy = self.legacy_points_path(batch_key)
        if not legacy.exists():
            raise FileNotFoundError(f"批次 {batch_key} 的转换数据不存在，请先执行坐标标准化")
        return self._read_jsonl(legacy)

    # ------------------------------------------------------------------
    # 台账
    # ------------------------------------------------------------------

    def load_registry(self, batch_key: str) -> dict[str, Any]:
        registry = self._read_json(self.registry_path(batch_key))
        if registry is not None:
            return registry
        return {
            "batch_key": batch_key,
            "next_seq": 1,
            "next_segment_seq": 1,
            "confirmed_segment_version": None,
            "latest_segment_version": None,
            "segments": {},          # segment_code -> 分段登记项
            "trials": {},            # segment_version -> 试算上下文
            "history": [],
        }

    def _save_registry(self, registry: dict[str, Any]) -> None:
        self._write_json(self.registry_path(registry["batch_key"]), registry)

    def _alloc_segment_code(self, registry: dict[str, Any], batch_key: str) -> str:
        seq = int(registry["next_seq"])
        registry["next_seq"] = seq + 1
        return f"SEG-{batch_key}-{seq:03d}"

    # ------------------------------------------------------------------
    # 切分引擎
    # ------------------------------------------------------------------

    @staticmethod
    def _point_id(point: dict[str, Any], index: int) -> str:
        pid = point.get("point_id")
        return str(pid) if pid is not None else f"idx-{index}"

    def _coord_source(self, point: dict[str, Any], point_version: str) -> dict[str, Any]:
        """每个切分都可回溯单个定位点的坐标来源。point_id 与 point_ids 一样字符串化。"""
        pid = point.get("point_id")
        return {
            "point_id": str(pid) if pid is not None else None,
            "timestamp": point.get("timestamp"),
            "lat": point["trans_lat"],
            "lon": point["trans_lon"],
            "point_version": point.get("point_version") or point_version,
            "rule_version": point.get("rule_version"),
        }

    def _mileage_of(self, points: list[dict[str, Any]]) -> float:
        mileage = 0.0
        for prev, cur in zip(points, points[1:]):
            mileage += self._haversine(
                prev["trans_lat"], prev["trans_lon"],
                cur["trans_lat"], cur["trans_lon"],
            )
        return round(mileage, 2)

    def segment_points(
        self,
        points: list[dict[str, Any]],
        *,
        batch_key: str,
        point_version: str,
        interval_sec: float,
        distance_threshold: float,
    ) -> list[dict[str, Any]]:
        """按终端、时间排序后执行时间间隔 + 距离阈值切分，输出带坐标来源的原始分段。"""
        ordered = sorted(points, key=lambda p: (str(p.get("terminal", "")), p.get("timestamp", 0)))
        results: list[dict[str, Any]] = []
        bucket: list[dict[str, Any]] = []

        def flush(seq: int) -> None:
            # 与原切分行为保持一致：阈值频繁触发时允许输出单点分段（里程为 0）。
            if not bucket:
                return
            start, end = bucket[0], bucket[-1]
            point_ids = [self._point_id(p, i) for i, p in enumerate(bucket)]
            results.append({
                "segment_id": seq,
                "batch_key": batch_key,
                "terminal": start.get("terminal"),
                "start_time": start.get("timestamp"),
                "end_time": end.get("timestamp"),
                "point_count": len(bucket),
                "start_lat": start["trans_lat"],
                "start_lon": start["trans_lon"],
                "end_lat": end["trans_lat"],
                "end_lon": end["trans_lon"],
                "mileage": self._mileage_of(bucket),
                "interval_threshold": interval_sec,
                "distance_threshold": distance_threshold,
                "strategy": "time_distance",
                "point_version": point_version,
                "rule_versions": sorted({
                    str(p.get("rule_version")) for p in bucket if p.get("rule_version") is not None
                }),
                "point_ids": point_ids,
                "coord_source": [self._coord_source(p, point_version) for p in bucket],
            })

        seq = 0
        for point in ordered:
            if not bucket:
                bucket.append(point)
                continue
            last = bucket[-1]
            time_diff = abs(float(point.get("timestamp", 0)) - float(last.get("timestamp", 0)))
            dist = self._haversine(
                last["trans_lat"], last["trans_lon"],
                point["trans_lat"], point["trans_lon"],
            )
            if time_diff > interval_sec or dist > distance_threshold:
                seq += 1
                flush(seq)
                bucket = [point]
            else:
                bucket.append(point)
        if bucket:
            # 与原实现一致：末尾只剩单点时不单独成段。
            if len(bucket) >= 2:
                seq += 1
                flush(seq)
        return results

    # ------------------------------------------------------------------
    # 沿用 / 撤销识别（试算，不改已确认数据）
    # ------------------------------------------------------------------

    @staticmethod
    def _terminal_active(registry: dict[str, Any], terminal: str) -> list[dict[str, Any]]:
        return [
            item for item in registry["segments"].values()
            if item.get("terminal") == terminal and item.get("status") == "confirmed"
        ]

    def _diff_segments(
        self,
        registry: dict[str, Any],
        raw_segments: list[dict[str, Any]],
    ) -> tuple[list[dict[str, Any]], dict[str, dict[str, Any]]]:
        """把新切分与台账中同终端的已确认分段比对，生成沿用/合并/变更判定。

        判据：仅在同一终端内匹配（跨终端分段不得挂接）；点集完全一致且里程一致
        才判定为沿用；否则按点集交叠识别合并/拆分/变更，并记录被撤销原分段。
        """
        planned: list[dict[str, Any]] = []
        # 每个旧分段在本次新切分中被哪些新分段引用，便于识别 1→N 拆分
        old_refs: dict[str, list[str]] = {}

        for raw in raw_segments:
            terminal = raw.get("terminal")
            new_ids = set(raw["point_ids"])
            candidates = self._terminal_active(registry, terminal)
            exact = [c for c in candidates if set(c.get("point_ids", [])) == new_ids]
            reused = next(
                (c for c in exact
                 if abs(float(c["mileage"]) - float(raw["mileage"])) <= MILEAGE_TOLERANCE),
                None,
            )

            plan = dict(raw)
            plan["reused"] = reused is not None
            plan["reused_segment_code"] = reused["segment_code"] if reused else None
            plan["old_mileage"] = float(reused["mileage"]) if reused else None
            plan["mileage_delta"] = 0.0
            plan["supersedes"] = []
            plan["change_type"] = "reused" if reused is not None else "new"

            if reused is not None:
                # 沿用分段直接保留原分段编号：版本文件里它就是那条已确认的旧分段，
                # 不占用新编号，也不会把别的终端分段挂到本终端新点上。
                plan["segment_code"] = reused["segment_code"]

            if reused is None:
                # 只允许在同一终端内匹配原分段，跨终端分段不得挂接
                intersected = [
                    c for c in candidates
                    if new_ids.intersection(set(c.get("point_ids", [])))
                ]
                old_codes = [c["segment_code"] for c in intersected]
                for code in old_codes:
                    old_refs.setdefault(code, []).append(plan["segment_code"])
                plan["supersedes"] = old_codes
                union_ids = set().union(
                    *(set(c.get("point_ids", [])) for c in intersected)) if intersected else set()
                if len(intersected) >= 2 and union_ids == new_ids:
                    plan["change_type"] = "merged"
                elif intersected:
                    plan["change_type"] = "changed"
                before = round(sum(float(c["mileage"]) for c in intersected), 2) if intersected \
                    else 0.0
                plan["old_mileage"] = before if intersected else None
                if intersected:
                    plan["mileage_delta"] = round(float(raw["mileage"]) - before, 2)
                else:
                    plan["mileage_delta"] = round(float(raw["mileage"]), 2)
            planned.append(plan)

        # 一个旧分段被多个新分段引用 → 这些新分段属于拆分
        split_old_codes = {code for code, refs in old_refs.items() if len(set(refs)) >= 2}
        if split_old_codes:
            for plan in planned:
                if plan["change_type"] == "changed" and split_old_codes.intersection(
                        plan["supersedes"]):
                    plan["change_type"] = "split"

        # 终端级汇总（mileage_before 按旧分段只计一次，合并时不重复累加）
        terminals: dict[str, dict[str, Any]] = {}
        for plan in planned:
            term = str(plan.get("terminal"))
            bucket = terminals.setdefault(term, {
                "terminal": term,
                "change_type": "unchanged",
                "mileage_before": 0.0,
                "mileage_after": 0.0,
                "new_segments": [],
                "reused_segments": [],
                "superseded_segments": [],
                "_counted_old": set(),
            })
            bucket["new_segments"].append({
                "segment_code": plan["segment_code"],
                "change_type": plan["change_type"],
                "reused": plan["reused"],
                "reused_segment_code": plan["reused_segment_code"],
                "mileage": plan["mileage"],
                "old_mileage": plan["old_mileage"],
                "mileage_delta": plan["mileage_delta"],
                "supersedes": list(plan["supersedes"]),
                "point_ids": list(plan["point_ids"]),
                "coord_source": list(plan["coord_source"]),
            })
            bucket["mileage_after"] = round(bucket["mileage_after"] + float(plan["mileage"]), 2)
            if plan["reused"]:
                bucket["reused_segments"].append(plan["reused_segment_code"])
                if plan["reused_segment_code"] not in bucket["_counted_old"]:
                    bucket["_counted_old"].add(plan["reused_segment_code"])
                    bucket["mileage_before"] = round(
                        bucket["mileage_before"] + float(plan["old_mileage"]), 2)
            else:
                bucket["superseded_segments"].extend(plan["supersedes"])
                for c in self._terminal_active(registry, term):
                    if c["segment_code"] in plan["supersedes"] and \
                            c["segment_code"] not in bucket["_counted_old"]:
                        bucket["_counted_old"].add(c["segment_code"])
                        bucket["mileage_before"] = round(
                            bucket["mileage_before"] + float(c["mileage"]), 2)

        # 新输入里已经消失的旧分段（终端仍在场）：dropped，确认时撤销
        active_terminals = {str(seg.get("terminal")) for seg in raw_segments}
        for term in active_terminals:
            bucket = terminals.setdefault(term, {
                "terminal": term,
                "change_type": "unchanged",
                "mileage_before": 0.0,
                "mileage_after": 0.0,
                "new_segments": [],
                "reused_segments": [],
                "superseded_segments": [],
                "_counted_old": set(),
            })
            reused_codes = {p["reused_segment_code"] for p in planned
                            if str(p.get("terminal")) == term and p["reused"]}
            for c in self._terminal_active(registry, term):
                if c["segment_code"] not in old_refs and c["segment_code"] not in reused_codes \
                        and c["segment_code"] not in bucket["_counted_old"]:
                    bucket["superseded_segments"].append(c["segment_code"])
                    bucket["_counted_old"].add(c["segment_code"])
                    bucket["mileage_before"] = round(
                        bucket["mileage_before"] + float(c["mileage"]), 2)

        for bucket in terminals.values():
            bucket.pop("_counted_old", None)
            bucket["superseded_segments"] = sorted(set(bucket["superseded_segments"]))
            types = {s["change_type"] for s in bucket["new_segments"]}
            if "merged" in types and bucket["superseded_segments"]:
                bucket["change_type"] = "merged"
            elif {"split", "changed"} & types and bucket["superseded_segments"]:
                bucket["change_type"] = "split" if "split" in types else "changed"
            elif types <= {"reused"} and bucket["new_segments"]:
                bucket["change_type"] = "unchanged"
            elif "new" in types:
                bucket["change_type"] = "new"
            # 未受影响终端不应出现差额
            bucket["mileage_delta"] = 0.0 if bucket["change_type"] == "unchanged" else round(
                float(bucket["mileage_after"]) - float(bucket["mileage_before"]), 2)

        return planned, terminals

    # ------------------------------------------------------------------
    # 试算
    # ------------------------------------------------------------------

    def recalculate(
        self,
        batch_key: str,
        *,
        point_version: str | None = None,
        interval_sec: float = 300.0,
        distance_threshold: float = 500.0,
        trial_id: str | None = None,
        triggered_by: str = "manual",
    ) -> dict[str, Any]:
        """基于实际坐标版本重算切分并落试算版本；不修改任何已确认分段。"""
        points = self.read_points(batch_key, point_version)
        if not points:
            return {"batch_key": batch_key, "segments": 0, "total_points": 0}
        resolved_version = point_version or self.latest_point_version(batch_key) or "pt-v1"
        rule_versions = sorted({
            str(p.get("rule_version")) for p in points if p.get("rule_version") is not None
        })

        registry = self.load_registry(batch_key)
        seg_seq = int(registry.get("next_segment_seq", 1))
        registry["next_segment_seq"] = seg_seq + 1
        trial_count = len(registry["trials"]) + 1
        segment_version = f"seg-v{seg_seq}.trial{trial_count}"

        raw = self.segment_points(
            points,
            batch_key=batch_key,
            point_version=resolved_version,
            interval_sec=interval_sec,
            distance_threshold=distance_threshold,
        )
        # 先给每个原始切分临时编号，供 diff 内部引用；沿用分段在 diff 中会改回原编号，
        # 只有新增/合并/拆分出来的分段才占用新的稳定编号。
        for index, plan in enumerate(raw, start=1):
            plan["segment_code"] = f"__tmp_{index}"
        planned, terminals = self._diff_segments(registry, raw)
        for plan in planned:
            if not plan.get("reused"):
                plan["segment_code"] = self._alloc_segment_code(registry, batch_key)
        # 终端汇总里仍带着临时编号，同步刷新
        for bucket in terminals.values():
            for item in bucket["new_segments"]:
                matched = next(p for p in planned if p["terminal"] == bucket["terminal"]
                               and item["point_ids"] == p["point_ids"])
                item["segment_code"] = matched["segment_code"]

        version_file = self.segments_version_path(batch_key, segment_version)
        self._write_jsonl_overwrite(version_file, planned)

        report = {
            "batch_key": batch_key,
            "trial_id": trial_id,
            "segment_version": segment_version,
            "point_version": resolved_version,
            "rule_versions": rule_versions,
            "interval_sec": interval_sec,
            "distance_threshold": distance_threshold,
            "triggered_by": triggered_by,
            "created_at": time.time(),
            "status": "trial",
            "total_points": len(points),
            "segments": len(planned),
            "terminals": terminals,
            "segment_codes": [p["segment_code"] for p in planned],
            "version_file": str(version_file),
        }
        registry["trials"][segment_version] = {
            "trial_id": trial_id,
            "point_version": resolved_version,
            "status": "trial",
            "created_at": report["created_at"],
            "triggered_by": triggered_by,
            "report": report,
        }
        self._save_registry(registry)
        return report

    # ------------------------------------------------------------------
    # 确认晋升
    # ------------------------------------------------------------------

    def confirm(self, batch_key: str, segment_version: str) -> dict[str, Any]:
        """确认试算结果：新分段登记、原分段撤销并挂引用链，刷新当前版本指针。"""
        registry = self.load_registry(batch_key)
        trial = registry["trials"].get(segment_version)
        if trial is None:
            raise FileNotFoundError(
                f"批次 {batch_key} 不存在切分试算版本 {segment_version}")
        if trial.get("status") == "confirmed":
            return {"batch_key": batch_key, "segment_version": segment_version,
                    "message": "该试算版本已确认，无需重复确认"}

        planned = self._read_jsonl(self.segments_version_path(batch_key, segment_version))
        confirmed_at = time.time()
        new_registry_items: list[dict[str, Any]] = []
        superseded_codes: list[str] = []
        reused_codes: list[str] = []

        for plan in planned:
            code = plan["segment_code"]
            if plan.get("reused"):
                reused_code = plan["reused_segment_code"]
                reused_codes.append(reused_code)
                item = registry["segments"][reused_code]
                item.setdefault("segment_versions", []).append(segment_version)
                continue

            for old_code in plan.get("supersedes", []):
                old = registry["segments"].get(old_code)
                if old and old["status"] == "confirmed":
                    old["status"] = "superseded"
                    old["superseded_at"] = confirmed_at
                    old["superseded_by"] = code
                    old["supersede_reason"] = plan["change_type"]
                    superseded_codes.append(old_code)
                    store_row = store.find(self.MODULE, int(old["store_id"])) if old.get("store_id") else None
                    if store_row is not None:
                        store_row["状态标记"] = "已撤销"
                        store_row["撤销原因"] = plan["change_type"]
                        store_row["撤销后分段"] = code
                        store_row["撤销时间"] = confirmed_at

            entry, _ = self.create_entry({
                "分段编号": code,
                "终端编号": plan.get("terminal", "UNKNOWN"),
                "起始时间": time.strftime(
                    "%Y-%m-%dT%H:%M:%S", time.localtime(plan["start_time"])),
                "结束时间": time.strftime(
                    "%Y-%m-%dT%H:%M:%S", time.localtime(plan["end_time"])),
                "点数": str(plan["point_count"]),
                "间隔阈值": f"{plan['interval_threshold']}s",
                "里程": f"{plan['mileage']}m",
                "切分策略": plan["strategy"],
            })
            store_id = entry["id"] if entry else None
            if entry is not None:
                self.run_action(store_id, "确认切分")
                entry["分段版本"] = segment_version
                entry["坐标版本"] = plan.get("point_version")
                entry["替换分段"] = ",".join(plan.get("supersedes", []))
                entry["里程差额"] = plan.get("mileage_delta", 0.0)
                entry["坐标来源点数"] = plan["point_count"]

            registry["segments"][code] = {
                "segment_code": code,
                "store_id": store_id,
                "terminal": plan.get("terminal"),
                "status": "confirmed",
                "mileage": plan["mileage"],
                "point_ids": list(plan["point_ids"]),
                "point_version": plan.get("point_version"),
                "segment_versions": [segment_version],
                "rule_versions": plan.get("rule_versions", []),
                "reused_from": None,
                "supersedes": list(plan.get("supersedes", [])),
                "change_type": plan["change_type"],
                "mileage_delta": plan.get("mileage_delta", 0.0),
                "confirmed_at": confirmed_at,
            }
            new_registry_items.append(code)

        # 输入中整段消失的旧分段也要撤销
        for term_bucket in trial["report"]["terminals"].values():
            for old_code in term_bucket.get("superseded_segments", []):
                old = registry["segments"].get(old_code)
                if old and old["status"] == "confirmed" and old_code not in superseded_codes:
                    old["status"] = "superseded"
                    old["superseded_at"] = confirmed_at
                    old["supersede_reason"] = "dropped"
                    superseded_codes.append(old_code)

        registry["confirmed_segment_version"] = segment_version
        registry["latest_segment_version"] = segment_version
        registry["history"].append({
            "segment_version": segment_version,
            "point_version": trial["point_version"],
            "confirmed_at": confirmed_at,
            "new_segments": new_registry_items,
            "reused_segments": sorted(set(reused_codes)),
            "superseded_segments": sorted(set(superseded_codes)),
        })
        trial["status"] = "confirmed"
        trial["confirmed_at"] = confirmed_at
        self._save_registry(registry)

        # 当前版本指针 + 兼容副本（下游 get_segments 读到的就是实际航段版本）
        self._write_json(self.segments_pointer_path(batch_key), {
            "batch_key": batch_key,
            "segment_version": segment_version,
            "point_version": trial["point_version"],
            "updated_at": confirmed_at,
        })
        self._write_jsonl_overwrite(self.legacy_segments_path(batch_key), planned)

        return {
            "batch_key": batch_key,
            "segment_version": segment_version,
            "point_version": trial["point_version"],
            "new_segments": new_registry_items,
            "reused_segments": sorted(set(reused_codes)),
            "superseded_segments": sorted(set(superseded_codes)),
            "terminals": trial["report"]["terminals"],
            "confirmed_at": confirmed_at,
        }

    # ------------------------------------------------------------------
    # 查询
    # ------------------------------------------------------------------

    def current_segment_version(self, batch_key: str) -> str | None:
        pointer = self._read_json(self.segments_pointer_path(batch_key))
        if pointer and pointer.get("segment_version"):
            return str(pointer["segment_version"])
        return None

    def get_current_segments(self, batch_key: str) -> tuple[list[dict[str, Any]], str]:
        """读取当前已确认航段版本；无指针时退回兼容文件。"""
        version = self.current_segment_version(batch_key)
        if version is not None:
            return self._read_jsonl(
                self.segments_version_path(batch_key, version)), version
        legacy = self.legacy_segments_path(batch_key)
        if not legacy.exists():
            raise FileNotFoundError(f"批次 {batch_key} 的轨迹分段数据集不存在")
        return self._read_jsonl(legacy), "legacy"

    def get_version_segments(self, batch_key: str, segment_version: str) -> list[dict[str, Any]]:
        path = self.segments_version_path(batch_key, segment_version)
        if not path.exists():
            raise FileNotFoundError(f"批次 {batch_key} 的切分版本 {segment_version} 不存在")
        return self._read_jsonl(path)

    def get_segment_coordinate_sources(
        self,
        batch_key: str,
        segment_code: str,
        segment_version: str | None = None,
    ) -> dict[str, Any]:
        """查某个切分的坐标来源：默认查当前版本，也可指定历史/试算版本。"""
        if segment_version is None:
            segments, segment_version = self.get_current_segments(batch_key)
        else:
            segments = self.get_version_segments(batch_key, segment_version)
        for seg in segments:
            if seg.get("segment_code") == segment_code:
                return {
                    "batch_key": batch_key,
                    "segment_version": segment_version,
                    "segment_code": segment_code,
                    "point_version": seg.get("point_version"),
                    "point_ids": seg.get("point_ids", []),
                    "coord_source": seg.get("coord_source", []),
                }
        raise FileNotFoundError(
            f"批次 {batch_key} 版本 {segment_version} 下找不到分段 {segment_code}")

    def get_trial_report(self, batch_key: str, segment_version: str) -> dict[str, Any]:
        registry = self.load_registry(batch_key)
        trial = registry["trials"].get(segment_version)
        if trial is None:
            raise FileNotFoundError(f"批次 {batch_key} 不存在试算版本 {segment_version}")
        return trial["report"]
