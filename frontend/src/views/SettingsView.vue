<script setup lang="ts">
import { onMounted, reactive, ref } from 'vue'
import { ElMessage } from 'element-plus'
import { api } from '../api'

const form = reactive({
  anidb_client: '',
  anidb_clientver: 1,
  dmm_api_id: '',
  dmm_affiliate_id: '',
  proxy_url: '',
  request_interval_seconds: 2.1,
  scheduled_refresh: false,
})
const status = ref<any>({})
const busy = ref(false)

async function load() {
  const data = (await api.get('/settings')).data
  Object.assign(form, data)
  status.value = data
}

async function save() {
  busy.value = true
  try {
    Object.assign(form, (await api.patch('/settings', form)).data)
    ElMessage.success('设置已保存')
  } catch (error) { ElMessage.error((error as Error).message) }
  finally { busy.value = false }
}

async function refreshTitles() {
  busy.value = true
  try {
    const result = (await api.post('/sources/anidb/titles/refresh')).data
    ElMessage.success(result.refreshed ? `已导入 ${result.titles} 条标题` : '24 小时内已刷新，无需重复下载')
    await load()
  } catch (error) { ElMessage.error((error as Error).message) }
  finally { busy.value = false }
}

onMounted(load)
</script>

<template>
  <section class="panel">
    <div class="panel-title"><div><p class="eyebrow">SOURCE CONFIGURATION</p><h2>数据源与任务</h2></div></div>
    <div class="setting-grid">
      <el-form label-position="top">
        <el-form-item label="AniDB 注册 client">
          <el-input v-model="form.anidb_client" placeholder="必须是 AniDB 已注册的小写标识" />
        </el-form-item>
        <el-form-item label="AniDB clientver">
          <el-input-number v-model="form.anidb_clientver" :min="1" />
        </el-form-item>
        <el-form-item label="DMM API ID">
          <el-input v-model="form.dmm_api_id" type="password" show-password placeholder="DMM Web Service API ID" />
        </el-form-item>
        <el-form-item label="DMM API 专用 Affiliate ID">
          <el-input v-model="form.dmm_affiliate_id" placeholder="例如 example-990" />
        </el-form-item>
        <el-form-item label="代理 URL">
          <el-input v-model="form.proxy_url" placeholder="可选，例如 http://127.0.0.1:7890" />
        </el-form-item>
        <el-form-item label="请求间隔（秒）">
          <el-input-number v-model="form.request_interval_seconds" :min="2" :step="0.1" />
        </el-form-item>
        <el-form-item><el-switch v-model="form.scheduled_refresh" /> <span style="margin-left:10px">启用作品定期刷新</span></el-form-item>
        <el-button type="primary" :loading="busy" @click="save">保存设置</el-button>
      </el-form>
      <div>
        <h3>AniDB 标题库</h3>
        <p class="muted">搜索使用本地标题索引，不抓取 AniDB 网页。官方标题库每天最多下载一次。</p>
        <p>最后刷新：{{ status.anidb_titles_refreshed_at ? new Date(status.anidb_titles_refreshed_at).toLocaleString() : '尚未下载' }}</p>
        <el-button :loading="busy" @click="refreshTitles">刷新标题库</el-button>
        <el-alert style="margin-top:22px" type="info" :closable="false" title="client/clientver 仅用于确认后的详情请求；未配置时仍可搜索标题。" />
        <el-alert style="margin-top:12px" type="info" :closable="false" title="DMM 搜索需要 API ID 与 API 专用 Affiliate ID；Getchu 无需账号配置。" />
      </div>
    </div>
  </section>
</template>
