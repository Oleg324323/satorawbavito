type ApplyReceipt = {
  approval?: { status?: string | null; applyState?: string | null; priceConfirmed?: boolean | null; wbMutationSent?: boolean | null; statusLabel?: string | null; rowErrors?: unknown[] } | null
  job?: { state?: string | null; lastApplyResult?: { priceConfirmed?: boolean | null; wbMutationSent?: boolean | null; statusLabel?: string | null; rowErrors?: unknown[] } | null } | null
}

// Submission and successful upload status are not a confirmed current price.
export function priceApplyOutcome(receipt: ApplyReceipt): 'confirmed' | 'local' | 'pending' | 'blocked' | 'failed' {
  const state = receipt.job?.state ?? receipt.approval?.applyState ?? receipt.approval?.status
  if (state === 'local_applied') return 'local'
  if ((state === 'accepted' || state === 'applied')
    && (receipt.job?.lastApplyResult?.priceConfirmed === true || receipt.approval?.priceConfirmed === true)) return 'confirmed'
  if (state === 'failed' || state === 'blocked') return state
  const result = receipt.job?.lastApplyResult ?? receipt.approval
  if (['all_failed', 'canceled'].includes(result?.statusLabel ?? '')
    || (result?.statusLabel === 'partial_errors' && result.rowErrors?.length)) return 'failed'
  if (['sent', 'queued', 'processing', 'applying', 'accepted'].includes(state ?? '')
    || receipt.job?.lastApplyResult?.wbMutationSent || receipt.approval?.wbMutationSent) return 'pending'
  return 'failed'
}

export function priceWasApplied(receipt: ApplyReceipt): boolean {
  return ['confirmed', 'local'].includes(priceApplyOutcome(receipt))
}

export const PRICE_PENDING_MESSAGE = 'Цена ещё не подтверждена WB. Повторная отправка заблокирована; проверка статуса не отправляет цену заново.'
