// Keep WB article adjacent to the existing frozen photo/article column.
export function observeProductsStickyColumns(table: HTMLTableElement) {
  const headers = Array.from(table.tHead?.rows[0]?.cells ?? []).slice(0, 2)
  const update = () => {
    const width = headers.reduce((total, cell) => total + cell.getBoundingClientRect().width, 0)
    table.style.setProperty('--wb-article-sticky-left', `${width}px`)
  }
  update()
  const observer = new ResizeObserver(update)
  headers.forEach(cell => observer.observe(cell))
  return () => observer.disconnect()
}
