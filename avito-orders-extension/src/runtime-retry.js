{
async function retryPageStateInspection(runAttempt, maxAttempts = 3, wait = null) {
  const attemptsLimit = Math.max(1, Number(maxAttempts) || 1)
  let lastResult = null
  for (let attempt = 1; attempt <= attemptsLimit; attempt += 1) {
    try {
      lastResult = await runAttempt(attempt)
    } catch (error) {
      lastResult = {
        apiAvailable: false,
        candidates: [],
        error: error instanceof Error ? error.message : String(error),
      }
    }
    if (lastResult?.apiAvailable) {
      return { ...lastResult, inspectorAttempts: attempt }
    }
    if (attempt < attemptsLimit && typeof wait === 'function') await wait(attempt)
  }
  return {
    ...(lastResult || { apiAvailable: false, candidates: [] }),
    inspectorAttempts: attemptsLimit,
  }
}

globalThis.SatornaRuntimeRetry = { retryPageStateInspection }
}
