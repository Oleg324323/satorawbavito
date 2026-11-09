import { createRoot } from 'react-dom/client'
import { RnpTableShellIsland, PnlLiveTableShellIsland, PnlLiveSourceStripIsland, WeekTableShellIsland } from '../VellaHtmlParityPage'
import { WB_REPORT_PRODUCT_CSS } from '../reportTableProductStyles'
import { WeekSourceStatus } from '../WeekSourceStatus'

const rows = Array.from({ length: 30 }, (_, index) => ({ nmId: null, sku: `FIXTURE-${index}`,
  productName: `Synthetic product ${index}`, photoUrl: null, buyoutCount: 34, buyoutSumKopecks: 4788531,
  financialSalesUnits: 39, financialReturnsUnits: 5, financialBuyoutState: 'confirmed' as const,
  revenueKopecks: 4788531, cogsKopecks: null, costReasons: ['cost_mapping_missing_or_ambiguous'],
  costOperationDates: ['2026-10-12'], sales: { units: 34, kopecks: 4788531, previousUnits: 52, previousKopecks: 6789654 },
}))
const warning = 'Выбранный период: показатели воронки неполные. Предыдущий период: рекламная статистика неполная. Выбранный период: показатели воронки неполные. Сравнение неполное: selected: baskets-summary; previous: comparison-pending'
const state = { status: 'ready' as const, report: { rows, blockerIds: ['WB_PNL_DATED_COST_MISSING'] } }

createRoot(document.getElementById('root')!).render(<div className="vella-html-parity-root">
  <style>{`
    body { margin: 0; } * { box-sizing: border-box; }
    .tab-content { height: 460px; width: 720px; overflow-y: auto; }
    .report-table-wrap { overflow: auto; }
    table.report-mid { border-collapse: separate; border-spacing: 0; overflow: hidden; width: max-content; }
    th, td { min-width: 150px; padding: 10px; } th:first-child, td:first-child { width: 300px; min-width: 300px; }
    #tab-week.tab-content.active { display: flex; flex-direction: column; }
    #tab-week .report-table-wrap { flex: 1 0 300px; }
    .week-source-status { padding: 12px; }
    ${WB_REPORT_PRODUCT_CSS}
  `}</style>
  <section id="tab-rnp" className="tab-content active"><RnpTableShellIsland replacementKey="rnp" state={state} /></section>
  <section id="tab-pnl" className="tab-content active">
    <PnlLiveSourceStripIsland replacementKey="cost-warning" state={state} source="financial" period={{ fromIso: '2026-10-12', toIso: '2026-10-18' }} />
    <PnlLiveTableShellIsland replacementKey="pnl" state={state} rows={rows} />
  </section>
  <section id="tab-week" className="tab-content active">
    <WeekSourceStatus warning={warning} />
    <p role="note">Без привязки к товару: Реклама 175 554 ₽</p>
    <WeekTableShellIsland replacementKey="week" state={state} marginUnit="п.п." exportAll={false} />
  </section>
</div>)
