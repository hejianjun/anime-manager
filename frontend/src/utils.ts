import type { Candidate } from './api'

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

