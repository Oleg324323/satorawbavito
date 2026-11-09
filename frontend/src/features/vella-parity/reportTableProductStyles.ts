// Only these WB report tables share this identity column. Keep the native
// header (sorting/focus/vertical pinning) and pin only its horizontal position.
export const WB_REPORT_PRODUCT_CSS = `
.vella-html-parity-root :is(#tab-rnp, #tab-pnl, #tab-week) .report-table-wrap:is(.rnp-table-workspace, .pnl-table-workspace, .week-table-workspace) {
  margin-left: 20px;
  margin-right: 20px;
  width: calc(100% - 40px);
  max-width: calc(100% - 40px);
  padding-left: 0;
  padding-right: 0;
}
.vella-html-parity-root :is(#tab-rnp, #tab-pnl, #tab-week) .report-table-wrap table.report-mid {
  overflow: visible;
}
.vella-html-parity-root :is(#tab-rnp, #tab-pnl, #tab-week) .report-table-wrap .report-sticky {
  position: sticky;
  left: 0;
  z-index: 9;
  background: var(--white, #fff);
  box-shadow: 6px 0 12px rgba(15, 23, 42, .06);
}
.vella-html-parity-root :is(#tab-rnp, #tab-pnl, #tab-week) .report-table-wrap thead th.report-sticky {
  z-index: 25;
  background: var(--gray-50, #f8fafc);
}
.vella-html-parity-root :is(#tab-rnp, #tab-pnl, #tab-week) .report-table-wrap tbody tr:hover .report-sticky {
  background: var(--gray-50, #f8fafc);
}
.vella-html-parity-root #tab-week.tab-content.active {
  display: block;
  overflow-y: auto;
  padding-bottom: 24px;
}
.vella-html-parity-root #tab-week .report-table-wrap.week-table-workspace {
  flex: none;
  min-height: 0;
  max-height: none;
  overflow-x: auto;
  overflow-y: hidden;
  overscroll-behavior-y: auto;
}
`
