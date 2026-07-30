import axios from 'axios'

export const api = axios.create({ baseURL: '/api', timeout: 65000 })

api.interceptors.response.use(
  response => response,
  error => {
    const payload = error.response?.data
    return Promise.reject(new Error(payload?.message || error.message || '请求失败'))
  },
)

export interface MediaFile {
  id: number
  path: string
  relative_path: string
  size: number
  parsed_title: string
  episode: string | null
  duration: number | null
  width: number | null
  height: number | null
  video_codec: string | null
  status: string
}

export interface Candidate {
  id: number
  source: string
  source_id: string
  title: string
  year: number | null
  episode_count: number | null
  cover_url: string | null
  score: number
  selected: boolean
  is_mock: boolean
}

export interface DescriptionCandidate {
  source: string
  source_id: string
  title: string
  year: number | null
  cover_url: string | null
  score: number
}

export interface MatchGroup {
  id: number
  display_title: string
  search_keyword: string
  status: string
  anime_id: number | null
  files: MediaFile[]
  candidates: Candidate[]
}

export interface Anime {
  id: number
  title: string
  original_title: string | null
  description: string | null
  year: number | null
  media_type: string | null
  episode_count: number | null
  studio: string | null
  cover_url: string | null
  episode_titles: Record<string, string>
  genres: string[]
  tags: string[]
  field_provenance: Record<string, string>
  catalog_health: {
    directory_name_mismatch: boolean
    missing_nfo_count: number
    missing_episode_image_count: number
  }
  mappings: Array<{ source: string; source_id: string; is_mock: boolean }>
  files: MediaFile[]
  updated_at: string
}

interface AnimePage {
  items: Anime[]
  total: number
  page: number
  page_size: number
}

export async function getAllAnime(pageSize = 100): Promise<Anime[]> {
  const first = (await api.get<AnimePage>('/anime', {
    params: { page: 1, page_size: pageSize },
  })).data
  const pageCount = Math.ceil(first.total / first.page_size)
  if (pageCount <= 1) return first.items

  const remaining = await Promise.all(
    Array.from({ length: pageCount - 1 }, (_, index) =>
      api.get<AnimePage>('/anime', {
        params: { page: index + 2, page_size: first.page_size },
      }),
    ),
  )
  return first.items.concat(remaining.flatMap(response => response.data.items))
}
