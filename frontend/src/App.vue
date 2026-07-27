<script setup lang="ts">
import { computed } from 'vue'
import { useRoute } from 'vue-router'
import {
  Collection,
  Compass,
  DataAnalysis,
  Files,
  Setting,
} from '@element-plus/icons-vue'

const route = useRoute()
const title = computed(() => route.meta.title || 'Anime Manager')
const nav = [
  { path: '/', label: '概览', icon: DataAnalysis },
  { path: '/library', label: '媒体库', icon: Files },
  { path: '/match', label: '待确认', icon: Compass },
  { path: '/anime', label: '作品', icon: Collection },
  { path: '/settings', label: '设置', icon: Setting },
]
</script>

<template>
  <div class="app-shell">
    <aside class="sidebar">
      <router-link to="/" class="brand">
        <span class="brand-mark">AM</span>
        <span><b>Anime</b><small>Manager</small></span>
      </router-link>
      <nav class="nav">
        <router-link v-for="item in nav" :key="item.path" :to="item.path" class="nav-item">
          <el-icon><component :is="item.icon" /></el-icon>
          <span>{{ item.label }}</span>
        </router-link>
      </nav>
      <div class="sidebar-foot">
        <span class="status-dot" />
        本地服务
      </div>
    </aside>
    <main class="main">
      <header class="topbar">
        <div>
          <p class="eyebrow">LOCAL MEDIA WORKSPACE</p>
          <h1>{{ title }}</h1>
        </div>
        <div class="topbar-note">扫描 · 匹配 · 确认 · 导出</div>
      </header>
      <div class="page-wrap"><router-view /></div>
    </main>
  </div>
</template>

