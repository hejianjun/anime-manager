<script setup lang="ts">
import { onMounted, ref } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import { api, type Anime } from '../api'
import { hasExportBlockers } from '../utils'

const items = ref<Anime[]>([])
const selected = ref<Anime | null>(null)
const detailOpen = ref(false)
const previewOpen = ref(false)
const preview = ref<any>(null)
const overwrite = ref(false)
const busy = ref(false)
const renameOpen = ref(false)
const renamePreview = ref<any>(null)
const renameSeason = ref(1)
const coverErrors = ref<Record<number, boolean>>({})

function markCoverError(animeId: number) {
  coverErrors.value[animeId] = true
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
  await ElMessageBox.confirm('视频将移动到作品目录并重命名，不会覆盖现有文件。确认继续？', '批量重命名确认', { type: 'warning' })
  busy.value = true
  try {
    const result = (await api.post(`/anime/${selected.value.id}/rename`, { season: renameSeason.value })).data
    ElMessage.success(`已移动并重命名 ${result.moved.length} 个视频`)
    renameOpen.value = false
    selected.value = (await api.get(`/anime/${selected.value.id}`)).data
    await load()
  } catch (error) { ElMessage.error((error as Error).message) }
  finally { busy.value = false }
}

onMounted(load)
</script>

<template>
  <section class="panel">
    <div class="panel-title"><div><p class="eyebrow">CATALOG</p><h2>已绑定作品</h2></div><span class="muted">{{ items.length }} 部</span></div>
    <div class="anime-grid">
      <article v-for="item in items" :key="item.id" class="anime-card" @click="show(item)">
        <div class="anime-cover">
          <img
            v-if="item.cover_url && !coverErrors[item.id]"
            :src="item.cover_url"
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
            <el-tag v-for="mapping in item.mappings" :key="mapping.source" :type="mapping.is_mock ? 'warning' : 'success'">{{ mapping.source }}</el-tag>
          </div>
        </div>
      </article>
      <div v-if="!items.length" class="empty">确认候选后，作品会显示在这里</div>
    </div>
  </section>

  <el-dialog v-model="detailOpen" width="min(820px, 94vw)" :title="selected?.title">
    <template v-if="selected">
      <div class="anime-detail-head">
        <div class="anime-detail-cover">
          <img
            v-if="selected.cover_url && !coverErrors[selected.id]"
            :src="selected.cover_url"
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
        <el-table-column prop="relative_path" label="文件" min-width="330" />
        <el-table-column label="集号" width="130">
          <template #default="{ row }"><el-input-number v-model="row.episode" :min="0" :max="9999" size="small" controls-position="right" @change="saveEpisode(row)" /></template>
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
      <el-table-column prop="episode" label="集" width="70" />
      <el-table-column prop="source" label="当前路径" min-width="300" show-overflow-tooltip />
      <el-table-column prop="target" label="目标路径" min-width="340" show-overflow-tooltip />
    </el-table>
    <template #footer>
      <el-button @click="renameOpen = false">取消</el-button>
      <el-button type="primary" :disabled="Boolean(renamePreview?.blockers?.length)" :loading="busy" @click="renameFiles">确认移动并重命名</el-button>
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
