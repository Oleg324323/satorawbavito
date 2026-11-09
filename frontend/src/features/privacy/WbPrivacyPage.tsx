import { useEffect } from 'react'

/** Standalone public document: no cabinet, session or marketplace data required. */
export function WbPrivacyPage() {
  useEffect(() => {
    document.title = 'Политика конфиденциальности — Satorna — цены Wildberries'
  }, [])

  return (
    <iframe
      src="/wb-privacy.html"
      title="Политика конфиденциальности Wildberries (WB)"
      className="fixed inset-0 h-dvh w-full border-0 bg-slate-50"
    />
  )
}
