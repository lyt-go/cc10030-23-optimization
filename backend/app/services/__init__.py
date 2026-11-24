"""领域服务注册表：通用资源路由按模块 key 分派到独立业务服务。"""
from __future__ import annotations

from app.services.base import BaseModuleService
from app.services.geo_source import GeoSourceService
from app.services.coord_transform import CoordTransformService
from app.services.track_segment import TrackSegmentService
from app.services.dwell_detect import DwellDetectService
from app.services.grid_index import GridIndexService
from app.services.geofence_rule import GeofenceRuleService
from app.services.spatial_join import SpatialJoinService
from app.services.poi_match import PoiMatchService
from app.services.region_dim import RegionDimService
from app.services.heatmap_aggregate import HeatmapAggregateService
from app.services.od_matrix import OdMatrixService
from app.services.route_restore import RouteRestoreService
from app.services.route_inspection import RouteInspectionService
from app.services.density_estimate import DensityEstimateService
from app.services.geo_quality import GeoQualityService
from app.services.dedup_merge import DedupMergeService
from app.services.geo_snapshot import GeoSnapshotService
from app.services.geo_backfill import GeoBackfillService
from app.services.geo_export import GeoExportService

SERVICES: dict[str, BaseModuleService] = {
    "geo_source": GeoSourceService(),
    "coord_transform": CoordTransformService(),
    "track_segment": TrackSegmentService(),
    "dwell_detect": DwellDetectService(),
    "grid_index": GridIndexService(),
    "geofence_rule": GeofenceRuleService(),
    "spatial_join": SpatialJoinService(),
    "poi_match": PoiMatchService(),
    "region_dim": RegionDimService(),
    "heatmap_aggregate": HeatmapAggregateService(),
    "od_matrix": OdMatrixService(),
    "route_restore": RouteRestoreService(),
    # 路径巡检为专用路由服务，不挂通用资源模块清单（前端 18 模块不变）。
    "route_inspection": RouteInspectionService(),
    "density_estimate": DensityEstimateService(),
    "geo_quality": GeoQualityService(),
    "dedup_merge": DedupMergeService(),
    "geo_snapshot": GeoSnapshotService(),
    "geo_backfill": GeoBackfillService(),
    "geo_export": GeoExportService(),
}


def service_for(module: str) -> BaseModuleService | None:
    return SERVICES.get(module)
