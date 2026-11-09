export type ScopedReportState<T> = { scope: string; state: T }

// Render-time guard: effect cleanup alone cannot hide an old report during
// the first render after the selected account, session, or period changes.
export function selectScopedReportState<T>(scope: string, published: ScopedReportState<T> | null, loading: T): T {
  return published?.scope === scope ? published.state : loading
}
