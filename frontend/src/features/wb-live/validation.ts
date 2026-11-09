import { ApiError } from '@/lib/api'
import type { WbAccount, WbCredential, WbProduct, WbProductsPage, WbSync } from './api'

function invalid(): never { throw new ApiError('Некорректный ответ сервиса WB', 502, 'INVALID_WB_RESPONSE') }
function record(value: unknown): Record<string, unknown> {
  if (!value || typeof value !== 'object' || Array.isArray(value)) invalid()
  return value as Record<string, unknown>
}
function string(value: unknown, max = 4096): string {
  if (typeof value !== 'string' || value.length > max) invalid()
  return value
}
function nullableString(value: unknown, max = 4096) { return value === null ? null : string(value, max) }
function integer(value: unknown, minimum = 0): number {
  if (typeof value !== 'number' || !Number.isSafeInteger(value) || value < minimum) invalid()
  return value
}
function boolean(value: unknown): boolean { if (typeof value !== 'boolean') invalid(); return value }
function array(value: unknown, max: number): unknown[] { if (!Array.isArray(value) || value.length > max) invalid(); return value }
function timestamp(value: unknown) {
  if (value === null) return null
  const text = string(value, 64)
  // Backend datetime fields may retain the PostgreSQL session's UTC offset.
  // Require an explicit zone, but do not reject valid non-UTC instants.
  if (!/^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,9})?(?:Z|[+-]\d{2}:\d{2})$/.test(text) || !Number.isFinite(Date.parse(text))) invalid()
  return text
}
function oneOf<T extends string>(value: unknown, options: readonly T[]): T {
  if (typeof value !== 'string' || !options.includes(value as T)) invalid()
  return value as T
}
function accountId(value: unknown, expected: number) { const id = integer(value, 1); if (id !== expected) invalid(); return id }
function identifier(value: unknown) { const id = string(value, 128); if (!/^\d+$/.test(id)) invalid(); return id }
function money(value: unknown) { return value === null ? null : identifier(value) }
function truncatedFields(value: unknown) { return value === undefined ? undefined : array(value, 64).map((item) => string(item, 128)) }

function parseAccount(value: unknown): WbAccount {
  const row = record(value)
  return { marketplaceAccountId: integer(row.marketplaceAccountId, 1), provider: oneOf(row.provider, ['wb', 'avito']), externalAccountId: string(row.externalAccountId, 256), displayName: nullableString(row.displayName, 512), status: string(row.status, 64) }
}
export function parseWbAccounts(value: unknown): WbAccount[] {
  const accounts = array(value, 10_000).map(parseAccount)
  if (new Set(accounts.map((account) => account.marketplaceAccountId)).size !== accounts.length) invalid()
  return accounts
}
export function parseWbCredential(value: unknown, expected: number): WbCredential {
  const row = record(value)
  return { marketplaceAccountId: accountId(row.marketplaceAccountId, expected), status: oneOf(row.status, ['missing', 'active', 'expired', 'revoked']), updatedAt: timestamp(row.updatedAt) }
}
export function parseWbConnection(value: unknown) {
  const row = record(value)
  const account = parseAccount(row.account)
  if (account.provider !== 'wb') invalid()
  return { account, credential: parseWbCredential(row.credential, account.marketplaceAccountId) }
}
function parseSources(value: unknown): WbSync['sources'] {
  const sources = array(value, 64).map((item) => {
    const row = record(item)
    return { source: string(row.source, 128), state: string(row.state, 64), processed: integer(row.processed), updatedAt: timestamp(row.updatedAt), errorCode: nullableString(row.errorCode, 256) }
  })
  if (new Set(sources.map((source) => source.source)).size !== sources.length) invalid()
  return sources
}
export function parseWbSync(value: unknown, expected: number): WbSync {
  const row = record(value)
  return { marketplaceAccountId: accountId(row.marketplaceAccountId, expected), jobId: nullableString(row.jobId, 128), state: oneOf(row.state, ['idle', 'queued', 'running', 'partial', 'completed', 'failed']), updatedAt: timestamp(row.updatedAt), sources: parseSources(row.sources) }
}
function parseProduct(value: unknown): WbProduct {
  const row = record(value)
  const sizes = array(row.sizes, 5).map((value) => {
    const size = record(value)
    const skus = size.skus === null ? null : array(size.skus, 0).map((sku) => string(sku, 255))
    const skusTruncated = boolean(size.skusTruncated)
    if (skus === null && skusTruncated) invalid()
    return { chrtId: identifier(size.chrtId), techSize: nullableString(size.techSize, 4096), skus, skusTruncated, priceKopecks: money(size.priceKopecks), discountedPriceKopecks: money(size.discountedPriceKopecks), truncatedFields: truncatedFields(size.truncatedFields) }
  })
  if (new Set(sizes.map((size) => size.chrtId)).size !== sizes.length) invalid()
  return {
    nmId: identifier(row.nmId), vendorCode: nullableString(row.vendorCode), title: nullableString(row.title), brand: nullableString(row.brand), subjectId: row.subjectId === null ? null : identifier(row.subjectId), subjectName: nullableString(row.subjectName), photoUrl: nullableString(row.photoUrl), contentUpdatedAt: timestamp(row.contentUpdatedAt), pricesUpdatedAt: timestamp(row.pricesUpdatedAt), sizes, sizesTruncated: boolean(row.sizesTruncated), truncatedFields: truncatedFields(row.truncatedFields),
  }
}
export function parseWbProducts(value: unknown, expected: number): WbProductsPage {
  const row = record(value)
  const items = array(row.items, 50).map(parseProduct)
  if (new Set(items.map((item) => item.nmId)).size !== items.length) invalid()
  return { marketplaceAccountId: accountId(row.marketplaceAccountId, expected), items, nextCursor: nullableString(row.nextCursor, 16384), readVersion: string(row.readVersion, 1024), readiness: oneOf(row.readiness, ['empty', 'partial', 'ready', 'error']), sources: parseSources(row.sources) }
}
