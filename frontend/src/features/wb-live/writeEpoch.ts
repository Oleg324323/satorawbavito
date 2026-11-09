// Identity alone cannot guard A → B → A. Every transition and unmount invalidates
// captured mutations, including callbacks belonging to an earlier mount.
export function createWbWriteEpoch() {
  let epoch = 0
  return {
    invalidate: () => { epoch += 1 },
    capture: () => { const captured = ++epoch; return () => captured === epoch },
    observe: () => { const captured = epoch; return () => captured === epoch },
  }
}
