import type { Anime, Candidate, MatchGroup } from './api'

export function groupCandidates(candidates: Candidate[], sources: string[]) {
  return Object.fromEntries(
    sources.map(source => [source, candidates.filter(item => item.source === source)]),
  ) as Record<string, Candidate[]>
}

export function hasExportBlockers(blockers: string[] | undefined): boolean {
  return Boolean(blockers?.length)
}

export function taskProgressText(message: string, progress: number): string {
  return `${message} · ${Math.round(progress * 100)}%`
}

function includesKeyword(values: Array<string | null | undefined>, keyword: string): boolean {
  const normalizedKeyword = keyword.trim().toLocaleLowerCase()
  if (!normalizedKeyword) return true
  return values.some(value => value?.toLocaleLowerCase().includes(normalizedKeyword))
}

export function matchesMatchGroupSearch(group: MatchGroup, keyword: string): boolean {
  return includesKeyword([
    group.display_title,
    ...group.files.flatMap(file => [file.relative_path, file.path, file.parsed_title]),
  ], keyword)
}

export function matchesAnimeSearch(anime: Anime, keyword: string): boolean {
  return includesKeyword([
    anime.title,
    anime.original_title,
    ...anime.files.flatMap(file => [file.relative_path, file.path, file.parsed_title]),
  ], keyword)
}

export interface EpisodeHealth {
  missingEpisodes: number[]
  unfilledCount: number
}

export function getEpisodeHealth(anime: Anime): EpisodeHealth {
  const presentFiles = anime.files.filter(file => file.status === 'present')
  const unfilledCount = presentFiles.filter(file => file.episode === null).length
  if (!anime.episode_count || anime.episode_count < 1) {
    return { missingEpisodes: [], unfilledCount }
  }

  const filledEpisodes = new Set(
    presentFiles
      .map(file => file.episode)
      .filter((episode): episode is string => episode !== null && /^\d+$/.test(episode))
      .map(episode => Number(episode))
      .filter(episode => episode >= 1 && episode <= anime.episode_count!),
  )
  const missingEpisodes = Array.from(
    { length: anime.episode_count },
    (_, index) => index + 1,
  ).filter(episode => !filledEpisodes.has(episode))

  return { missingEpisodes, unfilledCount }
}

export function missingEpisodeText(episodes: number[]): string {
  const visible = episodes.slice(0, 6).join('、')
  const remainder = episodes.length - 6
  return `缺第 ${visible}${remainder > 0 ? ` 等 ${episodes.length}` : ''} 集`
}
