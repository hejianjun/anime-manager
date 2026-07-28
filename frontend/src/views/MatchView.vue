<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'
import { ElMessage } from 'element-plus'
import { api, type Anime, type Candidate, type MatchGroup } from '../api'
import { groupCandidates } from '../utils'

const groups = ref<MatchGroup[]>([])
const active = ref<MatchGroup | null>(null)
const searching = ref(false)
const confirming = ref(false)
const keyword = ref('')
const selections = ref<Record<string, number | null>>({ anidb: null, dmm: null, getchu: null })
const sources = ['anidb', 'dmm', 'getchu']
const animeItems = ref<Anime[]>([])
const existingAnimeId = ref<number | null>(null)
const bySource = computed(() => groupCandidates(active.value?.candidates || [], sources))

async function loadGroups() {
  groups.value = (await api.get('/match-groups', { params: { status: 'pending', page_size: 100 } })).data.items
  if (!active.value && groups.value.length) selectGroup(groups.value[0])
}

async function loadAnime() {
  animeItems.value = (await api.get('/anime', { params: { page_size: 100 } })).data.items
}

function selectGroup(group: MatchGroup) {
  active.value = group
  keyword.value = group.search_keyword
  selections.value = { anidb: null, dmm: null, getchu: null }
  existingAnimeId.value = null
  group.candidates.filter(item => item.selected).forEach(item => selections.value[item.source] = item.id)
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
      sources,
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

onMounted(() => Promise.all([loadGroups(), loadAnime()]))
</script>

<template>
  <div class="split">
    <section class="panel">
      <div class="panel-title"><h2>待确认分组</h2><el-tag type="warning">{{ groups.length }}</el-tag></div>
      <div class="group-list">
        <article v-for="group in groups" :key="group.id" class="group-card" :class="{ active: active?.id === group.id }" @click="selectGroup(group)">
          <h3>{{ group.display_title }}</h3>
          <span class="muted">{{ group.files.length }} 个文件</span>
          <div v-for="file in group.files.slice(0, 3)" :key="file.id" class="file-pill">{{ file.relative_path }}</div>
        </article>
        <div v-if="!groups.length" class="empty">没有等待确认的作品</div>
      </div>
    </section>

    <section class="panel">
      <template v-if="active">
        <div class="panel-title">
          <div><p class="eyebrow">CANDIDATE REVIEW</p><h2>{{ active.display_title }}</h2></div>
        </div>
        <div class="toolbar">
          <el-input v-model="keyword" placeholder="搜索关键词" @keyup.enter="search" />
          <el-button type="primary" :loading="searching" @click="search">搜索全部来源</el-button>
        </div>
        <div v-for="source in sources" :key="source" class="source-section">
          <div class="source-head">{{ source }} <el-tag v-if="source !== 'anidb'" size="small" type="warning">模拟</el-tag></div>
          <label v-for="item in bySource[source] as Candidate[]" :key="item.id" class="candidate" :class="{ selected: selections[source] === item.id }">
            <input v-model="selections[source]" type="radio" :name="`candidate-${source}`" :value="item.id" :aria-label="`${item.title} · ID ${item.source_id}`" />
            <span><b>{{ item.title }}</b><small class="muted"> · ID {{ item.source_id }}</small></span>
            <span class="score">{{ Math.round(item.score * 100) }}%</span>
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
</template>
