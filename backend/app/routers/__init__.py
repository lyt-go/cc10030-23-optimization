"""业务路由汇总：通用模块共用一套资源路由，路径巡检等专用领域路由独立注册。"""
from __future__ import annotations

from app.routers import resource, route_inspection

# 专用路由必须排在通用 /api/{module} 资源路由之前，避免被动态路径抢先匹配。
ROUTERS = [route_inspection, resource]
