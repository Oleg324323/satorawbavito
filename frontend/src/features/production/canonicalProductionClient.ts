import { buildApiUrl } from '@/lib/api'
import { productionAssignmentInput, productionAssignmentResponse, productionCreateInput, productionCreateResponse, productionId,
  productionReadResponse, productionScope, type ProductionAssignmentInput, type ProductionCreateInput, type ProductionItem, type ProductionScope } from './canonicalProduction'

export class ProductionClientError extends Error {
  readonly kind: 'invalid' | 'stale' | 'busy' | 'rejected' | 'unavailable' | 'readback-required'
  readonly status?: number
  constructor(kind: ProductionClientError['kind'], status?: number) {
    super(kind === 'readback-required' ? 'Исход записи Production не подтверждён. Нужна явная проверка состояния; автоматического повтора нет.'
      : kind === 'rejected' ? 'Запрос Production отклонён сервером. Проверьте актуальные данные и права.'
        : kind === 'invalid' ? 'Некорректный запрос или ответ Production.' : kind === 'stale' ? 'Контекст Production изменился.'
          : kind === 'busy' ? 'Дождитесь завершения текущего запроса Production.' : 'Production недоступен.')
    this.kind = kind; this.status = status
  }
}
type Recovery = Readonly<{ workItemId: string | null; orderItemId: string; sourceItemVersion: string; minimumVersion?: string; outcome: 'unknown' | 'receipt'; operation: 'create' | 'assign' }>
export type PreparedProductionCommand = Readonly<{ operation: 'create' | 'assign' }>
type Command = { path: string; body: string; recovery: Recovery; expected?: ProductionItem; assignment?: ProductionAssignmentInput; used: boolean }

/** Dormant explicit-call client. No UI/effects, IDs/keys generation, token refresh,
 * cache, localStorage, print/physical actions or automatic write/readback/retry.
 * One instance per token/session/org/account epoch; dispose on any change.
 * POST receipts are NOT current-state or print-readiness evidence. After every
 * accepted/uncertain write, readback must be explicit before more authoring.
 */
export function createCanonicalProductionClient(token: string, scope: ProductionScope, isCurrent: () => boolean, fetcher: typeof fetch = fetch) {
  let captured: ProductionScope
  try { captured = productionScope.parse(scope) } catch { throw new ProductionClientError('invalid') }
  const base = `/api/v2/production/accounts/${captured.marketplaceAccountId}/work-items`
  let disposed = false, busy = false, controller: AbortController | null = null, recovery: Recovery | null = null, currentItem: ProductionItem | null = null
  const commands = new WeakMap<PreparedProductionCommand, Command>()
  const active = () => !disposed && Boolean(token) && isCurrent()
  function validate<T>(parse: () => T): T { try { return parse() } catch { throw new ProductionClientError('invalid') } }
  function authoring() {
    if (!active()) throw new ProductionClientError('stale')
    if (busy) throw new ProductionClientError('busy')
    if (recovery) throw new ProductionClientError('readback-required')
  }
  function prepare(command: Command): PreparedProductionCommand {
    if (new TextEncoder().encode(command.body).byteLength > 65536) throw new ProductionClientError('invalid')
    const handle = Object.freeze({ operation: command.recovery.operation }); commands.set(handle, command); return handle
  }
  async function request<T>(path: string, parse: (input: unknown) => T, command?: Command): Promise<T> {
    if (!active()) throw new ProductionClientError('stale')
    if (busy) throw new ProductionClientError('busy')
    busy = true
    const local = new AbortController(); controller = local
    let response: Response | undefined, reader: ReadableStreamDefaultReader<Uint8Array> | undefined, complete = false
    const abort = () => local.abort(), timer = setTimeout(abort, 20_000)
    const cancelled = new Promise<never>((_, reject) => local.signal.addEventListener('abort', () => reject(new ProductionClientError('stale')), { once: true }))
    void cancelled.catch(() => {})
    try {
      if (command) { command.used = true; currentItem = null; recovery = command.recovery }
      const fetching = fetcher(buildApiUrl(path), { method: command ? 'POST' : 'GET', credentials: 'omit', cache: 'no-store', redirect: 'error', signal: local.signal,
        headers: { Authorization: `Bearer ${token}`, Accept: 'application/json', ...(command ? { 'Content-Type': 'application/json' } : {}) }, ...(command ? { body: command.body } : {}) }).then(async result => {
        if (local.signal.aborted) { try { await result.body?.cancel() } catch { /* late transport cleanup */ } }
        return result
      })
      response = await Promise.race([fetching, cancelled])
      if (!active() || local.signal.aborted) throw new ProductionClientError('stale')
      if (response.status !== 200) {
        const rejected = [400, 401, 403, 404, 409, 422].includes(response.status)
        if (command && rejected) recovery = null
        throw new ProductionClientError(rejected ? 'rejected' : 'unavailable', response.status)
      }
      if (!response.body || response.headers.get('content-type')?.split(';')[0].trim().toLowerCase() !== 'application/json') throw new ProductionClientError('invalid')
      reader = response.body.getReader()
      const decoder = new TextDecoder('utf-8', { fatal: true }); let bytes = 0, text = ''
      while (true) {
        const chunk = await Promise.race([reader.read(), cancelled])
        if (!active() || local.signal.aborted) throw new ProductionClientError('stale')
        if (chunk.done) break
        bytes += chunk.value.byteLength
        if (bytes > 131072) throw new ProductionClientError('invalid')
        text += decoder.decode(chunk.value, { stream: true })
      }
      const result = validate(() => parse(JSON.parse(text + decoder.decode())))
      complete = true; return result
    } catch (error) {
      if (command && recovery) throw new ProductionClientError('readback-required')
      if (error instanceof ProductionClientError) throw error
      throw new ProductionClientError(!active() || local.signal.aborted ? 'stale' : response ? 'invalid' : 'unavailable')
    } finally {
      if (!complete) { abort(); try { if (reader) await reader.cancel(); else await response?.body?.cancel() } catch { /* preserve safe error */ } }
      try { reader?.releaseLock() } catch { /* preserve safe result */ }
      clearTimeout(timer); busy = false; if (controller === local) controller = null
    }
  }
  async function read(workItemId: string, expected?: { orderItemId: string; sourceItemVersion: string; minimumVersion?: string }) {
    validate(() => productionId.parse(workItemId))
    currentItem = null
    const item = await request(`${base}/${workItemId}`, value => {
      const result = productionReadResponse.parse(value).item
      if (result.organizationId !== captured.organizationId || result.marketplaceAccountId !== captured.marketplaceAccountId || result.workItemId !== workItemId
        || expected && (result.orderItemId !== expected.orderItemId || result.sourceItemVersion !== expected.sourceItemVersion
          || expected.minimumVersion && BigInt(result.version) < BigInt(expected.minimumVersion))) throw new Error()
      return Object.freeze(result)
    })
    if (!active()) throw new ProductionClientError('stale')
    currentItem = item; return item
  }
  return {
    dispose() { disposed = true; currentItem = null; controller?.abort() },
    getRecovery(): Recovery | null { return recovery ? Object.freeze({ ...recovery }) : null },
    read(workItemId: string) { return read(workItemId) },
    async readback() {
      const pending = recovery
      if (!pending?.workItemId) throw new ProductionClientError('readback-required')
      const item = await read(pending.workItemId, pending)
      // Current state does not prove that an unknown assignment key committed.
      recovery = null
      return { item, previousOutcome: pending.outcome, commandOutcomeVerified: false as const }
    },
    async readbackUnknownCreate() {
      const pending = recovery
      if (!pending || pending.operation !== 'create' || pending.outcome !== 'unknown' || pending.workItemId !== null) throw new ProductionClientError('readback-required')
      currentItem = null
      const item = await request(`${base}/by-order-item/${pending.orderItemId}?expectedSourceItemVersion=${pending.sourceItemVersion}`, value => {
        const result = productionReadResponse.parse(value).item
        if (result.organizationId !== captured.organizationId || result.marketplaceAccountId !== captured.marketplaceAccountId
          || result.orderItemId !== pending.orderItemId || result.sourceItemVersion !== pending.sourceItemVersion) throw new Error()
        return Object.freeze(result)
      })
      if (!active()) throw new ProductionClientError('stale')
      currentItem = item; recovery = null
      // Committed coherent state exists; this does not attribute creation to the
      // unknown POST or authorize replay of its already-used command handle.
      return { item, previousOutcome: 'unknown' as const, commandOutcomeVerified: false as const }
    },
    prepareCreate(input: ProductionCreateInput) {
      authoring(); const value = validate(() => productionCreateInput.parse(input))
      return prepare({ path: base, body: JSON.stringify(value), used: false,
        recovery: Object.freeze({ operation: 'create', workItemId: null, orderItemId: value.orderItemId, sourceItemVersion: value.expectedSourceItemVersion, outcome: 'unknown' }) })
    },
    prepareAssignment(item: ProductionItem, input: Omit<ProductionAssignmentInput, 'expectedVersion'>) {
      authoring()
      if (item !== currentItem) throw new ProductionClientError('invalid')
      const value = validate(() => productionAssignmentInput.parse({ ...productionAssignmentInput.omit({ expectedVersion: true }).parse(input), expectedVersion: item.version }))
      return prepare({ path: `${base}/${item.workItemId}/assignments`, body: JSON.stringify(value), used: false, expected: item, assignment: value,
        recovery: Object.freeze({ operation: 'assign', workItemId: item.workItemId, orderItemId: item.orderItemId, sourceItemVersion: item.sourceItemVersion, minimumVersion: item.version, outcome: 'unknown' }) })
    },
    async dispatch(handle: PreparedProductionCommand) {
      authoring()
      const command = commands.get(handle)
      if (!command || command.used || command.expected && command.expected !== currentItem) throw new ProductionClientError('invalid')
      if (command.recovery.operation === 'create') {
        const receipt = await request(command.path, value => productionCreateResponse.parse(value), command)
        if (!active()) throw new ProductionClientError('readback-required')
        recovery = Object.freeze({ ...command.recovery, workItemId: receipt.workItemId, outcome: 'receipt' })
        return { receipt, readbackRequired: true as const }
      }
      const receipt = await request(command.path, value => {
        const parsed = productionAssignmentResponse.parse(value), result = parsed.result, expected = command.expected!, assignment = command.assignment!
        if (result.workItemId !== expected.workItemId || result.version !== String(BigInt(expected.version) + 1n) || result.catalogSkuId !== assignment.catalogSkuId
          || result.sourceItemVersion !== expected.sourceItemVersion || result.requiredQuantity !== expected.requiredQuantity
          || result.plannedQuantity !== expected.plannedQuantity || result.remainingQuantity !== expected.remainingQuantity) throw new Error()
        return parsed
      }, command)
      if (!active()) throw new ProductionClientError('readback-required')
      recovery = Object.freeze({ ...command.recovery, outcome: 'receipt', minimumVersion: receipt.result.version })
      return { receipt, readbackRequired: true as const }
    },
  }
}
