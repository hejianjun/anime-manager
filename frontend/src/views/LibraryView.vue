<script setup lang="ts">
import { onMounted, ref } from 'vue'
import { ElMessage } from 'element-plus'
import { api } from '../api'
import { taskProgressText } from '../utils'

interface Root {
  id: number
  path: string
  scan_path: string | null
  enabled: boolean
  last_scan_at: string | null
  scan_last_scan_at: string | null
}

const roots = ref<Root[]>([])
const mainPath = ref('')
const scanPath = ref('')
const busy = ref(false)
const taskText = ref('')
const editVisible = ref(false)
const editing = ref<Root | null>(null)
const editMainPath = ref('')
const editScanPath = ref('')

async function load() {
  roots.value = (await api.get('/library-roots')).data
}

async function addRoot() {
  if (!mainPath.value.trim()) return
  busy.value = true
  try {
    await api.post('/library-roots', {
      path: mainPath.value.trim(),
      scan_path: scanPath.value.trim() || null,
      enabled: true,
    })
    mainPath.value = ''
    scanPath.value = ''
    ElMessage.success('媒体库已添加')
    await load()
  } catch (error) {
    ElMessage.error((error as Error).message)
  } finally { busy.value = false }
}

async function scan(root: Root, source: 'main' | 'scan') {
  busy.value = true
  try {
    const task = (
      await api.post(`/library-roots/${root.id}/scan`, null, { params: { source } })
    ).data
    taskText.value = '扫描任务已启动'
    for (;;) {
      const current = (await api.get(`/tasks/${task.id}`)).data
      taskText.value = taskProgressText(current.message, current.progress)
      if (current.status === 'completed') {
        const label = source === 'main' ? '主目录' : '扫描目录'
        ElMessage.success(`${label}扫描完成：发现 ${current.result.found} 个媒体文件`)
        break
      }
      if (current.status === 'failed') throw new Error(current.error?.message || '扫描失败')
      await new Promise(resolve => setTimeout(resolve, 800))
    }
    await load()
  } catch (error) {
    ElMessage.error((error as Error).message)
  } finally { busy.value = false }
}

function openEdit(root: Root) {
  editing.value = root
  editMainPath.value = root.path
  editScanPath.value = root.scan_path || ''
  editVisible.value = true
}

async function saveEdit() {
  if (!editing.value || !editMainPath.value.trim()) return
  busy.value = true
  try {
    await api.patch(`/library-roots/${editing.value.id}`, {
      path: editMainPath.value.trim(),
      scan_path: editScanPath.value.trim() || null,
    })
    editVisible.value = false
    ElMessage.success('媒体库目录已更新')
    await load()
  } catch (error) {
    ElMessage.error((error as Error).message)
  } finally { busy.value = false }
}

onMounted(load)
</script>

<template>
  <section class="panel">
    <div class="panel-title">
      <div><p class="eyebrow">LIBRARY ROOTS</p><h2>媒体库目录</h2></div>
      <span class="muted">{{ taskText }}</span>
    </div>
    <div class="toolbar">
      <el-input v-model="mainPath" placeholder="主目录，例如 E:\Anime" style="max-width: 420px" />
      <el-input
        v-model="scanPath"
        placeholder="扫描目录（可选），例如 E:\Downloads"
        style="max-width: 420px"
        @keyup.enter="addRoot"
      />
      <el-button type="primary" :loading="busy" @click="addRoot">添加媒体库</el-button>
    </div>
    <el-table :data="roots" empty-text="尚未添加媒体库">
      <el-table-column prop="path" label="主目录" min-width="300" />
      <el-table-column label="扫描目录" min-width="300">
        <template #default="{ row }">{{ row.scan_path || '未设置' }}</template>
      </el-table-column>
      <el-table-column label="状态" width="100">
        <template #default="{ row }">
          <el-tag :type="row.enabled ? 'success' : 'info'">{{ row.enabled ? '启用' : '停用' }}</el-tag>
        </template>
      </el-table-column>
      <el-table-column label="扫描时间" width="230">
        <template #default="{ row }">
          <div>主：{{ row.last_scan_at ? new Date(row.last_scan_at).toLocaleString() : '从未扫描' }}</div>
          <div>扫描：{{ row.scan_last_scan_at ? new Date(row.scan_last_scan_at).toLocaleString() : '从未扫描' }}</div>
        </template>
      </el-table-column>
      <el-table-column width="310" align="right">
        <template #default="{ row }">
          <el-button :loading="busy" @click="scan(row, 'main')">扫描主目录</el-button>
          <el-button :loading="busy" :disabled="!row.scan_path" @click="scan(row, 'scan')">扫描目录</el-button>
          <el-button @click="openEdit(row)">设置</el-button>
        </template>
      </el-table-column>
    </el-table>

    <el-dialog v-model="editVisible" title="设置媒体库目录" width="620px">
      <el-form label-width="90px">
        <el-form-item label="主目录">
          <el-input v-model="editMainPath" />
        </el-form-item>
        <el-form-item label="扫描目录">
          <el-input v-model="editScanPath" placeholder="留空表示不使用独立扫描目录" />
        </el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="editVisible = false">取消</el-button>
        <el-button type="primary" :loading="busy" @click="saveEdit">保存</el-button>
      </template>
    </el-dialog>
  </section>
</template>
