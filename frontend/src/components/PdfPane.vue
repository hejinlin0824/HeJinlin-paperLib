<script setup>
// 原版 PDF 并排面板（pdf.js 渲染），按块所在页联动
import * as pdfjsLib from 'pdfjs-dist'
import workerUrl from 'pdfjs-dist/build/pdf.worker.min.mjs?url'
import { onBeforeUnmount, onMounted, ref, watch } from 'vue'

pdfjsLib.GlobalWorkerOptions.workerSrc = workerUrl

const props = defineProps({
  url: String,
  targetPage: Number,   // 块点击时定位到的页码
})
const emit = defineEmits(['page'])

const wrap = ref(null)
const loading = ref(true)
const error = ref('')
const pageCount = ref(0)
const currentPage = ref(1)
let canvases = []
let observer = null
let doc = null

async function load() {
  loading.value = true
  error.value = ''
  try {
    doc = await pdfjsLib.getDocument(props.url).promise
    pageCount.value = doc.numPages
    const container = wrap.value
    const width = container.clientWidth - 16
    for (let i = 1; i <= doc.numPages; i++) {
      const page = await doc.getPage(i)
      const base = page.getViewport({ scale: 1 })
      const scale = Math.min(width / base.width, 2)
      const viewport = page.getViewport({ scale })
      const canvas = document.createElement('canvas')
      canvas.width = viewport.width
      canvas.height = viewport.height
      canvas.dataset.page = i
      container.appendChild(canvas)
      canvases.push(canvas)
      await page.render({ canvasContext: canvas.getContext('2d'), viewport }).promise
      if (i === 1) loading.value = false
    }
    observer = new IntersectionObserver((entries) => {
      for (const en of entries) {
        if (en.isIntersecting) {
          currentPage.value = parseInt(en.target.dataset.page) || 1
          emit('page', currentPage.value)
        }
      }
    }, { root: container, threshold: 0.4 })
    canvases.forEach(c => observer.observe(c))
  } catch (e) {
    error.value = 'PDF 加载失败：' + (e?.message || e)
    loading.value = false
  }
}

watch(() => props.targetPage, (p) => {
  if (!p) return
  const c = canvases.find(c => parseInt(c.dataset.page) === p)
  if (c) c.scrollIntoView({ behavior: 'smooth', block: 'start' })
})

onMounted(load)
onBeforeUnmount(() => {
  observer?.disconnect()
  doc?.destroy?.()
})
</script>

<template>
  <div class="pdf-pane">
    <div class="pdf-float">
      <span class="badge gray" style="background:#e8e8e8">{{ currentPage }} / {{ pageCount }} 页</span>
    </div>
    <div ref="wrap" style="padding: 8px 8px 40px;">
      <div v-if="loading" class="empty-tip" style="color:#ddd">PDF 渲染中…</div>
      <div v-if="error" class="empty-tip" style="color:#ffb4a8">{{ error }}</div>
    </div>
  </div>
</template>
