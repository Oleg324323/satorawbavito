export function weekWarningGroups(warning: string) {
  const groups: Record<'selected' | 'previous' | 'other', string[]> = { selected: [], previous: [], other: [] }
  const messages = new Set(warning.split(/(?<=[.!?])\s+(?=[А-ЯA-Z])/u).map(text => text.trim()).filter(Boolean))
  for (const message of messages) {
    const key = /предыдущ(?:ий|его) период/iu.test(message) ? 'previous'
      : /выбранн(?:ый|ого) период/iu.test(message) ? 'selected' : 'other'
    groups[key].push(message)
  }
  return groups
}

export function WeekSourceStatus({ warning }: { warning?: string | null }) {
  if (!warning?.trim()) return null
  const groups = weekWarningGroups(warning)
  return (
    <div className="week-source-status" data-vella-island="week-source-status">
      <p role="status">Показаны доступные данные; полнота источников ограничена. Прочерк не означает ноль.</p>
      <details>
        <summary>Полнота данных и причины</summary>
        {([['selected', 'Выбранный период'], ['previous', 'Предыдущий период'], ['other', 'Другие ограничения']] as const)
          .filter(([key]) => groups[key].length > 0).map(([key, title]) => (
            <section key={key} aria-label={title}>
              <strong>{title}</strong>
              <ul>{groups[key].map(message => <li key={message}>{message}</li>)}</ul>
            </section>
          ))}
      </details>
    </div>
  )
}
