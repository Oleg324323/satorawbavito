export function storageAttributionLabel(row: { isUnallocated?: boolean; storageAttribution?: string | null }) {
  if (row.isUnallocated || row.storageAttribution === 'account_unallocated') return 'Нераспределённые начисления кабинета'
  if (row.storageAttribution === 'current_stock_allocation' || row.storageAttribution === 'current_wb_stock') {
    return 'Хранение: расчёт по текущему снимку остатков WB'
  }
  if (row.storageAttribution === 'historical_stock_allocation') return 'Хранение: расчёт по истории остатков WB'
  if (row.storageAttribution === 'wb_finance_report') return 'Прямые финансовые операции WB'
  return 'Источник хранения не подтверждён'
}
