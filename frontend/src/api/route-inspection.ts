import { request } from './client'

/**
 * 路径巡检专用接口客户端：申请打开、交付、重算试算、版本选择、对账与坐标来源核查。
 * 与后端 app/routers/route_inspection.py 的路由保持一致。
 */

export type TerminalReconciliation = {
  terminal: string
  affected: boolean
  mileage_before: number
  mileage_after: number
  mileage_delta: number
  delivered_segments: Array<{ segment_code: string; mileage: number; point_version: string | null }>
  current_segments: Array<{
    segment_code: string
    mileage: number
    point_version: string | null
    change_type: 'reused' | 'merged' | 'split' | 'changed' | 'new'
    supersedes: string[]
    reused_segment_code: string | null
    coord_source_size: number
  }>
  reused_segments: string[]
  superseded_segments: string[]
  superseded_refs: Array<{
    segment_code: string
    mileage: number
    superseded_by: string
    supersede_reason: string
  }>
}

export type Reconciliation = {
  application_id: number
  application_code: string
  batch_key: string
  baseline_locked: boolean
  first_delivery_version: string | null
  first_delivery_point_version: string | null
  current_segment_version: string
  current_point_version: string | null
  choices: Array<Record<string, unknown>>
  need_recheck_segments: string[]
  terminals: TerminalReconciliation[]
}

export type RecalcReport = {
  batch_key: string
  trial_id: string | null
  segment_version: string
  point_version: string
  rule_versions: string[]
  status: 'trial' | 'confirmed'
  total_points: number
  segments: number
  terminals: Record<
    string,
    {
      terminal: string
      change_type: 'unchanged' | 'merged' | 'split' | 'changed' | 'new'
      mileage_before: number
      mileage_after: number
      mileage_delta: number
      new_segments: Array<Record<string, unknown>>
      reused_segments: string[]
      superseded_segments: string[]
    }
  >
}

export type CoordinateSource = {
  batch_key: string
  segment_version: string
  segment_code: string
  point_version: string | null
  point_ids: string[]
  coord_source: Array<{
    point_id: string
    timestamp: number
    lat: number
    lon: number
    point_version: string
    rule_version: string | null
  }>
}

async function parseJsonOrThrow(response: Response, message: string): Promise<any> {
  if (!response.ok) {
    let detail = message
    try {
      const body = await response.json()
      if (body?.detail) detail = body.detail
    } catch {
      // 响应不是 JSON 时沿用默认错误信息
    }
    throw new Error(detail)
  }
  return response.json()
}

export async function openInspectionApplication(
  batchKey: string,
  terminals: string[],
  remark?: string,
): Promise<{ ok: boolean; message: string; entry: Record<string, any> }> {
  const response = await request('/api/route-inspection/applications', {
    method: 'POST',
    body: JSON.stringify({ batch_key: batchKey, terminals, remark }),
  })
  return parseJsonOrThrow(response, '巡检申请打开失败')
}

export async function listInspectionApplications(): Promise<{
  items: Record<string, any>[]
  total: number
}> {
  const response = await request('/api/route-inspection/applications')
  return parseJsonOrThrow(response, '巡检申请读取失败')
}

export async function getInspectionApplication(applicationId: number): Promise<Record<string, any>> {
  const response = await request(`/api/route-inspection/applications/${applicationId}`)
  return parseJsonOrThrow(response, '巡检申请详情读取失败')
}

export async function deliverInspectionApplication(
  applicationId: number,
): Promise<Record<string, any>> {
  const response = await request(
    `/api/route-inspection/applications/${applicationId}/deliver`,
    { method: 'POST' },
  )
  return parseJsonOrThrow(response, '巡检交付失败')
}

export async function recalculateInspection(
  applicationId: number,
  options: { intervalSec?: number; distanceThreshold?: number; pointVersion?: string } = {},
): Promise<RecalcReport> {
  const response = await request(
    `/api/route-inspection/applications/${applicationId}/recalculate`,
    {
      method: 'POST',
      body: JSON.stringify({
        interval_sec: options.intervalSec ?? 300,
        distance_threshold: options.distanceThreshold ?? 500,
        point_version: options.pointVersion ?? null,
      }),
    },
  )
  return parseJsonOrThrow(response, '巡检重算失败')
}

export async function chooseInspectionVersion(
  applicationId: number,
  segmentVersion: string,
): Promise<Record<string, any>> {
  const response = await request(
    `/api/route-inspection/applications/${applicationId}/choose`,
    { method: 'POST', body: JSON.stringify({ segment_version: segmentVersion }) },
  )
  return parseJsonOrThrow(response, '航段版本选择失败')
}

export async function getInspectionReconciliation(
  applicationId: number,
): Promise<Reconciliation> {
  const response = await request(
    `/api/route-inspection/applications/${applicationId}/reconciliation`,
  )
  return parseJsonOrThrow(response, '巡检对账数据读取失败')
}

export async function getSegmentCoordinateSources(
  applicationId: number,
  segmentCode: string,
  segmentVersion?: string,
): Promise<CoordinateSource> {
  const query = segmentVersion ? `?segment_version=${encodeURIComponent(segmentVersion)}` : ''
  const response = await request(
    `/api/route-inspection/applications/${applicationId}` +
      `/segments/${encodeURIComponent(segmentCode)}/coordinate-sources${query}`,
  )
  return parseJsonOrThrow(response, '切分坐标来源读取失败')
}
