export function observeStatsStickyColumns(table: HTMLTableElement) {
  const headers = Array.from(table.tHead?.rows[0]?.cells ?? [])
  const productIndex = headers.findIndex(cell => cell.dataset.statsColumn === 'product')
  const preceding = productIndex < 0 ? [] : headers.slice(0, productIndex)
  const update = () => table.style.setProperty('--stats-product-left',
    `${preceding.reduce((total, cell) => total + cell.getBoundingClientRect().width, 0)}px`)
  update()
  if (typeof ResizeObserver === 'undefined') return () => {}
  const observer = new ResizeObserver(update)
  headers.forEach(cell => observer.observe(cell))
  return () => observer.disconnect()
}
