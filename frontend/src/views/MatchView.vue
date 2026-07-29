<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, ref } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import { api, getAllAnime, type Anime, type Candidate, type MatchGroup, type MediaFile } from '../api'
import { groupCandidates, matchesMatchGroupSearch } from '../utils'

const groups = ref<MatchGroup[]>([])
const active = ref<MatchGroup | null>(null)
const searching = ref(false)
const bulkSearching = ref(false)
const bulkProgress = ref(0)
const bulkTaskText = ref('')
const confirming = ref(false)
const keyword = ref('')
const groupSearchInput = ref('')
const groupSearchKeyword = ref('')
const selections = ref<Record<string, number | null>>({ anidb: null, dmm: null, getchu: null })
const sources = ref<string[]>([])
const animeItems = ref<Anime[]>([])
const existingAnimeId = ref<number | null>(null)
const coverErrors = ref<Record<number, boolean>>({})
const playerOpen = ref(false)
const playerFile = ref<MediaFile | null>(null)
let bulkEvents: EventSource | null = null
const bySource = computed(() => groupCandidates(active.value?.candidates || [], sources.value))
const playerUrl = computed(() => playerFile.value ? `/api/media-files/${playerFile.value.id}/stream` : '')
const filteredGroups = computed(() =>
  groups.value.filter(group => matchesMatchGroupSearch(group, groupSearchKeyword.value)),
)

async function loadGroups() {
  const items: MatchGroup[] = (
    await api.get('/match-groups', { params: { status: 'pending', page_size: 100 } })
  ).data.items
  groups.value = items.filter(group => group.files.length > 0)
  if (active.value && !groups.value.some(group => group.id === active.value?.id)) {
    active.value = null
  }
  if (!active.value && groups.value.length) selectGroup(groups.value[0])
}

async function loadAnime() {
  animeItems.value = await getAllAnime()
}

async function loadSettings() {
  const settings = (await api.get('/settings')).data
  sources.value = settings.enabled_scrapers || ['anidb', 'dmm', 'getchu']
}

function selectGroup(group: MatchGroup) {
  active.value = group
  keyword.value = group.search_keyword
  selections.value = { anidb: null, dmm: null, getchu: null }
  existingAnimeId.value = null
  group.candidates.filter(item => item.selected).forEach(item => selections.value[item.source] = item.id)
}

function applyGroupSearch() {
  groupSearchKeyword.value = groupSearchInput.value.trim()
  if (active.value && filteredGroups.value.some(group => group.id === active.value?.id)) return
  active.value = null
  if (filteredGroups.value.length) selectGroup(filteredGroups.value[0])
}

function markCoverError(candidateId: number) {
  coverErrors.value[candidateId] = true
}

function candidateCoverUrl(item: Candidate) {
  return item.source === 'getchu'
    ? `/api/sources/getchu/${encodeURIComponent(item.source_id)}/cover`
    : item.cover_url
}

function playMedia(file: MediaFile) {
  playerFile.value = file
  playerOpen.value = true
}

function closePlayer() {
  playerFile.value = null
}

async function bindExisting() {
  if (!active.value || !existingAnimeId.value) return
  confirming.value = true
  try {
    const anime = (await api.post(`/match-groups/${active.value.id}/bind-existing`, { anime_id: existingAnimeId.value })).data
    ElMessage.success(`已添加到「${anime.title}」`)
    active.value = null
    await Promise.all([loadGroups(), loadAnime()])
  } catch (error) {
    ElMessage.error((error as Error).message)
  } finally { confirming.value = false }
}

async function search() {
  if (!active.value) return
  searching.value = true
  try {
    await api.patch(`/match-groups/${active.value.id}`, { search_keyword: keyword.value })
    const response = await api.post(`/match-groups/${active.value.id}/search`, {
      keyword: keyword.value,
      sources: sources.value,
    })
    active.value.candidates = response.data.items
    selections.value = { anidb: null, dmm: null, getchu: null }
    if (response.data.errors.length) {
      ElMessage.warning(response.data.errors.map((item: any) => `${item.source}: ${item.message}`).join('；'))
    } else ElMessage.success('候选已更新')
  } catch (error) {
    ElMessage.error((error as Error).message)
  } finally { searching.value = false }
}

function waitForBulkTask(taskId: number): Promise<any> {
  return new Promise((resolve, reject) => {
    const events = new EventSource(`/api/tasks/${taskId}/events`)
    bulkEvents = events
    events.onmessage = (event) => {
      try {
        const current = JSON.parse(event.data)
        bulkProgress.value = Math.round(current.progress * 100)
        bulkTaskText.value = current.message
        if (current.status === 'completed') {
          events.close()
          bulkEvents = null
          resolve(current)
        } else if (current.status === 'failed') {
          events.close()
          bulkEvents = null
          reject(new Error(current.error?.message || '批量匹配失败'))
        }
      } catch (error) {
        events.close()
        bulkEvents = null
        reject(error)
      }
    }
    events.onerror = () => {
      bulkTaskText.value = '实时进度连接中断，正在自动重连'
    }
  })
}

async function bulkSearchConfirm() {
  if (!groups.value.length) return
  try {
    await ElMessageBox.confirm(
      '将搜索全部待确认分组，并自动确认其中的 100% 匹配。其他分组会保留，确认继续？',
      '批量搜索确认',
      {
        type: 'warning',
        confirmButtonText: '开始批量确认',
        cancelButtonText: '取消',
      },
    )
  } catch {
    return
  }
  bulkSearching.value = true
  bulkProgress.value = 0
  bulkTaskText.value = '正在启动批量匹配'
  try {
    const task = (await api.post('/match-groups/bulk-search-confirm', {
      sources: sources.value,
    })).data
    const current = await waitForBulkTask(task.id)
    const result = current.result
    const summary = `已搜索 ${result.searched} 个，确认 ${result.confirmed} 个，保留 ${result.skipped} 个`
    if (result.failed) ElMessage.warning(`${summary}，失败 ${result.failed} 个`)
    else ElMessage.success(summary)
    active.value = null
    await Promise.all([loadGroups(), loadAnime()])
  } catch (error) {
    ElMessage.error((error as Error).message)
  } finally {
    bulkSearching.value = false
    if (bulkProgress.value < 100) bulkTaskText.value = ''
  }
}

async function confirm() {
  if (!active.value) return
  confirming.value = true
  try {
    await api.put(`/match-groups/${active.value.id}/selections`, { selections: selections.value })
    const anime = (await api.post(`/match-groups/${active.value.id}/confirm`)).data
    ElMessage.success(`已绑定到「${anime.title}」`)
    active.value = null
    await loadGroups()
  } catch (error) {
    ElMessage.error((error as Error).message)
  } finally { confirming.value = false }
}

onMounted(() => Promise.all([loadGroups(), loadAnime(), loadSettings()]))
onBeforeUnmount(() => bulkEvents?.close())
</script>

<template>
  <div class="split">
    <section class="panel">
      <div class="panel-title">
        <div>
          <h2>待确认分组</h2>
          <el-tag type="warning">
            {{ groupSearchKeyword ? `${filteredGroups.length} / ${groups.length}` : groups.length }}
          </el-tag>
        </div>
        <el-button
          size="small"
          type="primary"
          plain
          :loading="bulkSearching"
          :disabled="!groups.length || !sources.length"
          @click="bulkSearchConfirm"
        >
          批量确认 100% 匹配
        </el-button>
      </div>
      <div class="toolbar">
        <el-input
          v-model="groupSearchInput"
          clearable
          placeholder="搜索番名或文件名"
          @clear="applyGroupSearch"
          @keyup.enter="applyGroupSearch"
        />
        <el-button type="primary" plain @click="applyGroupSearch">搜索</el-button>
      </div>
      <div class="group-list">
        <div v-if="bulkSearching || bulkTaskText" class="bulk-match-progress">
          <el-progress
            :percentage="bulkProgress"
            :status="bulkProgress === 100 ? 'success' : undefined"
          />
          <span class="muted">{{ bulkTaskText }}</span>
        </div>
        <article v-for="group in filteredGroups" :key="group.id" class="group-card" :class="{ active: active?.id === group.id }" @click="selectGroup(group)">
          <h3>{{ group.display_title }}</h3>
          <span class="muted">{{ group.files.length }} 个文件</span>
          <div v-for="file in group.files.slice(0, 3)" :key="file.id" class="file-pill">{{ file.relative_path }}</div>
        </article>
        <div v-if="!groups.length" class="empty">没有等待确认的作品</div>
        <div v-else-if="!filteredGroups.length" class="empty">没有匹配该番名或文件名的作品</div>
      </div>
    </section>

    <section class="panel">
      <template v-if="active">
        <div class="panel-title">
          <div><p class="eyebrow">CANDIDATE REVIEW</p><h2>{{ active.display_title }}</h2></div>
        </div>
        <div class="toolbar">
          <el-input v-model="keyword" placeholder="搜索关键词" @keyup.enter="search" />
          <el-button type="primary" :loading="searching" :disabled="!sources.length" @click="search">搜索已启用来源</el-button>
        </div>
        <el-alert v-if="!sources.length" type="warning" :closable="false" title="当前未启用任何爬虫，请先到设置页选择。" />
        <div class="source-section">
          <div class="source-head">原始视频</div>
          <div v-for="file in active.files" :key="file.id" class="media-preview-row">
            <span :title="file.relative_path">{{ file.relative_path }}</span>
            <el-button size="small" @click="playMedia(file)">播放</el-button>
          </div>
        </div>
        <div v-for="source in sources" :key="source" class="source-section">
          <div class="source-head">
            {{ source }}
            <el-tag v-if="source === 'dmm'" size="small" type="success">API</el-tag>
            <el-tag v-if="source === 'getchu'" size="small" type="info">站内检索</el-tag>
          </div>
          <label v-for="item in bySource[source] as Candidate[]" :key="item.id" class="candidate" :class="{ selected: selections[source] === item.id }">
            <input v-model="selections[source]" type="radio" :name="`candidate-${source}`" :value="item.id" :aria-label="`${item.title} · ID ${item.source_id}`" />
            <span><b>{{ item.title }}</b><small class="muted"> · ID {{ item.source_id }}</small></span>
            <span class="score">{{ Math.round(item.score * 100) }}%</span>
            <div v-if="selections[source] === item.id" class="candidate-cover">
              <img
                v-if="item.cover_url && !coverErrors[item.id]"
                :src="candidateCoverUrl(item) || ''"
                :alt="`${item.title} 封面`"
                referrerpolicy="no-referrer"
                @error="markCoverError(item.id)"
              >
              <span v-else>暂无封面</span>
            </div>
          </label>
          <div v-if="!bySource[source].length" class="muted">尚无候选</div>
        </div>
        <div style="margin-top: 26px">
          <el-button type="primary" :loading="confirming" @click="confirm">确认并永久绑定</el-button>
          <span class="muted" style="margin-left:12px">每个来源最多选择一个</span>
        </div>
        <div class="existing-bind">
          <div><p class="eyebrow">EXISTING COLLECTION</p><b>添加到已绑定作品</b></div>
          <el-select v-model="existingAnimeId" filterable clearable placeholder="搜索已有作品" style="min-width: 280px">
            <el-option v-for="anime in animeItems" :key="anime.id" :label="anime.title" :value="anime.id" />
          </el-select>
          <el-button :disabled="!existingAnimeId" :loading="confirming" @click="bindExisting">直接加入</el-button>
        </div>
      </template>
      <div v-else class="empty">选择左侧分组开始匹配</div>
    </section>
  </div>

  <el-dialog
    v-model="playerOpen"
    width="min(1000px, 96vw)"
    :title="playerFile?.relative_path || '播放原始视频'"
    destroy-on-close
    @closed="closePlayer"
  >
    <video v-if="playerFile" class="media-player" :src="playerUrl" controls autoplay preload="metadata">
      当前浏览器不支持播放该视频格式。
    </video>
  </el-dialog>
</template>
