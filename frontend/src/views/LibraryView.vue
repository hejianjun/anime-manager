<script setup lang="ts">
import { onMounted, ref } from 'vue'
import { ElMessage } from 'element-plus'
import { api } from '../api'
import { taskProgressText } from '../utils'

interface Root { id: number; path: string; enabled: boolean; last_scan_at: string | null }
const roots = ref<Root[]>([])
const path = ref('')
const busy = ref(false)
const taskText = ref('')

async function load() {
  roots.value = (await api.get('/library-roots')).data
}

async function addRoot() {
  if (!path.value.trim()) return
  busy.value = true
  try {
    await api.post('/library-roots', { path: path.value.trim(), enabled: true })
    path.value = ''
    ElMessage.success('媒体目录已添加')
    await load()
  } catch (error) {
    ElMessage.error((error as Error).message)
  } finally { busy.value = false }
}

async function scan(root: Root) {
  busy.value = true
  try {
    const task = (await api.post(`/library-roots/${root.id}/scan`)).data
    taskText.value = '扫描任务已启动'
    for (;;) {
      const current = (await api.get(`/tasks/${task.id}`)).data
      taskText.value = taskProgressText(current.message, current.progress)
      if (current.status === 'completed') {
        ElMessage.success(`扫描完成：发现 ${current.result.found} 个媒体文件`)
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

onMounted(load)
</script>

<template>
  <section class="panel">
    <div class="panel-title">
      <div><p class="eyebrow">LIBRARY ROOTS</p><h2>媒体目录</h2></div>
      <span class="muted">{{ taskText }}</span>
    </div>
    <div class="toolbar">
      <el-input v-model="path" placeholder="例如 E:\Anime" style="max-width: 560px" @keyup.enter="addRoot" />
      <el-button type="primary" :loading="busy" @click="addRoot">添加目录</el-button>
    </div>
    <el-table :data="roots" empty-text="尚未添加媒体目录">
      <el-table-column prop="path" label="路径" min-width="380" />
      <el-table-column label="状态" width="100">
        <template #default="{ row }"><el-tag :type="row.enabled ? 'success' : 'info'">{{ row.enabled ? '启用' : '停用' }}</el-tag></template>
      </el-table-column>
      <el-table-column label="最后扫描" width="190">
        <template #default="{ row }">{{ row.last_scan_at ? new Date(row.last_scan_at).toLocaleString() : '从未扫描' }}</template>
      </el-table-column>
      <el-table-column width="120" align="right">
        <template #default="{ row }"><el-button :loading="busy" @click="scan(row)">立即扫描</el-button></template>
      </el-table-column>
    </el-table>
  </section>
</template>
