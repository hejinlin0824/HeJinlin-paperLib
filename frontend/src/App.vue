<script setup>
import { computed } from 'vue'
import { useRoute } from 'vue-router'
import { store } from './stores'

const route = useRoute()
// 阅读器全屏沉浸：隐藏应用侧栏
const isReader = computed(() => route.path.startsWith('/reader'))
const navs = [
  { path: '/', label: '工作台', ico: '⌂' },
  { path: '/library', label: '论文库', ico: '▤' },
  { path: '/log', label: '研究总表', ico: '☰' },
  { path: '/settings', label: '设置', ico: '⚙' },
]
</script>

<template>
  <div class="layout">
    <aside v-if="!isReader" class="sidebar">
      <div class="brand"><span class="logo">📚</span>论文图书馆</div>
      <router-link v-for="n in navs" :key="n.path" :to="n.path" class="nav-item"
        :class="{ active: $route.path === n.path }">
        <span class="ico">{{ n.ico }}</span>{{ n.label }}
      </router-link>
      <div class="foot">v0.1 · 数据全部保存在本地<br>data/ 目录即完整备份</div>
    </aside>
    <main class="main" :class="{ bare: isReader }">
      <router-view />
    </main>
    <div class="toasts">
      <div v-for="t in store.toasts" :key="t.id" class="toast" :class="t.type">{{ t.msg }}</div>
    </div>
  </div>
</template>
