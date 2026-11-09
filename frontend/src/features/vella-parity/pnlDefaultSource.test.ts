import { readFileSync } from 'node:fs'
import { join } from 'node:path'
import { describe, expect, it } from 'vitest'

const source = readFileSync(
  join(process.cwd(), 'src/features/vella-parity/VellaHtmlParityPage.tsx'),
  'utf8',
)

describe('P&L default source', () => {
  it('opens on the WB financial report, not the 1C one', () => {
    // 1C is out of scope and disabled on the backend (legacyOneCEnabled=false),
    // so the operational P&L can never receive data.  Defaulting to it made the
    // whole tab look broken: it only ever rendered "Ждём операционные расходы
    // из 1С" while the WB financial report behind the toggle was fully working.
    expect(source).toContain("useState<PnlReportMode>('financial')")
    expect(source).not.toContain("useState<PnlReportMode>('operational')")
    expect(source).not.toContain("{ label: 'Операционный 1С', mode: 'operational' }")
  })
})
