import { describe, expect, it } from 'vitest'
import { canUseDangerousPermission, getUserAccountScope, getUsers, patchUserAccess, revokeSession } from './repository'

describe('settings access repository', () => {
  it('keeps production order scope without advertising the removed marking module', () => {
    const scope = getUsers().find((user) => user.id === 'user-print-lead')?.scopes.find((item) => item.marketplace === 'wb')
    expect(scope?.modules).toEqual(['Заказы', 'Лист печати'])
    expect(scope?.accountIds).toEqual(['wb-ogni-main'])
    expect(scope?.dangerousPermissions).toEqual([])
  })
  it('keeps Avito account scope explicit per user', () => {
    expect(getUserAccountScope('user-reviews', 'avito')).toEqual(['avito-bless-msk'])
    expect(getUserAccountScope('user-print-lead', 'avito')).toEqual(['avito-bless-msk', 'avito-anomie-msk'])
  })

  it('tracks dangerous permissions separately from role visibility', () => {
    expect(canUseDangerousPermission('user-maria-f', 'message_send')).toBe(true)
    expect(canUseDangerousPermission('user-print-lead', 'message_send')).toBe(false)
    expect(canUseDangerousPermission('user-maxim-b', 'wallet_topup')).toBe(true)
  })

  it('requires approval for admin or owner role elevation', () => {
    const result = patchUserAccess('user-reviews', { role: 'admin' })
    expect(result.status).toBe('approval_required')
  })

  it('blocks revoking the current session', () => {
    const result = revokeSession('session-current')
    expect(result.status).toBe('blocked')
  })
})
