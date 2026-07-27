import { describe, expect, it } from 'vitest'
import { groupCandidates, hasExportBlockers, taskProgressText } from './utils'
import type { Candidate } from './api'

const candidate = (id: number, source: string): Candidate => ({
  id,
  source,
  source_id: String(id),
  title: `Candidate ${id}`,
  year: null,
  episode_count: null,
  cover_url: null,
  score: 0.8,
  selected: false,
  is_mock: source !== 'anidb',
})

describe('candidate and task presentation', () => {
  it('keeps candidates separated by source', () => {
    const grouped = groupCandidates(
      [candidate(1, 'anidb'), candidate(2, 'dmm'), candidate(3, 'anidb')],
      ['anidb', 'dmm', 'getchu'],
    )
    expect(grouped.anidb.map(item => item.id)).toEqual([1, 3])
    expect(grouped.dmm.map(item => item.id)).toEqual([2])
    expect(grouped.getchu).toEqual([])
  })

  it('blocks export only when blockers exist', () => {
    expect(hasExportBlockers([])).toBe(false)
    expect(hasExportBlockers(['missing episode'])).toBe(true)
  })

  it('formats task progress as a percentage', () => {
    expect(taskProgressText('扫描中', 0.416)).toBe('扫描中 · 42%')
  })
})

