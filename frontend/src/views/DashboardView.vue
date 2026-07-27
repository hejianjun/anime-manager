<script setup lang="ts">
import { onMounted, ref } from 'vue'
import { useRouter } from 'vue-router'
import { api } from '../api'

const router = useRouter()
const stats = ref({ files: 0, anime: 0, pending: 0, missing: 0, running_tasks: 0 })

onMounted(async () => {
  stats.value = (await api.get('/dashboard')).data
})
</script>

<template>
  <section class="stat-grid">
    <div class="stat"><span>媒体文件</span><strong>{{ stats.files }}</strong></div>
    <div class="stat"><span>已绑定作品</span><strong>{{ stats.anime }}</strong></div>
    <div class="stat warn"><span>待人工确认</span><strong>{{ stats.pending }}</strong></div>
    <div class="stat warn"><span>缺失文件</span><strong>{{ stats.missing }}</strong></div>
    <div class="stat"><span>运行中任务</span><strong>{{ stats.running_tasks }}</strong></div>
  </section>

  <section class="action-strip">
    <div class="panel">
      <div class="panel-title">
        <div><p class="eyebrow">WORKFLOW</p><h2>从文件到媒体库</h2></div>
      </div>
      <div class="workflow">
        <div><b>01</b><span>扫描文件</span></div>
        <div><b>02</b><span>检索候选</span></div>
        <div><b>03</b><span>人工确认</span></div>
        <div><b>04</b><span>预览导出</span></div>
      </div>
    </div>
    <div class="panel">
      <div class="panel-title"><h2>下一步</h2></div>
      <p class="muted">添加媒体目录并开始扫描。系统不会移动或删除你的原始视频。</p>
      <el-button type="primary" @click="router.push('/library')">管理媒体库</el-button>
      <el-button @click="router.push('/match')">查看待确认</el-button>
    </div>
  </section>
</template>

