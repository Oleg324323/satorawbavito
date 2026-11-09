import { readFileSync } from 'node:fs'
import vm from 'node:vm'
import { expect, test, vi } from 'vitest'

const html = readFileSync(new URL('../../../public/vella-production.html', import.meta.url), 'utf8')
const script = html.slice(html.indexOf('const PERIOD_LABELS ='), html.indexOf('function setGlobalPeriod('))

function calendar() {
  const classes = () => ({ add: vi.fn(), remove: vi.fn(), toggle: vi.fn(), contains: () => false })
  const nodes: Record<string, any> = Object.fromEntries(['globalPeriod', 'calendarPopover', 'calendarMonths',
    'calendarStartInput', 'calendarEndInput', 'calendarVisibleRange', 'calendarSelection'].map(id => [id, {
    value: '', dataset: {}, classList: classes(), style: {}, setAttribute: vi.fn(), addEventListener: vi.fn(),
  }]))
  nodes.globalPeriod.dataset.vellaIsland = 'global-period-control'
  const body = { appendChild: vi.fn() }
  nodes.calendarPopover.parentElement = body
  const document = { body, activeElement: null, getElementById: (id: string) => nodes[id],
    querySelectorAll: () => [], querySelector: () => null }
  const apply = vi.fn(() => true)
  const context = vm.createContext({ document, window: { innerWidth: 1200, innerHeight: 900,
    __vellaApplyProductsCalendarRange: apply,
    __vellaWbCalendarState: { fromIso: '2026-10-01', toIso: '2026-10-06', mode: 'custom', days: 6,
      minIso: '2026-07-12', maxIso: '2026-10-09' } }, hideGlobalTip: vi.fn(), showToast: vi.fn() })
  vm.runInContext(script, context)
  const run = (code: string) => vm.runInContext(code, context)
  run('openCalendarPopover()')
  return { nodes, apply, run }
}

test('old two-month calendar opens on the applied WB range, not the demo dates', () => {
  const { nodes } = calendar()
  expect(nodes.calendarStartInput.value).toBe('01.10.2026')
  expect(nodes.calendarEndInput.value).toBe('06.10.2026')
  expect(nodes.calendarVisibleRange.textContent).toBe('Октябрь 2026 — Ноябрь 2026')
  expect(nodes.calendarMonths.innerHTML.match(/class="calendar-grid"/g)).toHaveLength(2)
  for (const day of ['07', '08', '09']) {
    const button = nodes.calendarMonths.innerHTML.match(new RegExp(`<button[^>]*data-date="2026-10-${day}"[^>]*>`))?.[0]
    expect(button).toBeDefined()
    expect(button).not.toContain('disabled')
  }
  expect(nodes.calendarMonths.innerHTML.match(/<button[^>]*data-date="2026-10-11"[^>]*>/)?.[0]).toContain('disabled')
})

test('date clicks edit the draft; Apply alone commits the exact uncached period', () => {
  const { run, apply } = calendar()
  run("selectCalendarDay('2026-10-07'); selectCalendarDay('2026-10-09')")
  expect(apply).not.toHaveBeenCalled()
  run('applyCalendarRange()')
  expect(apply).toHaveBeenCalledWith('2026-10-07', '2026-10-09', 'custom')
})

test('manual range uses the same Apply bridge and rejects future, reversed or blank input', () => {
  const { run, nodes, apply } = calendar()
  for (const [start, end] of [['07.10.2026', '11.10.2026'], ['09.10.2026', '07.10.2026'], ['', '09.10.2026']]) {
    nodes.calendarStartInput.value = start
    nodes.calendarEndInput.value = end
    run('onCalendarManualInput(); applyCalendarRange()')
    expect(apply).not.toHaveBeenCalled()
  }
  nodes.calendarStartInput.value = '07.10.2026'
  nodes.calendarEndInput.value = '09.10.2026'
  run('onCalendarManualInput(); applyCalendarRange()')
  expect(apply).toHaveBeenCalledWith('2026-10-07', '2026-10-09', 'custom')
})

test('presets end on the last completed Moscow day; Cancel does not apply', () => {
  const { run, nodes, apply } = calendar()
  run("selectCalendarPreset('7')")
  expect(nodes.calendarStartInput.value).toBe('03.10.2026')
  expect(nodes.calendarEndInput.value).toBe('09.10.2026')
  run('closeCalendarPopover(); openCalendarPopover()')
  expect(apply).not.toHaveBeenCalled()
  expect(nodes.calendarStartInput.value).toBe('01.10.2026')
  run("selectCalendarPreset('7'); applyCalendarRange()")
  expect(apply).toHaveBeenCalledWith('2026-10-03', '2026-10-09', '7')
})
