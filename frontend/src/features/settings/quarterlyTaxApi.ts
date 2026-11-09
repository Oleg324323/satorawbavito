import { authorizationHeaders } from '@/features/auth/authApi'
import { ApiError, apiRequest } from '@/lib/api'

export type QuarterlyTaxSettings = { year: number; rates: Array<number | null>; configured: boolean[]; revision: string; timezone: string }
const endpoint = '/api/v1/wb-repricer/quarterly-tax'

export async function requestQuarterlyTax(token: string, year: number,
  update?: { rates: Array<number | null>; revision: string }, signal?: AbortSignal): Promise<QuarterlyTaxSettings> {
  // This legacy endpoint returns a plain object, not the canonical {data} envelope.
  const view = await apiRequest<QuarterlyTaxSettings>(update ? endpoint : `${endpoint}?year=${year}`, {
    method: update ? 'PUT' : 'GET', headers: authorizationHeaders(token), signal,
    ...(update ? { body: JSON.stringify({ year, ...update }) } : {}),
  })
  if (!view || view.year !== year || !Array.isArray(view.rates) || view.rates.length !== 4
    || view.rates.some(rate => rate !== null && (typeof rate !== 'number' || !Number.isFinite(rate) || rate < 0 || rate > 100))
    || !Array.isArray(view.configured) || view.configured.length !== 4 || view.configured.some(value => typeof value !== 'boolean')
    || !/^[a-f0-9]{64}$/i.test(view.revision) || typeof view.timezone !== 'string') {
    throw new Error('Сервер вернул неполные налоговые настройки. Повторите загрузку.')
  }
  return view
}

export function quarterlyTaxError(error: unknown, saving = false): string {
  if (error instanceof ApiError && error.status === 403) return saving
    ? 'Недостаточно прав для изменения налоговых настроек.' : 'Недостаточно прав для просмотра налоговых настроек.'
  if (error instanceof ApiError && error.status === 409) return 'Ставки изменились в другом сеансе. Перечитайте ставки и повторите сохранение. Ваши правки оставлены в форме.'
  return saving ? 'Не удалось сохранить налог. Ваши правки оставлены в форме. Повторите сохранение.'
    : 'Не удалось загрузить налоговые настройки. Повторите загрузку.'
}

export function mergeQuarterlyTaxValues(current: string[], rates: Array<number | null>, dirty: boolean[]) {
  return rates.map((rate, index) => dirty[index] ? current[index] : rate === null ? '' : String(rate))
}
