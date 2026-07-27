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
  episode: number | null
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
  genres: string[]
  tags: string[]
  field_provenance: Record<string, string>
  mappings: Array<{ source: string; source_id: string; is_mock: boolean }>
  files: MediaFile[]
  updated_at: string
}

