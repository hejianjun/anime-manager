<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, ref } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import { api, getAllAnime, type Anime, type DescriptionCandidate, type MediaFile } from '../api'
import { getEpisodeHealth, hasExportBlockers, matchesAnimeSearch, missingEpisodeText } from '../utils'

const items = ref<Anime[]>([])
type IssueFilter = 'missing' | 'unfilled' | 'description' | 'directory' | 'nfo' | 'episodeImage'

const issueFilters = ref<IssueFilter[]>([])
const searchInput = ref('')
const searchKeyword = ref('')
const selected = ref<Anime | null>(null)
const detailOpen = ref(false)
const previewOpen = ref(false)
const preview = ref<any>(null)
const overwrite = ref(false)
const busy = ref(false)
const renameOpen = ref(false)
const renamePreview = ref<any>(null)
const renameSeason = ref(1)
const bulkRenameOpen = ref(false)
const bulkRenamePreview = ref<any>(null)
const bulkRenameSeason = ref(1)
const bulkRenamePreviewTaskId = ref<number | null>(null)
const bulkRenameRunning = ref(false)
const bulkRenameExecuting = ref(false)
const bulkRenameProgress = ref(0)
const bulkRenameTaskText = ref('')
const bulkArtifactOpen = ref(false)
const bulkArtifactPreview = ref<any>(null)
const bulkArtifactRunning = ref(false)
const bulkArtifactProgress = ref(0)
const bulkArtifactTaskText = ref('')
const coverErrors = ref<Record<number, boolean>>({})
const coverFallbacks = ref<Record<number, string>>({})
const playerOpen = ref(false)
const playerFile = ref<MediaFile | null>(null)
const removingMediaId = ref<number | null>(null)
const descriptionSearchOpen = ref(false)
const descriptionSearchKeyword = ref('')
const descriptionCandidates = ref<DescriptionCandidate[]>([])
const descriptionSearchErrors = ref<Array<{ source: string; message: string }>>([])
const descriptionSelection = ref('')
const descriptionSearching = ref(false)
const descriptionFilling = ref(false)
type GetchuCandidate = {
  source_id: string
  title: string
  year: number | null
  cover_url: string | null
  score: number
}
type GetchuRow = {
  anime_id: number
  anime_title: string
  search_keyword: string
  search_status: 'pending' | 'searching' | 'ready' | 'no_candidate' | 'failed' | 'cancelled'
  candidates: GetchuCandidate[]
  default_source_id: string | null
  description_preview: string | null
  nfo_targets: Array<{ path: string; kind: string; exists: boolean | null }>
  blockers: string[]
  error: { message: string } | null
}
const getchuOpen = ref(false)
const getchuPreviewTaskId = ref<number | null>(null)
const getchuRows = ref<GetchuRow[]>([])
const getchuSearchStatus = ref('idle')
const getchuSearchProgress = ref(0)
const getchuSearchMessage = ref('')
const getchuSelections = ref<Record<number, string>>({})
const getchuDetails = ref<Record<string, string>>({})
const getchuDetailLoading = ref<Record<number, boolean>>({})
const getchuConfirming = ref<Record<number, boolean>>({})
const getchuClosing = ref(false)
const getchuChildren = ref<any[]>([])
const handledGetchuTasks = new Set<number>()
let bulkRenameEvents: EventSource | null = null
let getchuEvents: EventSource | null = null
let getchuChildrenTimer: ReturnType<typeof setTimeout> | null = null
let getchuChildrenRequest: Promise<void> | null = null
const playerUrl = computed(() => playerFile.value ? `/api/media-files/${playerFile.value.id}/stream` : '')

const itemHealth = computed(() =>
  Object.fromEntries(items.value.map(item => [item.id, getEpisodeHealth(item)])),
)
const filteredItems = computed(() => {
  return items.value.filter((item) => {
    if (!matchesAnimeSearch(item, searchKeyword.value)) return false
    if (!issueFilters.value.length) return true
    const health = itemHealth.value[item.id]
    return issueFilters.value.some((filter) => {
      if (filter === 'missing') return health.missingEpisodes.length > 0
      if (filter === 'unfilled') return health.unfilledCount > 0
      if (filter === 'description') return !(item.description || '').trim()
      if (filter === 'directory') return item.catalog_health.directory_name_mismatch
      if (filter === 'nfo') return item.catalog_health.missing_nfo_count > 0
      return item.catalog_health.missing_episode_image_count > 0
    })
  })
})
const getchuEligibleItems = computed(() =>
  filteredItems.value.filter(item => !(item.description || '').trim()),
)
const missingAnimeCount = computed(() =>
  items.value.filter(item => itemHealth.value[item.id].missingEpisodes.length > 0).length,
)
const unfilledAnimeCount = computed(() =>
  items.value.filter(item => itemHealth.value[item.id].unfilledCount > 0).length,
)
const missingDescriptionCount = computed(() =>
  items.value.filter(item => !(item.description || '').trim()).length,
)
const directoryMismatchCount = computed(() =>
  items.value.filter(item => item.catalog_health.directory_name_mismatch).length,
)
const missingNfoCount = computed(() =>
  items.value.filter(item => item.catalog_health.missing_nfo_count > 0).length,
)
const missingEpisodeImageCount = computed(() =>
  items.value.filter(item => item.catalog_health.missing_episode_image_count > 0).length,
)
const bulkChangedFiles = computed(() =>
  (bulkRenamePreview.value?.files || []).filter((item: any) => item.changed),
)
const getchuTaskSummary = computed(() => ({
  confirmed: getchuChildren.value.length,
  queued: getchuChildren.value.filter(item => ['pending', 'running'].includes(item.status)).length,
  completed: getchuChildren.value.filter(item => item.status === 'completed').length,
  failed: getchuChildren.value.filter(item => item.status === 'failed').length,
}))
const renameKindLabels: Record<string, string> = {
  video: '视频',
  nfo: 'NFO',
  subtitle: '字幕',
  image: '图片',
}
const artifactKindLabels: Record<string, string> = {
  tvshow_nfo: '作品 NFO',
  season_nfo: '季 NFO',
  episode_nfo: '剧集 NFO',
  movie_nfo: '电影 NFO',
  poster: '作品主图',
  episode_image: '剧集图片',
}

function getchuCoverUrl(anime: Anime) {
  const getchu = anime.mappings.find(item => item.source === 'getchu' && !item.is_mock)
  return getchu ? `/api/sources/getchu/${encodeURIComponent(getchu.source_id)}/cover` : null
}

function coverUrl(anime: Anime) {
  if (coverFallbacks.value[anime.id]) return coverFallbacks.value[anime.id]
  if (anime.field_provenance.cover_url === 'getchu') {
    return getchuCoverUrl(anime) || anime.cover_url
  }
  return anime.cover_url
}

function markCoverError(anime: Anime) {
  const failedUrl = coverUrl(anime)
  const originalUrl = anime.cover_url
  // Getchu 代理失败时先尝试抓取结果中的原始地址；原图也失败才显示 NO COVER。
  if (
    anime.field_provenance.cover_url === 'getchu'
    && failedUrl !== originalUrl
    && originalUrl
  ) {
    coverFallbacks.value[anime.id] = originalUrl
    return
  }
  coverErrors.value[anime.id] = true
}

function applySearch() {
  searchKeyword.value = searchInput.value.trim()
}

function playMedia(file: MediaFile) {
  playerFile.value = file
  playerOpen.value = true
}

function closePlayer() {
  playerFile.value = null
}

async function load() {
  items.value = await getAllAnime()
}

async function show(item: Anime) {
  selected.value = (await api.get(`/anime/${item.id}`)).data
  detailOpen.value = true
}

async function refresh() {
  if (!selected.value) return
  busy.value = true
  try {
    selected.value = (await api.post(`/anime/${selected.value.id}/refresh`)).data
    await load()
    ElMessage.success('元数据已刷新')
  } catch (error) { ElMessage.error((error as Error).message) }
  finally { busy.value = false }
}

async function translateDescription() {
  if (!selected.value?.description) return
  try {
    await ElMessageBox.confirm(
      '翻译结果将覆盖当前简介，并在后续元数据刷新时保留。确认继续？',
      '翻译简介',
      { type: 'warning', confirmButtonText: '开始翻译', cancelButtonText: '取消' },
    )
  } catch {
    return
  }
  busy.value = true
  try {
    selected.value = (await api.post(`/anime/${selected.value.id}/translate-description`)).data
    await load()
    ElMessage.success('简介已翻译为简体中文')
  } catch (error) { ElMessage.error((error as Error).message) }
  finally { busy.value = false }
}

async function searchDescriptionCandidates() {
  if (!selected.value) return
  descriptionSearching.value = true
  descriptionSelection.value = ''
  try {
    const response = (await api.post(
      `/anime/${selected.value.id}/description-candidates`,
      null,
      { params: { keyword: descriptionSearchKeyword.value.trim() || selected.value.title } },
    )).data
    descriptionCandidates.value = response.items
    descriptionSearchErrors.value = response.errors
  } catch (error) {
    ElMessage.error((error as Error).message)
  } finally {
    descriptionSearching.value = false
  }
}

async function openDescriptionSearch() {
  if (!selected.value) return
  descriptionSearchKeyword.value = selected.value.original_title || selected.value.title
  descriptionCandidates.value = []
  descriptionSearchErrors.value = []
  descriptionSelection.value = ''
  descriptionSearchOpen.value = true
  await searchDescriptionCandidates()
}

async function fillDescription() {
  if (!selected.value || !descriptionSelection.value) return
  const candidate = descriptionCandidates.value.find(
    item => `${item.source}:${item.source_id}` === descriptionSelection.value,
  )
  if (!candidate) return
  descriptionFilling.value = true
  try {
    selected.value = (await api.post(`/anime/${selected.value.id}/fill-description`, {
      source: candidate.source,
      source_id: candidate.source_id,
    })).data
    descriptionSearchOpen.value = false
    await load()
    ElMessage.success('简介已补充，并保存了来源绑定')
  } catch (error) {
    ElMessage.error((error as Error).message)
  } finally {
    descriptionFilling.value = false
  }
}

function applyGetchuTask(current: any) {
  getchuSearchStatus.value = current.status
  getchuSearchProgress.value = Math.round((current.progress || 0) * 100)
  getchuSearchMessage.value = current.message || ''
  const rows = (current.result?.items || []) as GetchuRow[]
  getchuRows.value = rows
  for (const row of rows) {
    if (!getchuSelections.value[row.anime_id] && row.default_source_id) {
      getchuSelections.value[row.anime_id] = row.default_source_id
    }
    if (row.description_preview && row.default_source_id) {
      getchuDetails.value[`${row.anime_id}:${row.default_source_id}`] = row.description_preview
    }
  }
}

function watchGetchuPreview(taskId: number) {
  getchuEvents?.close()
  const events = new EventSource(`/api/tasks/${taskId}/events`)
  getchuEvents = events
  events.onmessage = (event) => {
    try {
      const current = JSON.parse(event.data)
      applyGetchuTask(current)
      if (['completed', 'failed'].includes(current.status)) {
        events.close()
        if (getchuEvents === events) getchuEvents = null
      }
    } catch (error) {
      ElMessage.error((error as Error).message)
    }
  }
  events.onerror = () => {
    getchuSearchMessage.value = '实时搜索连接中断，浏览器正在自动重连'
  }
}

async function refreshGetchuChildren() {
  if (!getchuPreviewTaskId.value) return
  if (getchuChildrenRequest) return getchuChildrenRequest
  const previewTaskId = getchuPreviewTaskId.value
  getchuChildrenRequest = (async () => {
    try {
      getchuChildren.value = (await api.get(`/tasks/${previewTaskId}/children`)).data
      let changed = false
      for (const task of getchuChildren.value) {
        if (task.status === 'completed' && !handledGetchuTasks.has(task.id)) {
          handledGetchuTasks.add(task.id)
          changed = true
        }
      }
      if (changed) await load()
    } catch {
      // 父任务刚创建或服务短暂不可用时，下一轮轮询会自动恢复。
    }
  })()
  try {
    await getchuChildrenRequest
  } finally {
    getchuChildrenRequest = null
  }
}

function startGetchuChildPolling() {
  if (getchuChildrenTimer) clearTimeout(getchuChildrenTimer)
  const poll = async () => {
    await refreshGetchuChildren()
    if (getchuPreviewTaskId.value) {
      getchuChildrenTimer = setTimeout(poll, 2000)
    }
  }
  void poll()
}

async function startGetchuPreview(force = false) {
  if (getchuPreviewTaskId.value && !force) {
    getchuOpen.value = true
    return
  }
  const animeIds = getchuEligibleItems.value.map(item => item.id)
  if (!animeIds.length) return
  busy.value = true
  try {
    const task = (await api.post('/anime/getchu-description-preview', { anime_ids: animeIds })).data
    getchuPreviewTaskId.value = task.id
    getchuSelections.value = {}
    getchuDetails.value = {}
    getchuChildren.value = []
    handledGetchuTasks.clear()
    applyGetchuTask(task)
    getchuOpen.value = true
    watchGetchuPreview(task.id)
    startGetchuChildPolling()
  } catch (error) {
    ElMessage.error((error as Error).message)
  } finally {
    busy.value = false
  }
}

function getchuDetailText(row: GetchuRow) {
  const sourceId = getchuSelections.value[row.anime_id]
  return getchuDetails.value[`${row.anime_id}:${sourceId}`] || row.description_preview || ''
}

async function loadGetchuDetail(row: GetchuRow) {
  const sourceId = getchuSelections.value[row.anime_id]
  if (!sourceId || !getchuPreviewTaskId.value) return
  const key = `${row.anime_id}:${sourceId}`
  if (getchuDetails.value[key]) return
  getchuDetailLoading.value[row.anime_id] = true
  try {
    const detail = (await api.post(`/anime/${row.anime_id}/getchu-description-detail`, {
      preview_task_id: getchuPreviewTaskId.value,
      source_id: sourceId,
    })).data
    getchuDetails.value[key] = detail.description_preview || '该候选没有简介'
  } catch (error) {
    ElMessage.error((error as Error).message)
  } finally {
    getchuDetailLoading.value[row.anime_id] = false
  }
}

function getchuChildFor(animeId: number) {
  return [...getchuChildren.value]
    .reverse()
    .find(item => item.result?.anime_id === animeId)
}

async function enqueueGetchuRow(row: GetchuRow, notify = true) {
  const sourceId = getchuSelections.value[row.anime_id]
  if (!sourceId || !getchuPreviewTaskId.value) return
  getchuConfirming.value[row.anime_id] = true
  try {
    await api.post(`/anime/${row.anime_id}/getchu-description-confirm`, {
      preview_task_id: getchuPreviewTaskId.value,
      source_id: sourceId,
    })
    await refreshGetchuChildren()
    if (notify) ElMessage.success(`${row.anime_title} 已加入简介和 NFO 写入队列`)
  } finally {
    getchuConfirming.value[row.anime_id] = false
  }
}

async function confirmGetchuRow(row: GetchuRow) {
  try {
    await enqueueGetchuRow(row)
  } catch (error) {
    ElMessage.error((error as Error).message)
  }
}

function getchuRowsReadyToQueue() {
  return getchuRows.value.filter((row) => {
    const child = getchuChildFor(row.anime_id)
    return row.search_status === 'ready'
      && Boolean(getchuSelections.value[row.anime_id])
      && !row.blockers.length
      && !['pending', 'running', 'completed'].includes(child?.status)
  })
}

async function beforeGetchuClose(done: () => void) {
  if (getchuClosing.value) return
  const waitingSearch = getchuRows.value.filter(row => ['pending', 'searching'].includes(row.search_status))
  const missingSelection = getchuRows.value.filter((row) => {
    const child = getchuChildFor(row.anime_id)
    return row.search_status === 'ready'
      && !getchuSelections.value[row.anime_id]
      && !['pending', 'running', 'completed'].includes(child?.status)
  })
  if (waitingSearch.length || missingSelection.length) {
    const details = [
      waitingSearch.length ? `${waitingSearch.length} 部仍在搜索` : '',
      missingSelection.length ? `${missingSelection.length} 部尚未选择候选` : '',
    ].filter(Boolean).join('，')
    try {
      await ElMessageBox.alert(
        `${details}，暂时无法全部写入。请等待搜索完成并选择候选。`,
        '还有作品未确认',
        { confirmButtonText: '返回选择', type: 'warning' },
      )
    } catch {
      // 用户关闭提示时继续留在当前弹窗。
    }
    return
  }

  const rowsToQueue = getchuRowsReadyToQueue()
  const activeCount = getchuChildren.value.filter(task => ['pending', 'running'].includes(task.status)).length
  if (!rowsToQueue.length && !activeCount) {
    done()
    return
  }
  try {
    await ElMessageBox.confirm(
      `还有 ${rowsToQueue.length} 部尚未确认，${activeCount} 部正在排队或写入。是否将剩余作品全部加入写入队列？`,
      '还有作品未写入',
      {
        confirmButtonText: '全部写入并关闭',
        cancelButtonText: '取消',
        type: 'warning',
      },
    )
  } catch {
    return
  }

  getchuClosing.value = true
  const failures: string[] = []
  try {
    // 顺序创建子任务，避免关闭瞬间并发占满数据库连接池。
    for (const row of rowsToQueue) {
      try {
        await enqueueGetchuRow(row, false)
      } catch (error) {
        failures.push(`${row.anime_title}: ${(error as Error).message}`)
      }
    }
    await refreshGetchuChildren()
    if (failures.length) {
      ElMessage.error(`有 ${failures.length} 部未能加入队列，请查看对应行错误`)
      return
    }
    if (rowsToQueue.length) ElMessage.success(`已将剩余 ${rowsToQueue.length} 部加入写入队列`)
    done()
  } finally {
    getchuClosing.value = false
  }
}

function requestGetchuClose() {
  void beforeGetchuClose(() => { getchuOpen.value = false })
}

async function cancelGetchuRow(row: GetchuRow) {
  if (!getchuPreviewTaskId.value) return
  try {
    await api.post(`/anime/${row.anime_id}/getchu-description-cancel`, {
      preview_task_id: getchuPreviewTaskId.value,
    })
  } catch (error) {
    ElMessage.error((error as Error).message)
  }
}

async function showPreview() {
  if (!selected.value) return
  try {
    preview.value = (await api.get(`/anime/${selected.value.id}/export-preview`)).data
    previewOpen.value = true
  } catch (error) { ElMessage.error((error as Error).message) }
}

async function runExport() {
  if (!selected.value || preview.value?.blockers?.length) return
  if (overwrite.value) {
    await ElMessageBox.confirm('已有文件将先备份再覆盖。确认继续？', '覆盖确认', { type: 'warning' })
  }
  busy.value = true
  try {
    const result = (await api.post(`/anime/${selected.value.id}/export`, { overwrite: overwrite.value })).data
    ElMessage.success(`已写入 ${result.written.length} 个文件`)
    previewOpen.value = false
  } catch (error) { ElMessage.error((error as Error).message) }
  finally { busy.value = false }
}

async function saveEpisode(file: any) {
  try {
    const episode = file.episode?.trim().toUpperCase() || null
    const response = await api.patch(`/media-files/${file.id}`, { episode })
    file.episode = response.data.episode
    ElMessage.success('集号已保存')
  } catch (error) { ElMessage.error((error as Error).message) }
}

async function removeMedia(file: MediaFile) {
  if (!selected.value) return
  try {
    await ElMessageBox.confirm(
      `确定将“${file.relative_path}”从当前作品中移除吗？磁盘上的物理文件不会被删除，移除后会回到待确认列表。`,
      '从作品中删除',
      {
        type: 'warning',
        confirmButtonText: '确认移除',
        cancelButtonText: '取消',
      },
    )
  } catch {
    return
  }
  removingMediaId.value = file.id
  try {
    await api.delete(`/anime/${selected.value.id}/media-files/${file.id}`)
    selected.value = (await api.get(`/anime/${selected.value.id}`)).data
    await load()
    ElMessage.success('已从作品中移除，物理文件未删除')
  } catch (error) {
    ElMessage.error((error as Error).message)
  } finally {
    removingMediaId.value = null
  }
}

async function previewRename() {
  if (!selected.value) return
  busy.value = true
  try {
    renamePreview.value = (await api.post(`/anime/${selected.value.id}/rename-preview`, { season: renameSeason.value })).data
    renameOpen.value = true
  } catch (error) { ElMessage.error((error as Error).message) }
  finally { busy.value = false }
}

async function renameFiles() {
  if (!selected.value || renamePreview.value?.blockers?.length) return
  await ElMessageBox.confirm(
    `将先补齐 ${renamePreview.value?.nfo_create_count || 0} 个缺失 NFO，再移动 ${renamePreview.value?.files?.filter((item: any) => item.changed).length || 0} 个文件；原作品文件夹随后移入隐藏目录“.delete”，已有 NFO 不会被覆盖。确认继续？`,
    '批量重命名确认',
    { type: 'warning' },
  )
  busy.value = true
  try {
    const result = (await api.post(`/anime/${selected.value.id}/rename`, { season: renameSeason.value })).data
    ElMessage.success(`已生成 ${result.written_nfos.length} 个 NFO、处理 ${result.moved.length} 个文件，归档 ${result.archived_dirs.length} 个旧文件夹`)
    renameOpen.value = false
    selected.value = (await api.get(`/anime/${selected.value.id}`)).data
    await load()
  } catch (error) { ElMessage.error((error as Error).message) }
  finally { busy.value = false }
}

async function previewBulkRename() {
  bulkRenameRunning.value = true
  bulkRenamePreviewTaskId.value = null
  bulkRenameProgress.value = 0
  bulkRenameTaskText.value = '正在启动重命名预览'
  try {
    const task = (await api.post('/anime/rename-preview', { season: bulkRenameSeason.value })).data
    const completed = await waitForRenameTask(task.id, '重命名预览失败')
    bulkRenamePreview.value = completed.result
    bulkRenamePreviewTaskId.value = task.id
    bulkRenameOpen.value = true
  } catch (error) { ElMessage.error((error as Error).message) }
  finally { bulkRenameRunning.value = false }
}

function waitForRenameTask(taskId: number, failureMessage: string): Promise<any> {
  bulkRenameEvents?.close()
  return new Promise((resolve, reject) => {
    const events = new EventSource(`/api/tasks/${taskId}/events`)
    bulkRenameEvents = events
    events.onmessage = (event) => {
      try {
        const current = JSON.parse(event.data)
        bulkRenameProgress.value = Math.round(current.progress * 100)
        bulkRenameTaskText.value = current.message
        if (current.status === 'completed') {
          events.close()
          bulkRenameEvents = null
          resolve(current)
        } else if (current.status === 'failed') {
          events.close()
          bulkRenameEvents = null
          reject(new Error(current.error?.message || failureMessage))
        }
      } catch (error) {
        events.close()
        bulkRenameEvents = null
        reject(error)
      }
    }
    events.onerror = () => {
      bulkRenameTaskText.value = '实时进度连接中断，正在自动重连'
    }
  })
}

async function renameAllFiles() {
  if (
    !bulkRenamePreviewTaskId.value
    || bulkRenameRunning.value
    || bulkRenamePreview.value?.blockers?.length
    || (!bulkRenamePreview.value?.changed_count && !bulkRenamePreview.value?.cleanup_count)
  ) return
  await ElMessageBox.confirm(
    `将先补齐 ${bulkRenamePreview.value.nfo_create_count || 0} 个缺失 NFO，再处理 ${bulkRenamePreview.value.changed_count} 个文件，并把 ${bulkRenamePreview.value.cleanup_count} 个旧文件夹移入隐藏目录“.delete”。已有 NFO 不会被覆盖，确认继续？`,
    '全部作品批量重命名确认',
    { type: 'warning' },
  )
  bulkRenameExecuting.value = true
  bulkRenameProgress.value = 0
  bulkRenameTaskText.value = '正在启动批量重命名'
  try {
    const task = (await api.post('/anime/rename', {
      preview_task_id: bulkRenamePreviewTaskId.value,
    })).data
    bulkRenamePreviewTaskId.value = null
    const completed = await waitForRenameTask(task.id, '全部作品重命名失败')
    const result = completed.result
    ElMessage.success(`已处理 ${result.anime_count} 部作品，生成 ${result.written_nfos.length} 个 NFO、移动 ${result.moved.length} 个文件、归档 ${result.archived_dirs.length} 个旧文件夹，跳过 ${result.skipped.length} 部作品`)
    bulkRenameOpen.value = false
    await load()
  } catch (error) { ElMessage.error((error as Error).message) }
  finally { bulkRenameExecuting.value = false }
}

async function previewBulkArtifacts() {
  busy.value = true
  try {
    bulkArtifactPreview.value = (await api.post('/anime/artifacts-preview')).data
    bulkArtifactProgress.value = 0
    bulkArtifactTaskText.value = ''
    bulkArtifactOpen.value = true
  } catch (error) { ElMessage.error((error as Error).message) }
  finally { busy.value = false }
}

async function writeBulkArtifacts() {
  if (
    bulkArtifactPreview.value?.blockers?.length
    || !bulkArtifactPreview.value?.files?.length
  ) return
  await ElMessageBox.confirm(
    `将补写 ${bulkArtifactPreview.value.nfo_count} 个 NFO、${bulkArtifactPreview.value.poster_count} 张作品主图，并生成 ${bulkArtifactPreview.value.episode_image_count} 张剧集图片${bulkArtifactPreview.value.auto_translate_description ? `；写入前自动翻译约 ${bulkArtifactPreview.value.translation_candidate_count} 部作品简介` : ''}；已有文件不会覆盖。确认继续？`,
    '批量写入确认',
    { type: 'warning' },
  )
  bulkArtifactRunning.value = true
  bulkArtifactProgress.value = 0
  bulkArtifactTaskText.value = '正在启动批量写入'
  try {
    const task = (await api.post('/anime/artifacts')).data
    let current = task
    for (;;) {
      current = (await api.get(`/tasks/${task.id}`)).data
      bulkArtifactProgress.value = Math.round(current.progress * 100)
      bulkArtifactTaskText.value = current.message
      if (current.status === 'completed') break
      if (current.status === 'failed') {
        throw new Error(current.error?.message || '批量写入失败')
      }
      await new Promise(resolve => setTimeout(resolve, 800))
    }
    const result = current.result
    const summary = `已写入 ${result.written.length} 个文件，跳过已有 ${result.existing.length} 个，翻译简介 ${result.translated_anime_ids?.length || 0} 部`
    if (result.failed.length || result.translation_failed?.length) {
      ElMessage.warning(`${summary}，文件失败 ${result.failed.length} 个，翻译失败 ${result.translation_failed?.length || 0} 部`)
    }
    else ElMessage.success(summary)
    bulkArtifactOpen.value = false
    await load()
  } catch (error) { ElMessage.error((error as Error).message) }
  finally { bulkArtifactRunning.value = false }
}

onMounted(load)
onBeforeUnmount(() => {
  bulkRenameEvents?.close()
  getchuEvents?.close()
  if (getchuChildrenTimer) clearTimeout(getchuChildrenTimer)
})
</script>

<template>
  <section class="panel">
    <div class="panel-title">
      <div><p class="eyebrow">CATALOG</p><h2>已绑定作品</h2></div>
      <div class="panel-actions">
        <span class="muted">{{ filteredItems.length }} / {{ items.length }} 部</span>
        <el-button
          :disabled="!getchuEligibleItems.length"
          :loading="busy"
          @click="startGetchuPreview()"
        >
          {{ getchuPreviewTaskId ? '查看 Getchu 补简介进度' : `从 Getchu 批量补简介（${getchuEligibleItems.length}）` }}
        </el-button>
        <el-button :loading="busy" @click="previewBulkArtifacts">批量写入 NFO/主图/剧集图片</el-button>
        <el-button :loading="bulkRenameRunning" @click="previewBulkRename">全部批量重命名（仅目录不一致）</el-button>
      </div>
    </div>
    <div v-if="bulkRenameRunning" class="bulk-match-progress">
      <el-progress :percentage="bulkRenameProgress" />
      <span class="muted">{{ bulkRenameTaskText }}</span>
    </div>
    <div class="toolbar catalog-search">
      <el-input
        v-model="searchInput"
        clearable
        placeholder="搜索番名或文件名"
        @clear="applySearch"
        @keyup.enter="applySearch"
      />
      <el-button type="primary" plain @click="applySearch">搜索</el-button>
    </div>
    <div class="catalog-filter">
      <span class="muted">仅显示</span>
      <el-checkbox-group v-model="issueFilters">
        <el-checkbox-button value="missing">缺集 {{ missingAnimeCount }}</el-checkbox-button>
        <el-checkbox-button value="unfilled">集数未填写 {{ unfilledAnimeCount }}</el-checkbox-button>
        <el-checkbox-button value="description">缺少简介 {{ missingDescriptionCount }}</el-checkbox-button>
        <el-checkbox-button value="directory">目录名不一致 {{ directoryMismatchCount }}</el-checkbox-button>
        <el-checkbox-button value="nfo">缺少 NFO {{ missingNfoCount }}</el-checkbox-button>
        <el-checkbox-button value="episodeImage">缺少剧集图片 {{ missingEpisodeImageCount }}</el-checkbox-button>
      </el-checkbox-group>
      <el-button v-if="issueFilters.length" text @click="issueFilters = []">显示全部</el-button>
    </div>
    <div class="anime-grid">
      <article v-for="item in filteredItems" :key="item.id" class="anime-card" @click="show(item)">
        <div class="anime-cover">
          <img
            v-if="coverUrl(item) && !coverErrors[item.id]"
            :src="coverUrl(item) || ''"
            :alt="`${item.title} 封面`"
            loading="lazy"
            referrerpolicy="no-referrer"
            @error="markCoverError(item)"
          >
          <span v-else>NO COVER</span>
        </div>
        <div class="anime-card-content">
          <p class="eyebrow">{{ item.media_type || 'ANIME' }}</p>
          <h3>{{ item.title }}</h3>
          <span class="muted">{{ item.original_title || '暂无原始标题' }}</span>
          <div class="anime-meta">
            <el-tag v-if="item.year">{{ item.year }}</el-tag>
            <el-tag type="info">{{ item.files.length }} 个文件</el-tag>
            <el-tag v-if="itemHealth[item.id].missingEpisodes.length" type="danger">
              {{ missingEpisodeText(itemHealth[item.id].missingEpisodes) }}
            </el-tag>
            <el-tag v-if="itemHealth[item.id].unfilledCount" type="warning">
              {{ itemHealth[item.id].unfilledCount }} 个文件未填集数
            </el-tag>
            <el-tag v-if="!(item.description || '').trim()" type="danger">
              缺少简介
            </el-tag>
            <el-tag v-if="item.catalog_health.directory_name_mismatch" type="warning">
              目录名不一致
            </el-tag>
            <el-tag v-if="item.catalog_health.missing_nfo_count" type="danger">
              缺 {{ item.catalog_health.missing_nfo_count }} 个 NFO
            </el-tag>
            <el-tag v-if="item.catalog_health.missing_episode_image_count" type="warning">
              缺 {{ item.catalog_health.missing_episode_image_count }} 张剧集图片
            </el-tag>
            <el-tag v-for="mapping in item.mappings" :key="mapping.source" :type="mapping.is_mock ? 'warning' : 'success'">{{ mapping.source }}</el-tag>
          </div>
        </div>
      </article>
      <div v-if="!items.length" class="empty">确认候选后，作品会显示在这里</div>
      <div v-else-if="!filteredItems.length" class="empty">没有符合当前搜索或筛选条件的作品</div>
    </div>
  </section>

  <el-dialog
    v-model="getchuOpen"
    width="min(1180px, 97vw)"
    title="Getchu 边搜索、边确认、边写入"
    append-to-body
    :before-close="beforeGetchuClose"
  >
    <div class="getchu-summary">
      <el-progress :percentage="getchuSearchProgress" :status="getchuSearchStatus === 'failed' ? 'exception' : undefined" />
      <span class="muted">{{ getchuSearchMessage }}</span>
      <div class="getchu-counters">
        <el-tag type="info">已确认 {{ getchuTaskSummary.confirmed }}</el-tag>
        <el-tag type="warning">排队/写入 {{ getchuTaskSummary.queued }}</el-tag>
        <el-tag type="success">成功 {{ getchuTaskSummary.completed }}</el-tag>
        <el-tag type="danger">失败 {{ getchuTaskSummary.failed }}</el-tag>
      </div>
    </div>
    <el-alert
      v-if="getchuSearchStatus === 'failed'"
      type="error"
      :closable="false"
      title="批量搜索任务失败；已确认的独立写入任务不受影响"
    />
    <el-table :data="getchuRows" row-key="anime_id" size="small" max-height="650">
      <el-table-column prop="anime_title" label="作品" min-width="190" show-overflow-tooltip>
        <template #default="{ row }">
          <b>{{ row.anime_title }}</b>
          <div class="muted getchu-keyword">{{ row.search_keyword }}</div>
        </template>
      </el-table-column>
      <el-table-column label="搜索" width="105">
        <template #default="{ row }">
          <el-tag v-if="row.search_status === 'pending'" type="info">等待搜索</el-tag>
          <el-tag v-else-if="row.search_status === 'searching'" type="warning">搜索中</el-tag>
          <el-tag v-else-if="row.search_status === 'ready'" type="success">可确认</el-tag>
          <el-tag v-else-if="row.search_status === 'cancelled'" type="info">已取消</el-tag>
          <el-tag v-else type="danger">{{ row.search_status === 'no_candidate' ? '无候选' : '失败' }}</el-tag>
          <el-button
            v-if="row.search_status === 'pending'"
            text
            type="danger"
            size="small"
            @click="cancelGetchuRow(row)"
          >取消</el-button>
        </template>
      </el-table-column>
      <el-table-column label="Getchu 候选" min-width="300">
        <template #default="{ row }">
          <el-select
            v-if="row.candidates.length"
            v-model="getchuSelections[row.anime_id]"
            placeholder="请选择候选"
            style="width:100%"
            @change="loadGetchuDetail(row)"
          >
            <el-option
              v-for="candidate in row.candidates"
              :key="candidate.source_id"
              :value="candidate.source_id"
              :label="`${candidate.title} · ${candidate.year || '-'} · ${Math.round(candidate.score * 100)}%`"
            />
          </el-select>
          <span v-else-if="row.error" class="getchu-error">{{ row.error.message }}</span>
          <span v-else class="muted">候选返回后可立即确认</span>
          <div v-if="getchuDetailLoading[row.anime_id]" class="muted">正在加载简介…</div>
          <div v-else-if="getchuDetailText(row)" class="getchu-description-preview">
            {{ getchuDetailText(row) }}
          </div>
        </template>
      </el-table-column>
      <el-table-column label="NFO" width="150">
        <template #default="{ row }">
          <div>{{ row.nfo_targets.length }} 个关联 NFO</div>
          <small class="muted">
            更新 {{ row.nfo_targets.filter((item: any) => item.exists).length }} ·
            新建 {{ row.nfo_targets.filter((item: any) => item.exists === false).length }}
          </small>
          <el-tooltip v-if="row.blockers.length" :content="row.blockers.join('；')">
            <el-tag type="danger" size="small">存在阻塞</el-tag>
          </el-tooltip>
        </template>
      </el-table-column>
      <el-table-column label="写入状态" width="130">
        <template #default="{ row }">
          <template v-if="getchuChildFor(row.anime_id)">
            <el-tag v-if="getchuChildFor(row.anime_id).status === 'completed'" type="success">已完成</el-tag>
            <el-tag v-else-if="getchuChildFor(row.anime_id).status === 'failed'" type="danger">写入失败</el-tag>
            <el-tag v-else type="warning">{{ getchuChildFor(row.anime_id).status === 'running' ? '写入中' : '已排队' }}</el-tag>
            <el-tooltip v-if="getchuChildFor(row.anime_id).error" :content="getchuChildFor(row.anime_id).error.message">
              <div class="getchu-error">查看错误</div>
            </el-tooltip>
          </template>
          <span v-else class="muted">未确认</span>
        </template>
      </el-table-column>
      <el-table-column label="操作" width="145" fixed="right">
        <template #default="{ row }">
          <el-button
            type="primary"
            size="small"
            :loading="getchuConfirming[row.anime_id]"
            :disabled="row.search_status !== 'ready' || !getchuSelections[row.anime_id] || Boolean(row.blockers.length) || ['pending', 'running', 'completed'].includes(getchuChildFor(row.anime_id)?.status)"
            @click="confirmGetchuRow(row)"
          >
            确认并写入
          </el-button>
        </template>
      </el-table-column>
    </el-table>
    <template #footer>
      <span class="muted">确认后会备份已有 NFO，并只更新其 plot 节点</span>
      <el-button :loading="getchuClosing" @click="requestGetchuClose">关闭</el-button>
      <el-button
        v-if="['completed', 'failed'].includes(getchuSearchStatus)"
        @click="getchuPreviewTaskId = null; startGetchuPreview(true)"
      >重新预览当前筛选</el-button>
    </template>
  </el-dialog>

  <el-dialog v-model="detailOpen" width="min(820px, 94vw)" :title="selected?.title">
    <template v-if="selected">
      <div class="anime-detail-head">
        <div class="anime-detail-cover">
          <img
            v-if="coverUrl(selected) && !coverErrors[selected.id]"
            :src="coverUrl(selected) || ''"
            :alt="`${selected.title} 封面`"
            referrerpolicy="no-referrer"
            @error="markCoverError(selected)"
          >
          <span v-else>NO COVER</span>
        </div>
        <div>
          <p class="muted">{{ selected.description || '暂无简介' }}</p>
          <el-button
            v-if="(selected.description || '').trim()"
            size="small"
            :loading="busy"
            @click="translateDescription"
          >
            翻译简介
          </el-button>
          <el-button
            v-else
            size="small"
            :loading="descriptionSearching"
            @click="openDescriptionSearch"
          >
            从其他来源获取简介
          </el-button>
        </div>
      </div>
      <el-descriptions :column="2" border>
        <el-descriptions-item label="原始标题">{{ selected.original_title || '-' }}</el-descriptions-item>
        <el-descriptions-item label="年份">{{ selected.year || '-' }}</el-descriptions-item>
        <el-descriptions-item label="类型">{{ selected.media_type || '-' }}</el-descriptions-item>
        <el-descriptions-item label="集数">{{ selected.episode_count || '-' }}</el-descriptions-item>
        <el-descriptions-item label="制作公司">{{ selected.studio || '-' }}</el-descriptions-item>
        <el-descriptions-item label="来源">{{ selected.mappings.map(item => `${item.source}:${item.source_id}`).join(' · ') }}</el-descriptions-item>
      </el-descriptions>
      <h4>媒体文件</h4>
      <el-table :data="selected.files" size="small">
        <el-table-column prop="relative_path" label="文件" min-width="280" show-overflow-tooltip />
        <el-table-column label="集号" width="130">
          <template #default="{ row }">
            <el-input
              v-model="row.episode"
              maxlength="16"
              clearable
              size="small"
              placeholder="如 1、S1"
              @change="saveEpisode(row)"
            />
          </template>
        </el-table-column>
        <el-table-column label="集标题" min-width="190" show-overflow-tooltip>
          <template #default="{ row }">{{ selected.episode_titles[String(row.episode)] || '-' }}</template>
        </el-table-column>
        <el-table-column prop="status" label="状态" width="90" />
        <el-table-column label="操作" width="150" fixed="right">
          <template #default="{ row }">
            <el-button size="small" @click="playMedia(row)">播放</el-button>
            <el-button
              size="small"
              type="danger"
              plain
              :loading="removingMediaId === row.id"
              :disabled="removingMediaId !== null && removingMediaId !== row.id"
              @click="removeMedia(row)"
            >
              删除
            </el-button>
          </template>
        </el-table-column>
      </el-table>
    </template>
    <template #footer>
      <el-button :loading="busy" @click="refresh">刷新元数据</el-button>
      <el-button :loading="busy" @click="previewRename">批量重命名</el-button>
      <el-button type="primary" @click="showPreview">预览导出</el-button>
    </template>
  </el-dialog>

  <el-dialog
    v-model="descriptionSearchOpen"
    width="min(760px, 94vw)"
    title="从其他来源获取简介"
    append-to-body
  >
    <div class="toolbar catalog-search">
      <el-input
        v-model="descriptionSearchKeyword"
        placeholder="输入作品标题"
        clearable
        @keyup.enter="searchDescriptionCandidates"
      />
      <el-button type="primary" plain :loading="descriptionSearching" @click="searchDescriptionCandidates">
        搜索
      </el-button>
    </div>
    <el-alert
      v-for="error in descriptionSearchErrors"
      :key="error.source"
      type="warning"
      :closable="false"
      :title="`${error.source}: ${error.message}`"
      style="margin-bottom: 10px"
    />
    <div v-loading="descriptionSearching" class="source-section">
      <label
        v-for="item in descriptionCandidates"
        :key="`${item.source}:${item.source_id}`"
        class="candidate"
        :class="{ selected: descriptionSelection === `${item.source}:${item.source_id}` }"
      >
        <input
          v-model="descriptionSelection"
          type="radio"
          name="description-candidate"
          :value="`${item.source}:${item.source_id}`"
        >
        <span>
          <b>{{ item.title }}</b>
          <small class="muted"> · {{ item.source }}:{{ item.source_id }}</small>
        </span>
        <span class="muted">{{ item.year || '-' }} · 匹配 {{ Math.round(item.score * 100) }}%</span>
      </label>
      <div v-if="!descriptionSearching && !descriptionCandidates.length" class="empty">
        其他已启用来源没有返回候选；可修改关键词后重试
      </div>
    </div>
    <template #footer>
      <el-button @click="descriptionSearchOpen = false">取消</el-button>
      <el-button
        type="primary"
        :disabled="!descriptionSelection"
        :loading="descriptionFilling"
        @click="fillDescription"
      >
        使用所选简介
      </el-button>
    </template>
  </el-dialog>

  <el-dialog
    v-model="playerOpen"
    width="min(1000px, 96vw)"
    :title="playerFile?.relative_path || '播放视频'"
    destroy-on-close
    append-to-body
    @closed="closePlayer"
  >
    <video v-if="playerFile" class="media-player" :src="playerUrl" controls autoplay preload="metadata">
      当前浏览器不支持播放该视频格式。
    </video>
  </el-dialog>

  <el-dialog v-model="renameOpen" width="min(980px, 96vw)" title="批量移动与重命名预览">
    <div class="toolbar">
      <span>季度</span>
      <el-input-number v-model="renameSeason" :min="0" :max="99" @change="previewRename" />
      <span class="muted">目标目录：{{ renamePreview?.target_dir }}</span>
      <span class="muted">将先生成 {{ renamePreview?.nfo_create_count || 0 }} 个缺失 NFO</span>
    </div>
    <el-alert v-if="renamePreview?.blockers?.length" type="error" :closable="false" title="存在阻塞项">
      <div v-for="item in renamePreview.blockers" :key="item">{{ item }}</div>
    </el-alert>
    <el-table :data="renamePreview?.files || []" size="small">
      <el-table-column label="类型" width="90">
        <template #default="{ row }">
          {{ row.generated ? `待生成${renameKindLabels[row.kind] || row.kind}` : (renameKindLabels[row.kind] || row.kind) }}
        </template>
      </el-table-column>
      <el-table-column prop="episode" label="集" width="70" />
      <el-table-column prop="episode_title" label="集标题" min-width="190" show-overflow-tooltip />
      <el-table-column prop="source" label="当前路径" min-width="300" show-overflow-tooltip />
      <el-table-column prop="target" label="目标路径" min-width="340" show-overflow-tooltip />
    </el-table>
    <el-alert
      v-if="renamePreview?.preserved_dirs?.length"
      class="cleanup-preview"
      type="info"
      :closable="false"
      title="检测到共享目录：仅移动计划内文件，原目录及其他文件会保留"
    >
      <div v-for="item in renamePreview.preserved_dirs" :key="item.source">
        {{ item.source }}（{{ item.reason }}）
      </div>
    </el-alert>
    <el-alert
      v-if="renamePreview?.cleanup_dirs?.length"
      class="cleanup-preview"
      type="warning"
      :closable="false"
      title="文件处理成功后，下列旧文件夹将移入隐藏目录 .delete"
    >
      <div v-for="item in renamePreview.cleanup_dirs" :key="item.source">
        {{ item.source }} → {{ item.target }}
      </div>
    </el-alert>
    <template #footer>
      <el-button @click="renameOpen = false">取消</el-button>
      <el-button type="primary" :disabled="Boolean(renamePreview?.blockers?.length)" :loading="busy" @click="renameFiles">确认移动并重命名</el-button>
    </template>
  </el-dialog>

  <el-dialog v-model="bulkRenameOpen" width="min(1100px, 96vw)" title="目录名不一致作品批量重命名预览">
    <div v-if="bulkRenameExecuting || bulkRenameTaskText" class="bulk-match-progress">
      <el-progress
        :percentage="bulkRenameProgress"
        :status="bulkRenameExecuting ? undefined : 'success'"
      />
      <span class="muted">{{ bulkRenameTaskText }}</span>
    </div>
    <div class="toolbar">
      <span>季度</span>
      <el-input-number v-model="bulkRenameSeason" :min="0" :max="99" @change="previewBulkRename" />
      <span class="muted">
        {{ bulkRenamePreview?.anime_count || 0 }} 部目录名不一致作品 ·
        先生成 {{ bulkRenamePreview?.nfo_create_count || 0 }} 个缺失 NFO ·
        {{ bulkRenamePreview?.changed_count || 0 }} 个文件需要处理 ·
        {{ bulkRenamePreview?.cleanup_count || 0 }} 个旧文件夹需要归档 ·
        {{ bulkRenamePreview?.skipped?.length || 0 }} 部跳过
      </span>
    </div>
    <el-alert v-if="bulkRenamePreview?.blockers?.length" type="error" :closable="false" title="存在阻塞项">
      <div v-for="item in bulkRenamePreview.blockers" :key="item">{{ item }}</div>
    </el-alert>
    <el-alert
      v-else-if="!bulkRenamePreview?.changed_count && !bulkRenamePreview?.cleanup_count"
      type="success"
      :closable="false"
      title="所有文件已经符合命名规则，无需处理"
    />
    <el-table :data="bulkChangedFiles" size="small" max-height="560">
      <el-table-column prop="anime_title" label="作品" min-width="190" show-overflow-tooltip />
      <el-table-column label="类型" width="90">
        <template #default="{ row }">
          {{ row.generated ? `待生成${renameKindLabels[row.kind] || row.kind}` : (renameKindLabels[row.kind] || row.kind) }}
        </template>
      </el-table-column>
      <el-table-column prop="episode" label="集" width="70" />
      <el-table-column prop="episode_title" label="集标题" min-width="190" show-overflow-tooltip />
      <el-table-column prop="source" label="当前路径" min-width="300" show-overflow-tooltip />
      <el-table-column prop="target" label="目标路径" min-width="340" show-overflow-tooltip />
    </el-table>
    <el-alert
      v-if="bulkRenamePreview?.preserved_dirs?.length"
      class="cleanup-preview"
      type="info"
      :closable="false"
      title="检测到共享目录：仅移动计划内文件，原目录及其他文件会保留"
    >
      <div v-for="item in bulkRenamePreview.preserved_dirs" :key="item.source">
        {{ item.source }}（{{ item.reason }}）
      </div>
    </el-alert>
    <el-alert
      v-if="bulkRenamePreview?.cleanup_dirs?.length"
      class="cleanup-preview"
      type="warning"
      :closable="false"
      title="文件处理成功后，旧文件夹将移入隐藏目录 .delete"
    >
      <div v-for="item in bulkRenamePreview.cleanup_dirs" :key="item.source">
        {{ item.source }} → {{ item.target }}
      </div>
    </el-alert>
    <template #footer>
      <el-button @click="bulkRenameOpen = false">取消</el-button>
      <el-button
        type="primary"
        :disabled="bulkRenameRunning || !bulkRenamePreviewTaskId || Boolean(bulkRenamePreview?.blockers?.length) || (!bulkRenamePreview?.changed_count && !bulkRenamePreview?.cleanup_count)"
        :loading="bulkRenameExecuting"
        @click="renameAllFiles"
      >
        确认全部移动并重命名
      </el-button>
    </template>
  </el-dialog>

  <el-dialog v-model="bulkArtifactOpen" width="min(1050px, 96vw)" title="批量写入 NFO、主图和剧集图片预览">
    <div class="toolbar">
      <span class="muted">
        {{ bulkArtifactPreview?.anime_count || 0 }} 部作品 ·
        {{ bulkArtifactPreview?.nfo_count || 0 }} 个 NFO ·
        {{ bulkArtifactPreview?.poster_count || 0 }} 张作品主图 ·
        {{ bulkArtifactPreview?.episode_image_count || 0 }} 张剧集图片 ·
        {{ bulkArtifactPreview?.skipped?.length || 0 }} 个跳过项
      </span>
    </div>
    <el-alert
      v-if="bulkArtifactPreview?.blockers?.length"
      type="error"
      :closable="false"
      title="存在阻塞项"
    >
      <div v-for="item in bulkArtifactPreview.blockers" :key="item">{{ item }}</div>
    </el-alert>
    <el-alert
      v-else-if="!bulkArtifactPreview?.files?.length"
      type="success"
      :closable="false"
      title="NFO、主图和剧集图片均已齐全，无需写入"
    />
    <el-alert
      v-if="bulkArtifactPreview?.auto_translate_description && bulkArtifactPreview?.translation_candidate_count"
      type="info"
      :closable="false"
      :title="`执行时将先自动翻译 ${bulkArtifactPreview.translation_candidate_count} 部作品简介，再重新生成 NFO 内容`"
    />
    <div v-if="bulkArtifactRunning || bulkArtifactTaskText" class="bulk-match-progress">
      <el-progress
        :percentage="bulkArtifactProgress"
        :status="bulkArtifactProgress === 100 ? 'success' : undefined"
      />
      <span class="muted">{{ bulkArtifactTaskText }}</span>
    </div>
    <el-table :data="bulkArtifactPreview?.files || []" size="small" max-height="560">
      <el-table-column prop="anime_title" label="作品" min-width="220" show-overflow-tooltip />
      <el-table-column label="类型" width="110">
        <template #default="{ row }">{{ artifactKindLabels[row.kind] || row.kind }}</template>
      </el-table-column>
      <el-table-column prop="path" label="写入路径" min-width="430" show-overflow-tooltip />
    </el-table>
    <el-alert
      v-if="bulkArtifactPreview?.skipped?.length"
      class="cleanup-preview"
      type="warning"
      :closable="false"
      title="以下项目将跳过"
    >
      <div v-for="(item, index) in bulkArtifactPreview.skipped" :key="`${item.anime_id}-${index}`">
        {{ item.title }}：{{ item.reason }}
      </div>
    </el-alert>
    <template #footer>
      <el-button :disabled="bulkArtifactRunning" @click="bulkArtifactOpen = false">取消</el-button>
      <el-button
        type="primary"
        :disabled="Boolean(bulkArtifactPreview?.blockers?.length) || !bulkArtifactPreview?.files?.length"
        :loading="bulkArtifactRunning"
        @click="writeBulkArtifacts"
      >
        确认补写
      </el-button>
    </template>
  </el-dialog>

  <el-dialog v-model="previewOpen" width="min(980px, 96vw)" title="NFO 与图片导出预览">
    <el-alert v-if="preview?.blockers?.length" type="error" :closable="false" title="存在阻塞项">
      <div v-for="item in preview.blockers" :key="item">{{ item }}</div>
    </el-alert>
    <template v-for="file in preview?.files || []" :key="file.path">
      <h4>{{ file.kind }} · {{ file.path }} <el-tag v-if="file.exists" type="warning" size="small">已存在</el-tag></h4>
      <pre class="code-block">{{ file.diff || file.content }}</pre>
    </template>
    <template #footer>
      <el-checkbox v-model="overwrite">备份并覆盖已有文件</el-checkbox>
      <el-button type="primary" :disabled="hasExportBlockers(preview?.blockers)" :loading="busy" @click="runExport">确认写入</el-button>
    </template>
  </el-dialog>
</template>
