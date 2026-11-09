import type { ViteDevServer } from 'vite'

/** Vite 8 listen(0) falls back to 5173. Its public native server still supports
 * Node's one-shot kernel allocation and retains Vite's initialization wrapper.
 * No reserve/close/rebind, port scanning, or application configuration.
 */
export async function withSyntheticVite<T>(server: ViteDevServer, label: string, run: (origin: string) => Promise<T>): Promise<T> {
  const http = server.httpServer
  let origin: string | undefined
  try {
    if (!http) throw new Error('Synthetic fixture requires a native HTTP server')
    await new Promise<void>((resolve, reject) => {
      const failed = (error: Error) => { http.off('listening', listening); reject(error) }
      const listening = () => { http.off('error', failed); resolve() }
      http.once('error', failed); http.once('listening', listening)
      http.listen({ host: '127.0.0.1', port: 0 })
    })
    const address = http.address()
    if (!address || typeof address === 'string' || address.address !== '127.0.0.1' || address.port === 5173) throw new Error('Invalid synthetic ephemeral binding')
    origin = `http://127.0.0.1:${address.port}`
    console.info(`[synthetic:${label}] bound ${origin}`)
    return await run(origin)
  } finally {
    await server.close()
    if (http?.listening || http?.address()) throw new Error('Synthetic fixture listener leaked')
    console.info(`[synthetic:${label}] closed ${origin ?? 'before-listen'}`)
  }
}
