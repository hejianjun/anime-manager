import { createRouter, createWebHistory } from 'vue-router'
import DashboardView from './views/DashboardView.vue'
import LibraryView from './views/LibraryView.vue'
import MatchView from './views/MatchView.vue'
import AnimeView from './views/AnimeView.vue'
import SettingsView from './views/SettingsView.vue'

export default createRouter({
  history: createWebHistory(),
  routes: [
    { path: '/', component: DashboardView, meta: { title: '概览' } },
    { path: '/library', component: LibraryView, meta: { title: '媒体库' } },
    { path: '/match', component: MatchView, meta: { title: '待确认' } },
    { path: '/anime', component: AnimeView, meta: { title: '作品' } },
    { path: '/settings', component: SettingsView, meta: { title: '设置' } },
  ],
})

