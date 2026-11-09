import type {
  DateRange,
  DigestResponse,
  ExportResponse,
  ReportGroupBy,
  ReportId,
  ReportResponse,
} from './types'
import { apiRequest } from '@/lib/api'

async function requestJson<T>(url: string, init?: RequestInit): Promise<T> {
  const data = await apiRequest<T>(url, init)
  if (data === null) throw new Error('Сервер вернул пустой ответ')
  return data
}

function toQuery(params: Record<string, string | number | boolean | null | undefined>) {
  const query = new URLSearchParams()
  Object.entries(params).forEach(([key, value]) => {
    if (value !== undefined && value !== null && value !== '') query.set(key, String(value))
  })
  const text = query.toString()
  return text ? `?${text}` : ''
}

function dateParams(range: DateRange) {
  return { preset: range.preset, from: range.from, to: range.to }
}

export function fetchDigest(dateRange: DateRange) {
  return requestJson<DigestResponse>(`/api/wb/reports/digest${toQuery(dateParams(dateRange))}`)
}

export function fetchReport(reportId: ReportId, dateRange: DateRange, groupBy?: ReportGroupBy) {
  return requestJson<ReportResponse>(
    `/api/wb/reports/${reportId}${toQuery({ ...dateParams(dateRange), groupBy })}`,
  )
}

export function startReportJob(reportId: ReportId, dateRange: DateRange, groupBy?: ReportGroupBy) {
  return requestJson<Record<string, unknown>>(
    `/api/wb/reports/${reportId}/jobs${toQuery({ ...dateParams(dateRange), groupBy })}`,
    { method: 'POST' },
  )
}

export function refreshReportSourcesJob(reportId: ReportId, dateRange: DateRange, groupBy?: ReportGroupBy) {
  return requestJson<Record<string, unknown>>(
    `/api/wb/reports/${reportId}/refresh-sources-job${toQuery({ ...dateParams(dateRange), groupBy })}`,
    { method: 'POST' },
  )
}

export function fetchDigestJob(dateRange: DateRange) {
  return requestJson<Record<string, unknown>>(`/api/wb/reports/digest/status${toQuery(dateParams(dateRange))}`)
}

export function fetchPnlReport(dateRange: DateRange, source: 'operational' | 'financial') {
  return requestJson<ReportResponse>(
    `/api/wb/reports/pnl${toQuery({ ...dateParams(dateRange), source })}`,
  )
}

export function fetchReportExport(reportId: ReportId) {
  return requestJson<ExportResponse>(`/api/wb/reports/export/${reportId}`)
}

export async function fetchAlerts() {
  const data = await requestJson<{ items: unknown[] }>('/api/wb/reports/alerts')
  return data.items
}
