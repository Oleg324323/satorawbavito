import { expect, it } from 'vitest'
import { assignmentGeneration, assignmentsPending, orderedStrategyAssignment, rememberAssignmentRevisions } from './strategyAssignmentOrdering'

it('orders overlapping row/configured/bulk mutations and keeps unrelated products responsive', async () => {
  const started: string[] = []
  let release!: () => void
  const blocked = new Promise<void>(resolve => { release = resolve })
  const first = orderedStrategyAssignment('synthetic-ordering', ['a'], async () => {
    started.push('row-a'); await blocked; return {}
  })
  const bulk = orderedStrategyAssignment('synthetic-ordering', ['a', 'b'], async () => {
    started.push('configured-a-b'); return {}
  })
  const last = orderedStrategyAssignment('synthetic-ordering', ['b'], async () => {
    started.push('clear-b'); return {}
  })
  const independent = orderedStrategyAssignment('synthetic-ordering', ['c'], async () => {
    started.push('row-c'); return {}
  })
  await independent
  expect(started).toEqual(['row-a', 'row-c'])
  release()
  expect((await first).superseded).toBe(true)
  expect((await bulk).superseded).toBe(true)
  expect((await last).superseded).toBe(false)
  expect(started).toEqual(['row-a', 'row-c', 'configured-a-b', 'clear-b'])
  expect(assignmentsPending()).toBe(false)
})

it('uses the last committed revision when a queued mutation actually starts', async () => {
  rememberAssignmentRevisions('synthetic-cas', { a: 'revision-A' })
  const seen: unknown[] = []
  const before = assignmentGeneration()
  const first = orderedStrategyAssignment('synthetic-cas', ['a'], async revision => {
    seen.push(revision); return { assignmentRevisions: { a: 'revision-B' } }
  })
  const clear = orderedStrategyAssignment('synthetic-cas', ['a'], async revision => {
    seen.push(revision); return { assignmentRevisions: { a: 'revision-None' } }
  })
  await Promise.all([first, clear])
  expect(seen).toEqual([{ a: 'revision-A' }, { a: 'revision-B' }])
  expect(assignmentGeneration()).toBeGreaterThan(before)
})

it('keeps the next intent executable after a rejected older save', async () => {
  rememberAssignmentRevisions('synthetic-failure', { a: 'before' })
  const failed = orderedStrategyAssignment('synthetic-failure', ['a'], async () => { throw new Error('Rejected save') },
    async () => ({ a: 'actually-committed' }))
  const next = orderedStrategyAssignment('synthetic-failure', ['a'], async expected => {
    expect(expected).toEqual({ a: 'actually-committed' })
    return { saved: 'None', assignmentRevisions: { a: 'revision-None' } }
  })
  await expect(failed).rejects.toThrow('Rejected save')
  expect(await next).toEqual({ saved: 'None', assignmentRevisions: { a: 'revision-None' }, superseded: false })
  expect(assignmentsPending()).toBe(false)
})

it('blocks a subsequent write until an uncertain revision can be read; never retries a mutation', async () => {
  let writes = 0, reads = 0
  const failed = orderedStrategyAssignment('synthetic-uncertain', ['a'], async () => {
    writes++; throw new Error('lost response')
  }, async () => { reads++; throw new Error('read unavailable') })
  await expect(failed).rejects.toThrow('lost response')
  await expect(orderedStrategyAssignment('synthetic-uncertain', ['a'], async () => {
    writes++; return {}
  }, async () => { reads++; throw new Error('read unavailable') })).rejects.toThrow('read unavailable')
  expect(writes).toBe(1)
  expect(reads).toBe(2)
  const recovered = await orderedStrategyAssignment('synthetic-uncertain', ['a'], async expected => {
    expect(expected).toEqual({ a: 'current' }); writes++; return { assignmentRevisions: { a: 'cleared' } }
  }, async () => ({ a: 'current' }))
  expect(recovered.superseded).toBe(false)
  expect(writes).toBe(2)
})

it('does not borrow revisions or uncertain state from another authorization scope', async () => {
  rememberAssignmentRevisions('scope-one', { a: 'one' })
  await orderedStrategyAssignment('scope-two', ['a'], async expected => {
    expect(expected).toBeUndefined(); return { assignmentRevisions: { a: 'two' } }
  })
  await orderedStrategyAssignment('scope-one', ['a'], async expected => {
    expect(expected).toEqual({ a: 'one' }); return {}
  })
})
