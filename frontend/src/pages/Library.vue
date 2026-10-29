<script setup>
// 论文库：上传（拖拽）+ 卡片列表 + 状态轮询 + 元数据编辑
import { onBeforeUnmount, onMounted, reactive, ref } from 'vue'
import { useRouter } from 'vue-router'
import api from '../api'
import { toast } from '../stores'

const router = useRouter()
const papers = ref([])
const uploading = ref(false)
const dragOver = ref(false)
const fileInput = ref(null)
const ACTIVE = new Set(['pending_parse', 'parsing', 'queued', 'translating'])

const edit = reactive({ open: false, id: '', form: {} })

const STATUS_MAP = {
  pending_parse: ['解析排队中', 'yellow'], parsing: ['解析中', 'yellow'],
  queued: ['翻译排队中', 'yellow'], translating: ['翻译中', 'blue'],
  parsed: ['待翻译', 'yellow'], ready: ['已就绪', 'green'], failed: ['失败', 'red'],
}

async function refresh() {
  papers.value = await api.papers()
}
let timer = null
function startPoll() {
  if (timer) return
  timer = setInterval(async () => {
    await refresh()
    if (!papers.value.some(p => ACTIVE.has(p.status))) {
      clearInterval(timer); timer = null
    }
  }, 1600)
}

async function uploadFiles(files) {
  const pdfs = [...files].filter(f => f.name.toLowerCase().endsWith('.pdf'))
  if (!pdfs.length) { toast('请选择 PDF 文件', 'err'); return }
  uploading.value = true
  for (const f of pdfs) {
    try {
      await api.uploadPdf(f)
      toast(`已上传「${f.name}」，开始解析`, 'ok')
    } catch (e) { toast(`上传失败：${e.message}`, 'err') }
  }
  uploading.value = false
  await refresh()
  startPoll()
}

function onDrop(e) {
  dragOver.value = false
  uploadFiles(e.dataTransfer.files)
}

function readEnabled(p) {
  return ['ready', 'parsed', 'failed'].includes(p.status)
}

function openEdit(p) {
  edit.open = true
  edit.id = p.id
  edit.form = { title: p.title, title_zh: p.title_zh, authors: p.authors,
                year: p.year, venue: p.venue, tags: p.tags, rating: p.rating || 0 }
}
async function saveEdit() {
  await api.patchPaper(edit.id, edit.form)
  toast('已保存', 'ok')
  edit.open = false
  refresh()
}

async function reparse(p) {
  await api.reparse(p.id)
  toast('重新解析中…', 'ok')
  refresh(); startPoll()
}
async function retranslate(p) {
  try {
    await api.translatePaper(p.id, true)
    toast('重新翻译中…', 'ok')
    refresh(); startPoll()
  } catch (e) { toast(e.message, 'err') }
}
async function remove(p) {
  if (!confirm(`确定删除「${p.title_zh || p.title}」？批注与记录将一并删除`)) return
  await api.deletePaper(p.id)
  toast('已删除', 'ok')
  refresh()
}

onMounted(async () => {
  await refresh()
  if (papers.value.some(p => ACTIVE.has(p.status))) startPoll()
})
onBeforeUnmount(() => { if (timer) clearInterval(timer) })
</script>

<template>
  <div>
    <h1 class="page-title">论文库</h1>
    <p class="page-sub">拖入 PDF，自动解析 → 翻译 → 进入阅读</p>

    <div class="dropzone" :class="{ drag: dragOver }" @click="fileInput.click()"
         @dragover.prevent="dragOver = true" @dragleave.prevent="dragOver = false"
         @drop.prevent="onDrop">
      <div style="font-size:26px; margin-bottom:6px;">📄</div>
      <b>{{ uploading ? '上传中…' : '点击或拖拽 PDF 到这里' }}</b>
      <div class="muted">支持一次选择多个文件 · 解析和翻译自动进行</div>
      <input ref="fileInput" type="file" accept=".pdf" multiple hidden
             @change="(e) => { uploadFiles(e.target.files); e.target.value = '' }" />
    </div>

    <div class="paper-grid">
      <div v-for="p in papers" :key="p.id" class="card paper-card">
        <div class="t1">{{ p.title_zh || p.title }}</div>
        <div class="t2" :title="p.title">{{ p.title }}</div>
        <div class="meta">
          <span v-if="p.year" class="badge gray">{{ p.year }}</span>
          <span v-if="p.venue" class="badge gray">{{ p.venue }}</span>
          <span class="badge" :class="STATUS_MAP[p.status]?.[1]">{{ STATUS_MAP[p.status]?.[0] || p.status }}</span>
          <span v-if="p.log_status" class="badge blue">{{ p.log_status }}</span>
          <span v-if="p.open_questions" class="badge red">❓{{ p.open_questions }}</span>
        </div>
        <div v-if="p.status === 'translating'" class="progressbar">
          <div :style="{ width: (p.translate_total ? Math.round(p.translate_done / p.translate_total * 100) : 0) + '%' }"></div>
        </div>
        <div v-if="p.error" class="muted" style="color:var(--danger); font-size:12px; margin-top:6px;">{{ p.error }}</div>
        <div v-if="p.tags" style="margin-top:6px;">
          <span v-for="t in p.tags.split(/[,，]/).filter(Boolean)" :key="t" class="badge gray" style="margin-right:4px;">{{ t.trim() }}</span>
        </div>
        <div class="ops">
          <button class="btn primary sm" :disabled="!readEnabled(p)"
                  @click="router.push(`/reader/${p.id}`)">📖 阅读</button>
          <button class="btn sm" @click="openEdit(p)">编辑</button>
          <button class="btn sm" title="重新解析" @click="reparse(p)">↻ 解析</button>
          <button class="btn sm" title="重新翻译" @click="retranslate(p)">⚡ 翻译</button>
          <a class="btn sm" :href="`/api/papers/${p.id}/export.md`">⤓ MD</a>
          <button class="btn sm danger" @click="remove(p)">🗑</button>
        </div>
      </div>
    </div>
    <div v-if="!papers.length" class="empty-tip" style="padding:60px 0;">
      论文库还是空的，从上面拖一篇 PDF 进来吧
    </div>

    <div v-if="edit.open" class="modal-mask" @click.self="edit.open = false">
      <div class="modal" style="width:560px;">
        <h3>编辑论文信息</h3>
        <div class="row"><label>英文标题</label><input class="input" v-model="edit.form.title" /></div>
        <div class="row"><label>中文标题</label><input class="input" v-model="edit.form.title_zh" /></div>
        <div class="row"><label>作者</label><input class="input" v-model="edit.form.authors" /></div>
        <div style="display:grid; grid-template-columns:1fr 1fr 1fr; gap:10px;">
          <div class="row"><label>年份</label><input class="input" v-model="edit.form.year" /></div>
          <div class="row"><label>来源</label><input class="input" v-model="edit.form.venue" /></div>
          <div class="row"><label>评分</label>
            <select class="select" v-model.number="edit.form.rating">
              <option v-for="n in 5" :key="n" :value="n">{{ '★'.repeat(n) }}</option>
              <option :value="0">未评</option>
            </select>
          </div>
        </div>
        <div class="row"><label>标签（逗号分隔）</label><input class="input" v-model="edit.form.tags" /></div>
        <div class="actions">
          <button class="btn" @click="edit.open = false">取消</button>
          <button class="btn primary" @click="saveEdit">保存</button>
        </div>
      </div>
    </div>
  </div>
</template>
