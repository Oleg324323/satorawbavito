// Measure the actual product column: names, viewport and column visibility
// can change its width. WB article must start exactly at its right edge.
export function observeAbcStickyColumns(table: HTMLTableElement) {
  const productHeader = table.querySelector<HTMLElement>('th[data-vella-column="abc-position"]')
  if (!productHeader) return () => {}
  const update = () => {
    const width = productHeader.getBoundingClientRect().width
    const value = `${width}px`
    if (table.style.getPropertyValue('--abc-product-width') !== value) {
      table.style.setProperty('--abc-product-width', value)
    }
  }
  update()
  const observer = new ResizeObserver(update)
  observer.observe(productHeader)
  return () => observer.disconnect()
}
