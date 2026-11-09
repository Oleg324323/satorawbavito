// Keep the original header (and its sort/resize/focus handlers) in the table.
// A native sticky cell cannot follow an outer vertical scroller across an
// intermediate horizontal overflow container. Translate the real header only
// on that axis; existing sticky identity columns still own horizontal movement.
export function isWbTableRoute(pathname: string) {
  return pathname === '/wb' || pathname.startsWith('/wb/')
}

export function stickyHeaderShift(top: number, height: number, tableBottom: number, clipTop: number, clipBottom: number) {
  if (height <= 0 || top >= clipBottom || tableBottom <= clipTop) return 0
  return Math.max(0, Math.min(clipTop - top, tableBottom - height - top))
}

export function installWbStickyHeaders(root: HTMLElement) {
  const headers = new Map<HTMLTableSectionElement, { table: HTMLTableElement; original: string; shift: number }>()
  let frame = 0, disposed = false
  root.dataset.wbStickyHeaders = 'true'
  const resize = typeof ResizeObserver === 'undefined' ? null : new ResizeObserver(schedule)
  resize?.observe(root)

  function update() {
    if (disposed) return
    const present = new Set(root.querySelectorAll<HTMLTableSectionElement>('table > thead'))
    for (const [head, state] of headers) if (!present.has(head)) {
      head.style.translate = state.original; delete head.dataset.wbStickyHead
      resize?.unobserve(state.table); headers.delete(head)
    }
    for (const head of present) {
      const table = head.closest('table')!
      if (!headers.has(head)) {
        headers.set(head, { table, original: head.style.translate, shift: 0 })
        head.dataset.wbStickyHead = 'true'; resize?.observe(table)
      }
      const state = headers.get(head)!
      if (!table.getClientRects().length) continue
      let clipTop = 0, clipBottom = window.innerHeight
      let branch: HTMLElement = table
      for (let parent = table.parentElement; parent; parent = parent.parentElement) {
        const style = getComputedStyle(parent)
        if (/auto|scroll|hidden|clip/.test(style.overflowY)) {
          const rect = parent.getBoundingClientRect()
          clipTop = Math.max(clipTop, rect.top + parent.clientTop)
          clipBottom = Math.min(clipBottom, rect.top + parent.clientTop + parent.clientHeight)
          // Respect a sticky toolbar in the same scrollport, without inventing
          // a fixed pixel offset that would break when its height changes.
          for (const sibling of parent.children) {
            if (sibling === branch || !(sibling instanceof HTMLElement)) continue
            const siblingStyle = getComputedStyle(sibling)
            // Fixed popovers and drawer backdrops also live beside the tab.
            // They do not reserve space in this scrollport (even when open).
            // Only a visible sticky toolbar can move its usable top edge.
            if (siblingStyle.position !== 'sticky' || siblingStyle.visibility !== 'visible'
              || Number(siblingStyle.opacity) === 0 || sibling.hidden
              || sibling.getAttribute('aria-hidden') === 'true') continue
            const bounds = sibling.getBoundingClientRect(), tableBounds = table.getBoundingClientRect()
            if (bounds.top <= clipTop + 1 && bounds.bottom > clipTop
              && bounds.right > tableBounds.left && bounds.left < tableBounds.right) clipTop = bounds.bottom
          }
        }
        branch = parent
        if (parent === root) break
      }
      const rect = head.getBoundingClientRect()
      const shift = stickyHeaderShift(rect.top - state.shift, rect.height,
        table.getBoundingClientRect().bottom, clipTop, clipBottom)
      if (Math.abs(shift - state.shift) > .1) {
        head.style.translate = `0px ${shift}px`; state.shift = shift
      }
    }
  }
  function schedule() {
    if (!frame && !disposed) frame = requestAnimationFrame(() => { frame = 0; update() })
  }
  // Capture catches all nested scrollports, including the table's own one.
  root.addEventListener('scroll', schedule, true)
  window.addEventListener('scroll', schedule, true)
  window.addEventListener('resize', schedule)
  const mutation = new MutationObserver(schedule)
  mutation.observe(root, { childList: true, subtree: true, characterData: true })
  update()
  return () => {
    disposed = true; cancelAnimationFrame(frame); resize?.disconnect(); mutation.disconnect()
    root.removeEventListener('scroll', schedule, true)
    window.removeEventListener('scroll', schedule, true); window.removeEventListener('resize', schedule)
    for (const [head, state] of headers) { head.style.translate = state.original; delete head.dataset.wbStickyHead }
    delete root.dataset.wbStickyHeaders
  }
}
