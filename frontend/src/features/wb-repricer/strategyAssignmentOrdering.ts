// A fetch abort cannot undo a server mutation. Overlapping product sets share
// tails so row, configured and bulk saves reach the server in intent order.
const tails = new Map<string, Promise<unknown>>()
const latest = new Map<string, number>()
const revisions = new Map<string, string>()
const uncertain = new Set<string>()
let generation = 0
let pending = 0

export const assignmentGeneration = () => generation
export const assignmentsPending = () => pending > 0
const key = (token: string, article: string) => JSON.stringify([token, article])

export function rememberAssignmentRevisions(token: string, values: Record<string, string>) {
  for (const [article, revision] of Object.entries(values)) revisions.set(key(token, article), revision)
}

export function orderedStrategyAssignment<T extends { assignmentRevisions?: Record<string, string> }>(
  token: string, articles: string[], run: (expected?: Record<string, string>) => Promise<T>,
  reconcile?: () => Promise<Record<string, string>>,
): Promise<T & { superseded: boolean }> {
  const keys = [...new Set(articles)].map(article => key(token, article))
  const intent = ++generation
  pending++
  for (const item of keys) latest.set(item, intent)
  const previous = keys.map(item => tails.get(item)).filter((item): item is Promise<unknown> => !!item)
  const task = Promise.allSettled(previous).then(async () => {
    const reconcileRevision = async () => {
      if (!reconcile) throw new Error('Предыдущее сохранение не подтверждено. Обновите состояние стратегии перед повтором.')
      const values = await reconcile()
      if (!articles.every(article => typeof values[article] === 'string' && values[article])) {
        throw new Error('Сервер не подтвердил актуальную версию стратегии. Повторите чтение состояния.')
      }
      rememberAssignmentRevisions(token, values)
      keys.forEach(item => uncertain.delete(item))
    }
    // Stay inside the product's ordered tail. A normal list reload deliberately
    // ignores revisions while mutations are pending and cannot repair this race.
    if (keys.some(item => uncertain.has(item))) await reconcileRevision()
    const expected = Object.fromEntries(articles.map(article => [article, revisions.get(key(token, article))]))
    // Legacy servers/fixtures may not expose revisions. Current servers do.
    const known = Object.values(expected).every(value => typeof value === 'string')
    try {
      const result = await run(known ? expected as Record<string, string> : undefined)
      if (result.assignmentRevisions) rememberAssignmentRevisions(token, result.assignmentRevisions)
      return { ...result, superseded: keys.some(item => latest.get(item) !== intent) }
    } catch (error) {
      // A transport/HTTP failure cannot tell us whether the server committed.
      // Reconcile once, never replay the mutation or disable its CAS condition.
      keys.forEach(item => uncertain.add(item))
      try { await reconcileRevision() } catch { /* Keep uncertain; the next intent must read before writing. */ }
      throw error
    }
  }).finally(() => {
    pending--
    generation++
    for (const item of keys) {
      if (tails.get(item) === task) tails.delete(item)
      if (latest.get(item) === intent) latest.delete(item)
    }
  })
  for (const item of keys) tails.set(item, task)
  return task
}
