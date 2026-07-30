import { describe, expect, it } from 'vitest'
import {
  getEpisodeHealth,
  groupCandidates,
  hasExportBlockers,
  matchesAnimeSearch,
  matchesMatchGroupSearch,
  missingEpisodeText,
  taskProgressText,
} from './utils'
import type { Anime, Candidate, MatchGroup, MediaFile } from './api'

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

const mediaFile = (id: number, episode: string | null, status = 'present'): MediaFile => ({
  id,
  path: `D:\\Anime\\${id}.mkv`,
  relative_path: `${id}.mkv`,
  size: 1,
  parsed_title: `Episode ${id}`,
  episode,
  duration: null,
  width: null,
  height: null,
  video_codec: null,
  status,
})

const anime = (episodeCount: number | null, files: MediaFile[]): Anime => ({
  id: 1,
  title: 'Example',
  original_title: null,
  description: null,
  year: null,
  media_type: 'TV Series',
  episode_count: episodeCount,
  studio: null,
  cover_url: null,
  episode_titles: {},
  genres: [],
  tags: [],
  field_provenance: {},
  catalog_health: {
    directory_name_mismatch: false,
    missing_nfo_count: 0,
    missing_episode_image_count: 0,
  },
  mappings: [],
  files,
  updated_at: '2026-07-28T00:00:00Z',
})

describe('episode health presentation', () => {
  it('finds gaps using only present files in the expected episode range', () => {
    const result = getEpisodeHealth(anime(4, [
      mediaFile(1, '1'),
      mediaFile(2, '3'),
      mediaFile(3, '2', 'missing'),
      mediaFile(4, '8'),
      mediaFile(5, 'S1'),
    ]))
    expect(result.missingEpisodes).toEqual([2, 4])
    expect(result.unfilledCount).toBe(0)
  })

  it('counts present files whose episode number is not filled', () => {
    const result = getEpisodeHealth(anime(null, [
      mediaFile(1, null),
      mediaFile(2, null, 'missing'),
    ]))
    expect(result.missingEpisodes).toEqual([])
    expect(result.unfilledCount).toBe(1)
  })

  it('shortens long missing episode lists', () => {
    expect(missingEpisodeText([1, 2, 3])).toBe('缺第 1、2、3 集')
    expect(missingEpisodeText([1, 2, 3, 4, 5, 6, 7])).toBe('缺第 1、2、3、4、5、6 等 7 集')
  })
})

describe('catalog search', () => {
  it('matches a pending group by anime title or file name, ignoring case', () => {
    const group: MatchGroup = {
      id: 1,
      display_title: '葬送的芙莉莲',
      search_keyword: '',
      status: 'pending',
      anime_id: null,
      files: [mediaFile(1, '1')],
      candidates: [],
    }
    group.files[0].relative_path = '[Group] Frieren - 01.mkv'

    expect(matchesMatchGroupSearch(group, '芙莉莲')).toBe(true)
    expect(matchesMatchGroupSearch(group, 'frieren')).toBe(true)
    expect(matchesMatchGroupSearch(group, '不存在')).toBe(false)
  })

  it('matches a bound anime by title, original title, or file name', () => {
    const item = anime(1, [mediaFile(1, '1')])
    item.title = '迷宫饭'
    item.original_title = 'Dungeon Meshi'
    item.files[0].relative_path = 'Delicious in Dungeon S01E01.mkv'

    expect(matchesAnimeSearch(item, '迷宫饭')).toBe(true)
    expect(matchesAnimeSearch(item, 'meshi')).toBe(true)
    expect(matchesAnimeSearch(item, 's01e01')).toBe(true)
    expect(matchesAnimeSearch(item, '  ')).toBe(true)
  })
})
