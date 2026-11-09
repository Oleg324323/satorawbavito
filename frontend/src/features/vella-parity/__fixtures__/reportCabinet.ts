import type { CabinetMeView } from '@/features/auth/authApi'

// Report readers wait for tenant identity before they can use a saved cache.
export const reportCabinet: CabinetMeView = {
  organization: { organizationId: 77, slug: 'synthetic-reports', name: 'Synthetic reports', createdAt: '2026-01-01' },
  user: { userId: 'synthetic-user', organizationId: 77, email: 'synthetic@example.test', fullName: 'Synthetic user',
    permissionProfile: 'admin', permissions: ['settings:read', 'finance:read'], isActive: true, createdAt: '2026-01-01' },
  activeSession: null,
  preferences: { userId: 'synthetic-user', notificationSettings: {}, exportSettings: {}, timezone: 'Europe/Moscow', updatedAt: '2026-01-01' },
}
