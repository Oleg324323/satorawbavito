export type WeekFinancialMetric = { kopecks?: number | null; previousKopecks?: number | null; deltaPct?: number | null }
export const WEEK_FINANCIAL_COLUMNS = [
  { key: 'commissionKopecks', label: 'Комиссия, ₽', tip: 'Удержание на базе реализации: retailAmount − forPay − acquiringFee, как в P&L; эквайринг исключён. По rrDate, возвраты вычитаются. Не смешивается с ppvzSalesCommission до услуг поверенного без НДС; неоднозначные корректировки не подменяются нулём.' },
  { key: 'logisticsKopecks', label: 'Логистика, ₽', tip: 'Стоимость логистики WB (deliveryService), не тариф и не возмещение из rebillLogisticCost.' },
  { key: 'storageKopecks', label: 'Хранение, ₽', tip: 'Расчётное распределение paidStorage по rrDate пропорционально физическим остаткам всех товаров на складах WB в тот день, без товаров в пути. При неполной истории показана известная часть; даты снимков и нераспределённый остаток указаны отдельно. Прибыль повторно не пересчитывается.' },
  { key: 'adSpendKopecks', label: 'Реклама, ₽', tip: 'Списания deduction, классифицированные как «WB Продвижение» по bonusTypeName финансового отчёта WB, за период по дате проводки rrDate. Расход кампаний campaignAdSpendKopecks учитывается отдельно и сюда не прибавляется. Списания без nmId показаны отдельно.' },
  { key: 'campaignAdSpendKopecks', label: 'Расход кампаний, ₽', tip: 'Расход рекламных кампаний WB Продвижение по дням рекламной статистики, с привязкой к товару. Уже загруженное поле campaignAdSpendKopecks; не складывается со списаниями финансового отчёта и не меняет расчёт прибыли.' },
  { key: 'acceptanceKopecks', label: 'Платная приёмка, ₽', tip: 'Платная приёмка WB (paidAcceptance), без распределения общих расходов.' },
  { key: 'penaltyKopecks', label: 'Штрафы, ₽', tip: 'Штрафы WB (penalty), включая отрицательные корректировки.' },
  { key: 'compensationKopecks', label: 'Компенсация, ₽', tip: 'Выплаты WB по операциям компенсации: доход, а не расход. Другие доплаты и коррекции продаж сюда не включаются.' },
  { key: 'taxKopecks', label: 'Налог, ₽', tip: 'Расчёт платформы по существующим датированным налоговым настройкам и базе выручки P&L. Без настроек — прочерк.' },
] as const

export function formatWeekMoney(value: number | null | undefined) {
  return typeof value === 'number' && Number.isFinite(value)
    ? `${(value / 100).toLocaleString('ru-RU', { maximumFractionDigits: 2 })} ₽` : '—'
}
