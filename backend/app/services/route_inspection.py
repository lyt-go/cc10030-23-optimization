"""路径巡检领域服务：以已交付路段为对账基准，跟踪坐标修订后的切分重算与分段沿用。

关键规则：

- 巡检申请打开时快照当时已确认分段（打开时通过核查的旧分段，重算期间保留）；
- 交付动作锁定首版交付快照，作为后续对账基准且不可被之后的版本选择改写；
- 重算只产生试算版本（trial），选择版本后才由切分端确认晋升；
- 对账按终端给出新旧里程、里程差、沿用分段与被撤销原分段引用，
  未改终端差额为 0，且不允许把其他终端的既有分段挂给本终端新点。
"""
from __future__ import annotations

import time
from typing import Any

from app.services.base import BaseModuleService
from app.services.segment_recalc import SegmentRecalcService
from app.store import store


class RouteInspectionService(BaseModuleService):
    MODULE = "route_inspection"
    ENTITY = "巡检申请"
    OPERATION_TERM = "巡检"
    LIST_FIELDS = ["申请编号", "采集批次", "巡检终端", "申请状态", "首版交付", "当前航段版本", "待复验分段"]
    REQUIRED_FIELDS = ["申请编号", "采集批次", "巡检终端"]
    STATUS_ORDER = ["草稿", "待重算", "重算中", "待选择", "已交付"]
    ACTION_RULES = {
        "发起重算": "重算中",
        "提交选择": "待选择",
        "确认交付": "已交付",
        "退回草稿": "草稿",
    }
    NEGATIVE_ACTIONS = []

    def __init__(self) -> None:
        self.engine = SegmentRecalcService()

    # ------------------------------------------------------------------
    # 内部工具
    # ------------------------------------------------------------------

    @staticmethod
    def _terminal_list(row: dict[str, Any]) -> list[str]:
        raw = row.get("巡检终端", "")
        if isinstance(raw, list):
            return [str(x) for x in raw]
        return [part.strip() for part in str(raw).split(",") if part.strip()]

    def _snapshot_segments(self, batch_key: str, terminals: list[str]) -> dict[str, Any]:
        """对指定终端快照当前已确认航段，作为打开/交付时刻的输入留痕。"""
        segments, segment_version = self.engine.get_current_segments(batch_key)
        per_terminal: dict[str, list[dict[str, Any]]] = {t: [] for t in terminals}
        for seg in segments:
            term = str(seg.get("terminal"))
            if term in per_terminal:
                per_terminal[term].append({
                    "segment_code": seg.get("segment_code"),
                    "segment_version": segment_version,
                    "point_version": seg.get("point_version"),
                    "mileage": seg.get("mileage"),
                    "point_ids": list(seg.get("point_ids", [])),
                    "coord_source_size": len(seg.get("coord_source", [])),
                })
        point_version = self.engine.latest_point_version(batch_key)
        return {
            "batch_key": batch_key,
            "segment_version": segment_version,
            "point_version": point_version,
            "captured_at": time.time(),
            "terminals": per_terminal,
        }

    def _find_application(self, application_id: int) -> dict[str, Any]:
        row = store.find(self.MODULE, application_id)
        if row is None:
            raise FileNotFoundError(f"巡检申请 {application_id} 不存在或已归档")
        return row

    # ------------------------------------------------------------------
    # 申请生命周期
    # ------------------------------------------------------------------

    def open_application(self, batch_key: str, terminals: list[str],
                          remark: str = "") -> dict[str, Any]:
        """打开巡检申请：保留打开时刻通过核查的旧分段快照，供重算期间对照。"""
        if not terminals:
            raise ValueError("巡检终端不能为空")
        rows = store.rows(self.MODULE)
        next_id = max((int(r.get("id", 0)) for r in rows), default=0) + 1
        baseline = self._snapshot_segments(batch_key, terminals)
        entry, missing = self.create_entry({
            "申请编号": f"INS-{batch_key}-{next_id:03d}",
            "采集批次": batch_key,
            "巡检终端": ",".join(terminals),
        })
        if entry is None:
            raise ValueError(f"缺少必填字段: {missing}")
        entry["申请状态"] = "待重算"
        entry["首版交付"] = "未交付"
        entry["当前航段版本"] = baseline["segment_version"]
        entry["待复验分段"] = ""
        entry["remark"] = remark
        entry["baseline_at_open"] = baseline
        entry["delivered_baseline"] = None          # 首版交付后锁定，永不改写
        entry["delivery_history"] = []
        entry["trials"] = []
        entry["choices"] = []
        return entry

    def deliver(self, application_id: int) -> dict[str, Any]:
        """交付当前航段：首版交付快照锁定为对账基准；重复交付只追加历史不改首版。"""
        row = self._find_application(application_id)
        batch_key = row["采集批次"]
        terminals = self._terminal_list(row)
        snapshot = self._snapshot_segments(batch_key, terminals)

        record = {
            "delivery_seq": len(row["delivery_history"]) + 1,
            "segment_version": snapshot["segment_version"],
            "point_version": snapshot["point_version"],
            "delivered_at": snapshot["captured_at"],
            "terminals": snapshot["terminals"],
        }
        row["delivery_history"].append(record)
        if row["delivered_baseline"] is None:
            # 首版交付即对账基准，之后任何选择都不得改写
            row["delivered_baseline"] = record
            row["首版交付"] = "已交付"
        row["申请状态"] = "已交付"
        row["当前航段版本"] = snapshot["segment_version"]
        return {"ok": True, "application_id": application_id,
                "first_delivery_version": row["delivered_baseline"]["segment_version"],
                "delivery": record}

    def recalculate(self, application_id: int, interval_sec: float = 300.0,
                     distance_threshold: float = 500.0,
                     point_version: str | None = None) -> dict[str, Any]:
        """对申请批次发起试算重算：只落试算版本，不动已确认分段与交付基准。"""
        row = self._find_application(application_id)
        batch_key = row["采集批次"]
        row["申请状态"] = "重算中"
        report = self.engine.recalculate(
            batch_key,
            point_version=point_version,
            interval_sec=interval_sec,
            distance_threshold=distance_threshold,
            trial_id=row["申请编号"],
            triggered_by="route_inspection",
        )
        row["trials"].append({
            "segment_version": report["segment_version"],
            "point_version": report["point_version"],
            "created_at": report["created_at"],
            "status": "trial",
            "report": report,
        })
        row["trial_segment_version"] = report["segment_version"]
        row["申请状态"] = "待选择"
        return report

    def choose_version(self, application_id: int, segment_version: str) -> dict[str, Any]:
        """选择某个试算版本并由切分端独立确认；首版交付记录保持不变。"""
        row = self._find_application(application_id)
        trial = next((t for t in row["trials"]
                      if t["segment_version"] == segment_version and t["status"] == "trial"),
                     None)
        if trial is None:
            raise FileNotFoundError(
                f"申请 {row['申请编号']} 下不存在待选择的试算版本 {segment_version}")

        confirmation = self.engine.confirm(row["采集批次"], segment_version)
        trial["status"] = "confirmed"
        choice = {
            "segment_version": segment_version,
            "point_version": trial["point_version"],
            "chosen_at": time.time(),
            "new_segments": confirmation["new_segments"],
            "reused_segments": confirmation["reused_segments"],
            "superseded_segments": confirmation["superseded_segments"],
            "terminals": confirmation["terminals"],
        }
        row["choices"].append(choice)
        row["当前航段版本"] = segment_version
        row["申请状态"] = "已交付"

        # 打开时通过核查、如今已被新结果撤销的旧分段：下次仍需复验
        registry = self.engine.load_registry(row["采集批次"])
        need_recheck: list[str] = []
        baseline = row.get("baseline_at_open") or {}
        for term, segs in (baseline.get("terminals") or {}).items():
            for seg in segs:
                code = seg.get("segment_code")
                item = registry["segments"].get(code)
                if item and item.get("status") == "superseded":
                    need_recheck.append(code)
        row["待复验分段"] = ",".join(sorted(set(need_recheck)))
        return choice

    # ------------------------------------------------------------------
    # 对账与查询
    # ------------------------------------------------------------------

    def get_reconciliation(self, application_id: int) -> dict[str, Any]:
        """以首版交付（或打开快照）为基准，对照当前航段输出终端级对账结果。"""
        row = self._find_application(application_id)
        batch_key = row["采集批次"]
        terminals = self._terminal_list(row)
        registry = self.engine.load_registry(batch_key)

        baseline = row.get("delivered_baseline") or row.get("baseline_at_open")
        baseline_terms = (baseline or {}).get("terminals", {})
        current_segments, current_version = self.engine.get_current_segments(batch_key)
        current_by_term: dict[str, list[dict[str, Any]]] = {t: [] for t in terminals}
        for seg in current_segments:
            term = str(seg.get("terminal"))
            if term not in current_by_term:
                continue
            if seg.get("reused"):
                # 沿用分段在版本文件里只记录 reused_segment_code，补成完整视图，
                # 保证未受影响终端仍看到自己原编号、原里程的已确认分段。
                code = seg.get("reused_segment_code")
                registered = registry["segments"].get(code, {})
                view = dict(seg)
                view.update({
                    "segment_code": code,
                    "mileage": registered.get("mileage", seg.get("mileage")),
                    "point_ids": registered.get("point_ids", seg.get("point_ids", [])),
                    "point_version": registered.get("point_version", seg.get("point_version")),
                    "change_type": "reused",
                    "reused_segment_code": code,
                    "supersedes": [],
                })
                current_by_term[term].append(view)
            else:
                current_by_term[term].append(seg)

        result_terminals = []
        for term in terminals:
            old_segs = baseline_terms.get(term, [])
            new_segs = current_by_term.get(term, [])
            old_codes = {s.get("segment_code") for s in old_segs}
            new_codes = {s.get("segment_code") for s in new_segs}
            reused = sorted(old_codes & new_codes)
            superseded = sorted(
                code for code in old_codes - new_codes
                if registry["segments"].get(code, {}).get("status") == "superseded"
            )
            mileage_before = round(sum(float(s.get("mileage") or 0) for s in old_segs), 2)
            mileage_after = round(sum(float(s.get("mileage") or 0) for s in new_segs), 2)
            affected = bool(superseded) or any(
                (s.get("change_type") or "") != "reused" for s in new_segs
            )
            result_terminals.append({
                "terminal": term,
                "affected": affected,
                "mileage_before": mileage_before,
                "mileage_after": mileage_after,
                # 未受影响终端不应出现差额
                "mileage_delta": round(mileage_after - mileage_before, 2) if affected else 0.0,
                "delivered_segments": [
                    {"segment_code": s.get("segment_code"), "mileage": s.get("mileage"),
                     "point_version": s.get("point_version")}
                    for s in old_segs
                ],
                "current_segments": [
                    {"segment_code": s.get("segment_code"), "mileage": s.get("mileage"),
                     "point_version": s.get("point_version"),
                     "change_type": s.get("change_type", "reused"
                                          if s.get("segment_code") in reused else "new"),
                     "supersedes": s.get("supersedes", []),
                     "reused_segment_code": s.get("reused_segment_code"),
                     "coord_source_size": len(s.get("coord_source", []))}
                    for s in new_segs
                ],
                "reused_segments": reused,
                "superseded_segments": superseded,
                "superseded_refs": [
                    {
                        "segment_code": code,
                        "mileage": registry["segments"][code].get("mileage"),
                        "superseded_by": registry["segments"][code].get("superseded_by"),
                        "supersede_reason": registry["segments"][code].get("supersede_reason"),
                    }
                    for code in superseded
                ],
            })

        return {
            "application_id": application_id,
            "application_code": row.get("申请编号"),
            "batch_key": batch_key,
            "baseline_locked": row.get("delivered_baseline") is not None,
            "first_delivery_version": (row.get("delivered_baseline") or {}).get("segment_version"),
            "first_delivery_point_version": (row.get("delivered_baseline") or {}).get("point_version"),
            "current_segment_version": current_version,
            "current_point_version": self.engine.latest_point_version(batch_key),
            "choices": row.get("choices", []),
            "need_recheck_segments": [
                s for s in str(row.get("待复验分段", "")).split(",") if s
            ],
            "terminals": result_terminals,
        }

    def get_application_detail(self, application_id: int) -> dict[str, Any]:
        """申请详情：含打开快照、交付基准、历次试算与选择，全部输入均可核验。"""
        row = dict(self._find_application(application_id))
        row["reconciliation"] = self.get_reconciliation(application_id)
        return row

    def get_segment_coordinate_sources(self, application_id: int, segment_code: str,
                                        segment_version: str | None = None) -> dict[str, Any]:
        """通过巡检申请查某切分的坐标来源，只允许查本申请批次内的分段。"""
        row = self._find_application(application_id)
        return self.engine.get_segment_coordinate_sources(
            row["采集批次"], segment_code, segment_version)
