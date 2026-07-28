<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import { api, type Anime } from '../api'
import { getEpisodeHealth, hasExportBlockers, missingEpisodeText } from '../utils'

const items = ref<Anime[]>([])
type IssueFilter = 'missing' | 'unfilled' | 'directory' | 'nfo' | 'episodeImage'

const issueFilters = ref<IssueFilter[]>([])
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
const coverErrors = ref<Record<number, boolean>>({})

const itemHealth = computed(() =>
  Object.fromEntries(items.value.map(item => [item.id, getEpisodeHealth(item)])),
)
const filteredItems = computed(() => {
  if (!issueFilters.value.length) return items.value
  return items.value.filter((item) => {
    const health = itemHealth.value[item.id]
    return issueFilters.value.some((filter) => {
      if (filter === 'missing') return health.missingEpisodes.length > 0
      if (filter === 'unfilled') return health.unfilledCount > 0
      if (filter === 'directory') return item.catalog_health.directory_name_mismatch
      if (filter === 'nfo') return item.catalog_health.missing_nfo_count > 0
      return item.catalog_health.missing_episode_image_count > 0
    })
  })
})
const missingAnimeCount = computed(() =>
  items.value.filter(item => itemHealth.value[item.id].missingEpisodes.length > 0).length,
)
const unfilledAnimeCount = computed(() =>
  items.value.filter(item => itemHealth.value[item.id].unfilledCount > 0).length,
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
const renameKindLabels: Record<string, string> = {
  video: '视频',
  nfo: 'NFO',
  subtitle: '字幕',
  image: '图片',
}

function markCoverError(animeId: number) {
  coverErrors.value[animeId] = true
}

function coverUrl(anime: Anime) {
  const getchu = anime.mappings.find(item => item.source === 'getchu' && !item.is_mock)
  return getchu
    ? `/api/sources/getchu/${encodeURIComponent(getchu.source_id)}/cover`
    : anime.cover_url
}

async function load() {
  items.value = (await api.get('/anime', { params: { page_size: 100 } })).data.items
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
    await api.patch(`/media-files/${file.id}`, { episode: file.episode })
    ElMessage.success('集号已保存')
  } catch (error) { ElMessage.error((error as Error).message) }
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
  await ElMessageBox.confirm('媒体文件将移动到作品目录，原作品文件夹随后移入隐藏目录“.delete”，不会覆盖现有文件。确认继续？', '批量重命名确认', { type: 'warning' })
  busy.value = true
  try {
    const result = (await api.post(`/anime/${selected.value.id}/rename`, { season: renameSeason.value })).data
    ElMessage.success(`已处理 ${result.moved.length} 个文件，归档 ${result.archived_dirs.length} 个旧文件夹`)
    renameOpen.value = false
    selected.value = (await api.get(`/anime/${selected.value.id}`)).data
    await load()
  } catch (error) { ElMessage.error((error as Error).message) }
  finally { busy.value = false }
}

async function previewBulkRename() {
  busy.value = true
  try {
    bulkRenamePreview.value = (await api.post('/anime/rename-preview', { season: bulkRenameSeason.value })).data
    bulkRenameOpen.value = true
  } catch (error) { ElMessage.error((error as Error).message) }
  finally { busy.value = false }
}

async function renameAllFiles() {
  if (
    bulkRenamePreview.value?.blockers?.length
    || (!bulkRenamePreview.value?.changed_count && !bulkRenamePreview.value?.cleanup_count)
  ) return
  await ElMessageBox.confirm(
    `将处理 ${bulkRenamePreview.value.changed_count} 个媒体文件，并把 ${bulkRenamePreview.value.cleanup_count} 个旧文件夹移入隐藏目录“.delete”。确认继续？`,
    '全部作品批量重命名确认',
    { type: 'warning' },
  )
  busy.value = true
  try {
    const result = (await api.post('/anime/rename', { season: bulkRenameSeason.value })).data
    ElMessage.success(`已处理 ${result.anime_count} 部作品、${result.moved.length} 个文件，归档 ${result.archived_dirs.length} 个旧文件夹`)
    bulkRenameOpen.value = false
    await load()
  } catch (error) { ElMessage.error((error as Error).message) }
  finally { busy.value = false }
}

onMounted(load)
</script>

<template>
  <section class="panel">
    <div class="panel-title">
      <div><p class="eyebrow">CATALOG</p><h2>已绑定作品</h2></div>
      <div class="panel-actions">
        <span class="muted">{{ filteredItems.length }} / {{ items.length }} 部</span>
        <el-button :loading="busy" @click="previewBulkRename">全部批量重命名</el-button>
      </div>
    </div>
    <div class="catalog-filter">
      <span class="muted">仅显示</span>
      <el-checkbox-group v-model="issueFilters">
        <el-checkbox-button value="missing">缺集 {{ missingAnimeCount }}</el-checkbox-button>
        <el-checkbox-button value="unfilled">集数未填写 {{ unfilledAnimeCount }}</el-checkbox-button>
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
            v-if="item.cover_url && !coverErrors[item.id]"
            :src="coverUrl(item) || ''"
            :alt="`${item.title} 封面`"
            loading="lazy"
            referrerpolicy="no-referrer"
            @error="markCoverError(item.id)"
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
      <div v-else-if="!filteredItems.length" class="empty">没有符合当前条件的作品</div>
    </div>
  </section>

  <el-dialog v-model="detailOpen" width="min(820px, 94vw)" :title="selected?.title">
    <template v-if="selected">
      <div class="anime-detail-head">
        <div class="anime-detail-cover">
          <img
            v-if="selected.cover_url && !coverErrors[selected.id]"
            :src="coverUrl(selected) || ''"
            :alt="`${selected.title} 封面`"
            referrerpolicy="no-referrer"
            @error="markCoverError(selected.id)"
          >
          <span v-else>NO COVER</span>
        </div>
        <p class="muted">{{ selected.description || '暂无简介' }}</p>
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
          <template #default="{ row }"><el-input-number v-model="row.episode" :min="0" :max="9999" size="small" controls-position="right" @change="saveEpisode(row)" /></template>
        </el-table-column>
        <el-table-column label="集标题" min-width="190" show-overflow-tooltip>
          <template #default="{ row }">{{ selected.episode_titles[String(row.episode)] || '-' }}</template>
        </el-table-column>
        <el-table-column prop="status" label="状态" width="90" />
      </el-table>
    </template>
    <template #footer>
      <el-button :loading="busy" @click="refresh">刷新元数据</el-button>
      <el-button :loading="busy" @click="previewRename">批量重命名</el-button>
      <el-button type="primary" @click="showPreview">预览导出</el-button>
    </template>
  </el-dialog>

  <el-dialog v-model="renameOpen" width="min(980px, 96vw)" title="批量移动与重命名预览">
    <div class="toolbar">
      <span>季度</span>
      <el-input-number v-model="renameSeason" :min="0" :max="99" @change="previewRename" />
      <span class="muted">目标目录：{{ renamePreview?.target_dir }}</span>
    </div>
    <el-alert v-if="renamePreview?.blockers?.length" type="error" :closable="false" title="存在阻塞项">
      <div v-for="item in renamePreview.blockers" :key="item">{{ item }}</div>
    </el-alert>
    <el-table :data="renamePreview?.files || []" size="small">
      <el-table-column label="类型" width="90">
        <template #default="{ row }">{{ renameKindLabels[row.kind] || row.kind }}</template>
      </el-table-column>
      <el-table-column prop="episode" label="集" width="70" />
      <el-table-column prop="episode_title" label="集标题" min-width="190" show-overflow-tooltip />
      <el-table-column prop="source" label="当前路径" min-width="300" show-overflow-tooltip />
      <el-table-column prop="target" label="目标路径" min-width="340" show-overflow-tooltip />
    </el-table>
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

  <el-dialog v-model="bulkRenameOpen" width="min(1100px, 96vw)" title="全部已匹配作品批量重命名预览">
    <div class="toolbar">
      <span>季度</span>
      <el-input-number v-model="bulkRenameSeason" :min="0" :max="99" @change="previewBulkRename" />
      <span class="muted">
        {{ bulkRenamePreview?.anime_count || 0 }} 部作品 ·
        {{ bulkRenamePreview?.changed_count || 0 }} 个文件需要处理 ·
        {{ bulkRenamePreview?.cleanup_count || 0 }} 个旧文件夹需要归档 ·
        {{ bulkRenamePreview?.skipped?.length || 0 }} 部无可用文件
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
        <template #default="{ row }">{{ renameKindLabels[row.kind] || row.kind }}</template>
      </el-table-column>
      <el-table-column prop="episode" label="集" width="70" />
      <el-table-column prop="episode_title" label="集标题" min-width="190" show-overflow-tooltip />
      <el-table-column prop="source" label="当前路径" min-width="300" show-overflow-tooltip />
      <el-table-column prop="target" label="目标路径" min-width="340" show-overflow-tooltip />
    </el-table>
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
        :disabled="Boolean(bulkRenamePreview?.blockers?.length) || (!bulkRenamePreview?.changed_count && !bulkRenamePreview?.cleanup_count)"
        :loading="busy"
        @click="renameAllFiles"
      >
        确认全部移动并重命名
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
