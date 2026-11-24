"""路径巡检专用接口：申请打开、交付、重算试算、版本选择、对账与坐标来源核查。"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from app.services.route_inspection import RouteInspectionService

router = APIRouter(prefix="/api/route-inspection", tags=["路径巡检"])


class OpenApplicationPayload(BaseModel):
    batch_key: str
    terminals: list[str] = Field(default_factory=list)
    remark: str | None = None


class RecalcPayload(BaseModel):
    interval_sec: float = 300.0
    distance_threshold: float = 500.0
    point_version: str | None = None


class ChoosePayload(BaseModel):
    segment_version: str


def _service() -> RouteInspectionService:
    # 延迟到请求时再取服务注册表：模块导入阶段 services 包可能尚未初始化完成，
    # 顶层导入会和 app.main 形成循环依赖。
    from app.services import SERVICES
    svc = SERVICES.get("route_inspection")
    if svc is None:  # 兜底：注册表未挂载时仍可独立工作
        svc = RouteInspectionService()
    return svc  # type: ignore[return-value]


@router.post("/applications", response_model=dict)
def open_application(payload: OpenApplicationPayload) -> dict[str, Any]:
    try:
        entry = _service().open_application(
            payload.batch_key, payload.terminals, payload.remark or "")
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return {"ok": True, "message": "巡检申请已打开", "entry": entry}


@router.get("/applications", response_model=dict)
def list_applications() -> dict[str, Any]:
    rows = _service().list_entries(page=1, size=200)[0]
    return {"items": rows, "total": len(rows), "page": 1, "size": len(rows)}


@router.get("/applications/{application_id}", response_model=dict)
def get_application(application_id: int) -> dict[str, Any]:
    try:
        return _service().get_application_detail(application_id)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.post("/applications/{application_id}/deliver", response_model=dict)
def deliver_application(application_id: int) -> dict[str, Any]:
    try:
        return _service().deliver(application_id)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.post("/applications/{application_id}/recalculate", response_model=dict)
def recalculate_application(application_id: int, payload: RecalcPayload) -> dict[str, Any]:
    try:
        return _service().recalculate(
            application_id,
            interval_sec=payload.interval_sec,
            distance_threshold=payload.distance_threshold,
            point_version=payload.point_version,
        )
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.post("/applications/{application_id}/choose", response_model=dict)
def choose_version(application_id: int, payload: ChoosePayload) -> dict[str, Any]:
    try:
        return _service().choose_version(application_id, payload.segment_version)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.get("/applications/{application_id}/reconciliation", response_model=dict)
def get_reconciliation(application_id: int) -> dict[str, Any]:
    try:
        return _service().get_reconciliation(application_id)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.get("/applications/{application_id}/segments/{segment_code}/coordinate-sources",
            response_model=dict)
def get_coordinate_sources(application_id: int, segment_code: str,
                            segment_version: str | None = None) -> dict[str, Any]:
    try:
        return _service().get_segment_coordinate_sources(
            application_id, segment_code, segment_version)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
