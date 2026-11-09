import { useEffect, useLayoutEffect, useMemo, useRef, useState } from 'react'
import { createCanonicalReviewNotificationsClient } from './canonicalReviewNotificationsClient'
import { canonicalNotificationError, canonicalNotificationItems } from './canonicalNotificationView'
import type { ReviewNotificationList, ReviewNotificationScope } from './canonicalReviewNotifications'

export function useCanonicalNotificationInbox(options: {
  scope: ReviewNotificationScope | null; accessToken: string | null; sessionKey: string
}) {
  const { scope, accessToken, sessionKey } = options
  const scopeKey = `${sessionKey}:${scope?.organizationId}:${scope?.marketplaceAccountId}:${scope?.marketplace}`
  const generation = useRef(0)
  const requestSequence = useRef(0)
  const writePending = useRef(false)
  const clients = useRef(new Set<ReturnType<typeof createCanonicalReviewNotificationsClient>>())
  const [snapshot, setSnapshot] = useState<{ key: string; data: ReviewNotificationList } | null>(null)
  const [cursorScope, setCursorScope] = useState(scopeKey)
  const [cursors, setCursors] = useState<(string | null)[]>([null])
  const [pageIndex, setPageIndex] = useState(0)
  const [revision, setRevision] = useState(0)
  const [loading, setLoading] = useState(false)
  const [writing, setWriting] = useState(false)
  const [needsReadback, setNeedsReadback] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [notice, setNotice] = useState<string | null>(null)
  const cursor = cursors[pageIndex] ?? null
  const requestKey = `${scopeKey}:${cursor ?? 'first'}`
  useLayoutEffect(() => {
    generation.current += 1; writePending.current = false
    setSnapshot(null); setError(null); setNotice(null); setWriting(false); setNeedsReadback(false)
    setCursors([null]); setPageIndex(0); setCursorScope(scopeKey)
    return () => { generation.current += 1; for (const client of clients.current) client.dispose(); clients.current.clear() }
  }, [scopeKey, accessToken])

  useEffect(() => {
    if (!scope || !accessToken || cursorScope !== scopeKey) { setLoading(false); return }
    const epoch = generation.current, sequence = ++requestSequence.current
    let disposed = false
    const isCurrent = () => !disposed && generation.current === epoch && requestSequence.current === sequence
    const client = createCanonicalReviewNotificationsClient({ scope, accessToken, isCurrent, maxVisibleIds: 50, maxResponseBytes: 262_144 })
    clients.current.add(client); setLoading(true); setError(null)
    void client.list(cursor).then((result) => {
      if (!isCurrent()) return
      if (result.state === 'ready') {
        setSnapshot({ key: requestKey, data: result.data }); setNeedsReadback(false)
      } else if (result.state === 'conflict' && pageIndex > 0) {
        setSnapshot(null); setCursors([null]); setPageIndex(0)
        setNotice('Появились новые уведомления. Открыта первая страница.')
      } else {
        if (result.state === 'unauthenticated' || result.state === 'no-access' || result.state === 'not-found') setSnapshot(null)
        setError(canonicalNotificationError(result))
      }
    }).finally(() => { if (isCurrent()) setLoading(false) })
    return () => { disposed = true; client.dispose(); clients.current.delete(client) }
  }, [scopeKey, accessToken, cursorScope, cursor, revision, requestKey])

  const data = cursorScope === scopeKey && snapshot?.key === requestKey ? snapshot.data : null
  const items = useMemo(() => data?.capabilities.canRead ? canonicalNotificationItems(data) : [], [data])
  async function markRead(ids: string[]) {
    if (!scope || !accessToken || !data?.capabilities.canMarkRead || needsReadback || error || loading || writePending.current) return
    const selected = [...new Set(ids)].filter((id) => items.some((item) => item.id === id && !item.readAt))
    if (selected.length === 0 || selected.length > 50) return
    const epoch = generation.current, sequence = requestSequence.current
    const isCurrent = () => generation.current === epoch && requestSequence.current === sequence
    const client = createCanonicalReviewNotificationsClient({ scope, accessToken, recipientMembershipId: data.recipientMembershipId,
      isCurrent, maxVisibleIds: 50, maxResponseBytes: 262_144 })
    clients.current.add(client); writePending.current = true; setWriting(true); setError(null)
    try {
      const result = await client.mark(selected, 'read')
      if (!isCurrent()) return
      if (result.state === 'ready') setRevision((value) => value + 1)
      else {
        if (result.state === 'unauthenticated' || result.state === 'no-access' || result.state === 'not-found') setSnapshot(null)
        setError(canonicalNotificationError(result)); setNeedsReadback(result.outcome === 'unknown')
      }
    } finally {
      client.dispose(); clients.current.delete(client)
      if (isCurrent()) { writePending.current = false; setWriting(false) }
    }
  }
  return {
    data, items, loading, writing, error, notice, needsReadback, pageIndex,
    canMarkRead: Boolean(data?.capabilities.canMarkRead) && !loading && !writing && !needsReadback && !error,
    refresh: () => { if (!writePending.current) setRevision((value) => value + 1) },
    markRead,
    previous: () => { if (!writePending.current && !loading) setPageIndex((value) => Math.max(0, value - 1)) },
    next: () => { if (!writePending.current && !loading && data?.nextCursor) { setCursors((values) => [...values.slice(0, pageIndex + 1), data.nextCursor]); setPageIndex((value) => value + 1) } },
  }
}
