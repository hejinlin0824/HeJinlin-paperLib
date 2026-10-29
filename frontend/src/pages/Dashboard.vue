<script setup>
// 工作台：统计 + 最近在读 + 待解决问题
import { onMounted, ref } from 'vue'
import { useRouter } from 'vue-router'
import api from '../api'

const router = useRouter()
const data = ref(null)

const CARDS = [
  ['total', '论文总数', '📚'],
  ['done', '已精读完', '✅'],
  ['annotations', '批注笔记', '✍'],
  ['open_questions', '待解决问题', '❓'],
]

onMounted(async () => {
  data.value = await api.dashboard()
})
</script>

<template>
  <div v-if="data">
    <h1 class="page-title">工作台</h1>
    <p class="page-sub">今天也要读论文呀 ⛅</p>

    <div style="display:grid; grid-template-columns:repeat(4, 1fr); gap:14px; margin-bottom:24px;">
      <div v-for="[k, label, ico] in CARDS" :key="k" class="card" style="text-align:center; padding:18px 8px;">
        <div style="font-size:24px;">{{ ico }}</div>
        <div style="font-size:26px; font-weight:700; margin:4px 0;">{{ data.stats[k] }}</div>
        <div class="muted">{{ label }}</div>
      </div>
    </div>

    <div style="display:grid; grid-template-columns: 1.2fr 1fr; gap:16px;">
      <div class="card">
        <div style="display:flex; justify-content:space-between; align-items:center; margin-bottom:12px;">
          <b>最近在读</b>
          <router-link to="/library" class="btn sm">＋ 上传论文</router-link>
        </div>
        <div v-for="p in data.recent" :key="p.id" class="side-item"
             style="display:flex; justify-content:space-between; gap:8px; white-space:nowrap;"
             @click="router.push(`/reader/${p.id}`)">
          <span class="t" style="flex:1;">{{ p.title_zh || p.title }}</span>
          <span class="badge gray">{{ p.log_status || '未记录' }}</span>
        </div>
        <div v-if="!data.recent.length" class="muted" style="padding:16px; text-align:center;">
          还没有论文，先去 <router-link to="/library">论文库</router-link> 上传一篇吧
        </div>
      </div>

      <div class="card">
        <div style="display:flex; justify-content:space-between; align-items:center; margin-bottom:12px;">
          <b>待解决问题</b>
          <router-link to="/log" class="btn sm">研究总表 →</router-link>
        </div>
        <div v-for="q in data.open_questions" :key="q.id" class="side-item"
             style="white-space:normal;" @click="router.push(`/reader/${q.paper_id}`)">
          <span class="t">❓ {{ q.content }}</span>
          <span class="muted" style="font-size:11px;">{{ (q.title_zh || q.title || '').slice(0, 24) }}</span>
        </div>
        <div v-if="!data.open_questions.length" class="muted" style="padding:16px; text-align:center;">
          没有未解决的问题，太棒了 🎉
        </div>
      </div>
    </div>
  </div>
  <div v-else class="empty-tip">加载中…</div>
</template>
