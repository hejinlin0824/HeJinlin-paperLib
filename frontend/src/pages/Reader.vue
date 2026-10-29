<script setup>
// 阅读器：三模式阅读 + 选区批注（高亮/加粗/批注/问题）+ 侧边导航 + 原PDF并排 + 进度记忆
import { computed, nextTick, onBeforeUnmount, onMounted, reactive, ref } from 'vue'
import { useRoute } from 'vue-router'
import api from '../api'
import { toast } from '../stores'
import BlockView from '../components/BlockView.vue'
import SectionCard from '../components/SectionCard.vue'
import PdfPane from '../components/PdfPane.vue'
import { findAnchor, renderInline, renderRich } from '../inline'

const route = useRoute()
const pid = route.params.id

const ACTIVE = new Set(['pending_parse', 'parsing', 'queued', 'translating'])

const paper = ref(null)
const blocks = ref([])
const sections = ref([])
const annotations = ref([])
const mode = ref(localStorage.getItem('reader-mode') || 'zh')
const sideTab = ref('outline')
const showPdf = ref(localStorage.getItem('reader-pdf') === '1')
const targetPage = ref(0)
const currentPage = ref(1)
const contentRef = ref(null)
const loading = ref(true)

const translating = computed(() => ACTIVE.has(paper.value?.status))
const canTranslate = computed(() =>
  ['parsed', 'ready', 'failed'].includes(paper.value?.status))
const pct = computed(() => paper.value?.translate_total
  ? Math.round((paper.value.translate_done / paper.value.translate_total) * 100) : 0)

// ---------- 导读与章节解读 ----------
const guideOpen = ref(true)
const guideLoading = ref(false)
let explainTimer = null

const guide = computed(() => {
  try { return JSON.parse(paper.value?.guide || 'null') } catch { return null }
})
const whyHtml = computed(() => renderRich(guide.value?.why_read || ''))
const explaining = computed(() => paper.value
  ? paper.value.explain_total > 0 && paper.value.explain_done < paper.value.explain_total
  : false)
const explainPct = computed(() => paper.value?.explain_total
  ? Math.round((paper.value.explain_done / paper.value.explain_total) * 100) : 0)

const cleaningReport = computed(() => {
  const raw = paper.value?.cleaning_report
  if (!raw) return { removed_count: 0, by_rule: {}, samples: [] }
  if (typeof raw === 'object') return raw
  try { return JSON.parse(raw) } catch { return { removed_count: 0, by_rule: {}, samples: [] } }
})
const cleaningSamples = computed(() => Array.isArray(cleaningReport.value.samples)
  ? cleaningReport.value.samples : [])
const CLEANING_LABELS = {
  arxiv_watermark: 'arXiv 水印',
  publication_watermark: '期刊/会议状态水印',
  publisher_margin_metadata: '页边出版信息',
  publisher_metadata: '出版模板信息',
  explicit_footnote: '明确标记的脚注',
  footnote_margin: '页底脚注区域',
  repeated_header_footer: '跨页重复页眉/页脚',
  page_footer: '页码/作者页脚',
  orphan_page_number: '孤立页码',
}

// 正文渲染序列：块 + 章节解读卡（插在本章最后一个块之后，读完再看解读）
const sectionByStart = computed(() => {
  const m = {}
  for (const s of sections.value) m[s.block_start] = s
  return m
})
const flow = computed(() => {
  const out = []
  let pendingSec = null
  for (const b of blocks.value) {
    const s = sectionByStart.value[b.id]
    if (s) {
      if (pendingSec) out.push({ kind: 'sec', s: pendingSec })
      pendingSec = s
    }
    out.push({ kind: 'blk', b })
  }
  if (pendingSec) out.push({ kind: 'sec', s: pendingSec })
  return out
})
function onSecUpdated(s) {
  sections.value = sections.value.map(x => (x.id === s.id ? s : x))
}
async function genGuide() {
  guideLoading.value = true
  toast('导读生成中，约 10~30 秒…')
  try {
    const g = await api.generateGuide(pid)
    paper.value = { ...paper.value, guide: JSON.stringify(g) }
    guideOpen.value = true
    toast('导读已生成', 'ok')
  } catch (e) { toast(e.message, 'err') } finally { guideLoading.value = false }
}
function onGuideBtn() {
  if (guide.value) {
    guideOpen.value = !guideOpen.value
    contentRef.value?.scrollTo({ top: 0, behavior: 'smooth' })
  } else {
    genGuide()
  }
}
async function explainAll() {
  if (explaining.value) return
  try {
    await api.explainPaper(pid)
    toast('全文解读已开始，进度见顶部按钮')
    pollExplain()
  } catch (e) { toast(e.message, 'err') }
}
function pollExplain() {
  if (explainTimer) return
  explainTimer = setInterval(async () => {
    try {
      const p = await api.paper(pid)
      paper.value = p
      if (!p.explain_total || p.explain_done >= p.explain_total) {
        clearInterval(explainTimer); explainTimer = null
        sections.value = await api.sections(pid)
        toast('全部章节解读完成', 'ok')
      }
    } catch { /* ignore */ }
  }, 2500)
}

// ---------- 数据加载 ----------
async function loadAll() {
  const [p, bs, secs, annos] = await Promise.all([
    api.paper(pid), api.blocks(pid), api.sections(pid), api.annotations(pid),
  ])
  paper.value = p
  blocks.value = bs
  sections.value = secs
  annotations.value = annos
  loading.value = false
  await nextTick()
  restoreProgress(p)
}

function restoreProgress(p) {
  try {
    const prog = JSON.parse(p.reading_progress || 'null')
    if (prog?.block_idx > 0 && contentRef.value) {
      const el = contentRef.value.querySelector('.inner').children[prog.block_idx]
      el?.scrollIntoView({ block: 'start' })
      toast('已回到上次阅读位置', 'ok')
    }
  } catch { /* ignore */ }
}

// ---------- 轮询（解析/翻译进度） ----------
let pollTimer = null
function startPoll() {
  if (pollTimer) return
  pollTimer = setInterval(async () => {
    try {
      paper.value = await api.paper(pid)
      if (paper.value.status === 'translating') {
        const st = contentRef.value?.scrollTop
        blocks.value = await api.blocks(pid)
        await nextTick()
        if (contentRef.value && st != null) contentRef.value.scrollTop = st
      }
      if (!ACTIVE.has(paper.value.status)) {
        clearInterval(pollTimer); pollTimer = null
        blocks.value = await api.blocks(pid)
      }
    } catch { /* ignore */ }
  }, 1600)
}

// ---------- 阅读进度 ----------
let scrollTimer = null
function onScroll() {
  clearTimeout(scrollTimer)
  scrollTimer = setTimeout(() => {
    if (!contentRef.value) return
    const inner = contentRef.value.querySelector('.inner')
    if (!inner) return
    let idx = 0
    Array.from(inner.children).forEach((el, i) => {
      if (el.getBoundingClientRect().top < 260) idx = i
    })
    const ratio = contentRef.value.scrollTop / Math.max(1, contentRef.value.scrollHeight - contentRef.value.clientHeight)
    api.saveProgress(pid, idx, ratio).catch(() => {})
  }, 900)
}

// ---------- 模式与展开 ----------
function setMode(m) {
  mode.value = m
  localStorage.setItem('reader-mode', m)
}
function togglePdf() {
  showPdf.value = !showPdf.value
  localStorage.setItem('reader-pdf', showPdf.value ? '1' : '0')
}

// ---------- 批注 ----------
const annosByBlock = computed(() => {
  const m = {}
  for (const a of annotations.value) {
    (m[a.block_id] = m[a.block_id] || []).push(a)
  }
  return m
})

const sel = reactive({ show: false, x: 0, y: 0, text: '', blockId: 0, surface: 'zh' })

function onMouseUp() {
  if (dlg.open) return
  const s = window.getSelection()
  if (!s || s.isCollapsed) { sel.show = false; return }
  const text = s.toString().trim()
  if (!text || text.length > 1500) { sel.show = false; return }
  const node = s.anchorNode
  const el = node?.nodeType === 3 ? node.parentElement : node
  const blkEl = el?.closest?.('[data-block-id]')
  if (!blkEl || !contentRef.value?.contains(blkEl)) { sel.show = false; return }
  const r = s.getRangeAt(0).getBoundingClientRect()
  sel.text = text
  sel.blockId = parseInt(blkEl.dataset.blockId)
  sel.surface = el?.closest?.('[data-surface]')?.dataset.surface || 'zh'
  sel.x = Math.min(Math.max(r.left + r.width / 2, 150), window.innerWidth - 150)
  sel.y = Math.max(r.top, 60)
  sel.show = true
}

async function addAnnotation(body) {
  const a = await api.addAnnotation(pid, body)
  annotations.value.unshift(a)
  window.getSelection()?.removeAllRanges()
  sel.show = false
  return a
}

const HIGHLIGHTS = [
  ['yellow', 'var(--hl-yellow)'], ['green', 'var(--hl-green)'],
  ['blue', 'var(--hl-blue)'], ['pink', 'var(--hl-pink)'],
]

function doHighlight(color) {
  addAnnotation({ kind: 'highlight', color, block_id: sel.blockId, surface: sel.surface, anchor_text: sel.text, content: '' })
}
function doBold() {
  addAnnotation({ kind: 'bold', block_id: sel.blockId, surface: sel.surface, anchor_text: sel.text, content: '' })
}
async function doCopy() {
  try { await navigator.clipboard.writeText(sel.text); toast('已复制', 'ok') } catch { toast('复制失败', 'err') }
  sel.show = false
}

// 批注/问题/小结 弹窗（创建或编辑）
const KIND_LABEL = { note: '批注', summary: '小结', question: '问题' }
const dlg = reactive({ open: false, kind: 'note', content: '', blockId: 0, anchorText: '', surface: 'zh', editAnno: null })

function openDialog(kind, blockId, anchorText, surface) {
  dlg.open = true; dlg.kind = kind; dlg.blockId = blockId || 0
  dlg.anchorText = anchorText || ''; dlg.surface = surface || 'zh'
  dlg.content = ''; dlg.editAnno = null
}
async function saveDialog() {
  const content = dlg.content.trim()
  if (!content && dlg.kind !== 'highlight') { toast('内容不能为空', 'err'); return }
  if (dlg.editAnno) {
    const a = await api.updateAnnotation(pid, dlg.editAnno, { content })
    const i = annotations.value.findIndex(x => x.id === a.id)
    if (i >= 0) annotations.value[i] = a
    toast('已更新', 'ok')
  } else {
    await addAnnotation({
      kind: dlg.kind, block_id: dlg.blockId, surface: dlg.surface,
      anchor_text: dlg.anchorText, content,
    })
    toast(KIND_LABEL[dlg.kind] + '已保存', 'ok')
  }
  dlg.open = false
}
async function deleteDialogAnno() {
  if (!dlg.editAnno) return
  await api.deleteAnnotation(pid, dlg.editAnno)
  annotations.value = annotations.value.filter(a => a.id !== dlg.editAnno)
  dlg.open = false
  toast('已删除', 'ok')
}

// 块事件
async function onRetranslate(b) {
  try {
    toast('重译中…')
    const nb = await api.retranslateBlock(pid, b.id)
    const i = blocks.value.findIndex(x => x.id === b.id)
    if (i >= 0) blocks.value[i] = nb
  } catch (e) { toast(e.message, 'err') }
}
async function onSaveZh(b, zh) {
  const nb = await api.editBlockZh(pid, b.id, zh)
  const i = blocks.value.findIndex(x => x.id === b.id)
  if (i >= 0) blocks.value[i] = nb
  toast('译文已保存', 'ok')
}
async function startTranslate() {
  try {
    await api.translatePaper(pid)
    toast('开始翻译', 'ok')
    startPoll()
  } catch (e) { toast(e.message, 'err') }
}

// 内容点击：批注标记 / 引用提示 / PDF 页联动
const citeTip = reactive({ show: false, x: 0, y: 0, text: '' })
let citeTimer = null
const imgViewer = reactive({ open: false, src: '' })

// ---------- 选区查词（选中文字 → 工具条 📖释义 → 词卡） ----------
const wordCard = reactive({ show: false, loading: false, word: '', phonetic: '', meaning: '', note: '', notFound: false, x: 0, y: 0 })

function hideWordCard() {
  wordCard.show = false
}

async function lookupSelection() {
  const text = (sel.text || '').trim()
  if (!text || text.split(/\s+/).length > 5) { toast('请选中一个单词或短语（≤5词）', 'err'); return }
  wordCard.word = text
  wordCard.loading = true
  wordCard.show = true
  wordCard.notFound = false
  wordCard.x = Math.max(12, Math.min(sel.x - 120, (window.innerWidth || 1200) - 300))
  wordCard.y = Math.min(sel.y + 42, (window.innerHeight || 800) - 230)
  wordCard.phonetic = ''
  wordCard.meaning = ''
  wordCard.note = ''
  try {
    const c = await api.dictCard(text, text)
    if (c.not_found) {
      wordCard.meaning = ''
      wordCard.note = ''
      wordCard.notFound = true
    } else {
      wordCard.phonetic = c.phonetic || ''
      wordCard.meaning = c.meaning || ''
      wordCard.note = c.context_note || c.note || ''
      wordCard.notFound = false
    }
  } catch (e) {
    wordCard.meaning = '查询失败：' + e.message
    wordCard.notFound = false
  } finally {
    wordCard.loading = false
  }
}

function speakWord() {
  try {
    const u = new SpeechSynthesisUtterance(wordCard.word)
    u.lang = 'en-US'
    speechSynthesis.cancel()
    speechSynthesis.speak(u)
  } catch { /* ignore */ }
}

function onContentClick(e) {
  // 有拖选选区时不做任何点击处理（保护标注工具条）
  const s = window.getSelection()
  const selecting = !!(s && !s.isCollapsed && s.toString().trim())
  // 批注标记 → 编辑
  const mark = e.target.closest('[data-anno]')
  if (mark) {
    const id = parseInt(mark.dataset.anno)
    const a = annotations.value.find(x => x.id === id)
    if (a) {
      dlg.open = true; dlg.editAnno = a.id; dlg.kind = a.kind
      dlg.content = a.content; dlg.anchorText = a.anchor_text
      dlg.blockId = a.block_id; dlg.surface = a.surface
      sel.show = false
    }
    return
  }
  // 引用编号 → 悬浮显示参考文献
  const cite = e.target.closest('sup.cite')
  if (cite) {
    const n = parseInt((cite.dataset.cite || '').split(/[,;-]/)[0])
    const entry = refsEntries.value[n - 1]
    citeTip.show = true
    citeTip.x = e.clientX
    citeTip.y = e.clientY
    citeTip.text = entry ? entry.replace(/^\[\d+\]\s*/, '') : '未找到该条参考文献'
    clearTimeout(citeTimer)
    citeTimer = setTimeout(() => citeTip.show = false, 6000)
    return
  }
  if (selecting) return
  sel.show = false
  // PDF 页联动
  const blkEl = e.target.closest('[data-block-id]')
  if (blkEl) {
    const b = blocks.value.find(x => x.id === parseInt(blkEl.dataset.blockId))
    const p = b?.meta?.page
    if (p && showPdf.value) targetPage.value = p
  }
}

// ---------- 侧边栏 ----------
const headings = computed(() => blocks.value.filter(b => b.type === 'heading'))
const figures = computed(() => blocks.value.filter(b => ['image', 'table', 'formula'].includes(b.type)))
const refsEntries = computed(() => {
  const rb = blocks.value.find(b => b.type === 'references')
  return rb?.meta?.entries || []
})
// 大纲缩进深度：编号标题按编号层级（"1"=1级，"3.2"=2级）；
// 无编号小节跟随当前章深度+1（如"从盔甲到马具"挂在第3章下）；摘要顶格
const headingsWithDepth = computed(() => {
  let cur = 1
  return headings.value.map(h => {
    const text = (h.zh || h.en || '').trim()
    const m = text.match(/^(\d+(?:\.\d+)*)[\s.．]/)
    if (m) {
      cur = Math.min(m[1].split('.').length, 4)
      return { ...h, depth: cur }
    }
    if (/^abstract/i.test(h.en || '') || /^摘/.test(text)) {
      cur = 0
      return { ...h, depth: 0 }
    }
    return { ...h, depth: Math.min(cur + 1, 5) }
  })
})
function anchorLost(a) {
  if (!a.anchor_text) return false
  const b = blocks.value.find(x => x.id === a.block_id)
  if (!b) return true
  const text = a.surface === 'en' ? b.en : (b.zh || b.en)
  return findAnchor(text || '', a.anchor_text) < 0
}
function scrollToBlock(id) {
  contentRef.value?.querySelector(`[data-block-id="${id}"]`)?.scrollIntoView({
    behavior: 'smooth', block: 'center',
  })
}

const STATUS_MAP = {
  pending_parse: ['解析排队中', 'yellow'], parsing: ['解析中', 'yellow'],
  queued: ['翻译排队中', 'yellow'], translating: ['翻译中', 'blue'],
  parsed: ['待翻译', 'yellow'], ready: ['已就绪', 'green'], failed: ['失败', 'red'],
}

const citeLeftPx = computed(() =>
  Math.min(citeTip.x, (window.innerWidth || 1200) - 390) + 'px')

onMounted(async () => {
  document.addEventListener('mouseup', onMouseUp)
  try { await loadAll() } catch (e) { toast('加载失败：' + e.message, 'err') }
  if (ACTIVE.has(paper.value?.status)) startPoll()
})
onBeforeUnmount(() => {
  document.removeEventListener('mouseup', onMouseUp)
  if (pollTimer) clearInterval(pollTimer)
  if (explainTimer) clearInterval(explainTimer)
})
</script>

<template>
  <div class="reader" v-if="paper">
    <!-- 顶栏 -->
    <div class="reader-top">
      <router-link to="/library" class="btn sm">← 论文库</router-link>
      <div class="t-title" :title="paper.title">{{ paper.title_zh || paper.title }}</div>
      <span class="badge" :class="STATUS_MAP[paper.status]?.[1] || 'gray'">
        {{ STATUS_MAP[paper.status]?.[0] || paper.status }}</span>
      <div v-if="paper.status === 'translating'" class="progressbar" style="width:110px">
        <div :style="{ width: pct + '%' }"></div>
      </div>
      <span v-if="paper.status === 'translating'" class="muted">{{ pct }}%</span>
      <div style="flex:1"></div>
      <button v-if="canTranslate" class="btn sm primary" @click="startTranslate">⚡ 翻译全文</button>
      <button class="btn sm" :disabled="guideLoading" @click="onGuideBtn"
              title="整篇导读：为什么读、带着什么问题、结构地图">
        🧭 {{ guideLoading ? '生成中…' : (guide ? '导读' : '生成导读') }}</button>
      <button class="btn sm" :disabled="explaining" @click="explainAll"
              title="为每个章节生成：通俗理解、本章要点、思考问题">
        {{ explaining ? `💡 解读中 ${explainPct}%` : '💡 解读全文' }}</button>
      <a class="btn sm" :href="`/api/papers/${pid}/export.notes.md`"
         title="导出精读笔记：导读 + 每章译文/通俗解释/要点/思考问题（含你的回答与AI点评）">⤓ 笔记</a>
      <div class="seg">
        <button :class="{ on: mode === 'zh' }" @click="setMode('zh')">中文</button>
        <button :class="{ on: mode === 'en' }" @click="setMode('en')">英文</button>
        <button :class="{ on: mode === 'bilingual' }" @click="setMode('bilingual')">对照</button>
      </div>
      <button class="btn sm" :class="{ primary: showPdf }" @click="togglePdf"
              title="并排显示原版 PDF">原PDF</button>
    </div>

    <div v-if="paper.error" style="padding:8px 18px; background:#f9e9e7; color:var(--danger); font-size:13px;">
      {{ paper.error }}
    </div>

    <div v-if="cleaningReport.removed_count > 0" style="padding:8px 18px; background:var(--panel); border-bottom:1px solid var(--line); font-size:12px;">
      <details>
        <summary style="cursor:pointer; color:var(--ink-2)">
          🧹 已自动清理 {{ cleaningReport.removed_count }} 项非正文内容（页眉、arXiv 水印、页脚或脚注）· 查看清理记录
        </summary>
        <p style="margin:8px 0 4px; color:var(--ink-2)">
          只自动移除高置信的独立水印/元数据；跨页重复项需在至少 3 个不同页面重复出现。正文里正常提及 arXiv 不会按关键词删除。
        </p>
        <ul style="margin:4px 0 4px 18px; padding:0; line-height:1.7">
          <li v-for="(sample, idx) in cleaningSamples" :key="idx">
            {{ sample.page ? '第 ' + sample.page + ' 页 · ' : '' }}{{ CLEANING_LABELS[sample.rule] || sample.rule }}：{{ sample.text }}
          </li>
        </ul>
      </details>
    </div>

    <div class="reader-body">
      <!-- 侧边导航 -->
      <aside class="reader-side">
        <div class="tabs">
          <div :class="{ active: sideTab === 'outline' }" @click="sideTab = 'outline'">大纲</div>
          <div :class="{ active: sideTab === 'figures' }" @click="sideTab = 'figures'">图表</div>
          <div :class="{ active: sideTab === 'notes' }" @click="sideTab = 'notes'">
            批注<span v-if="annotations.length">({{ annotations.length }})</span></div>
          <div :class="{ active: sideTab === 'refs' }" @click="sideTab = 'refs'">文献</div>
        </div>
        <div class="panel">
          <template v-if="sideTab === 'outline'">
            <div v-for="h in headingsWithDepth" :key="h.id" class="side-item"
                 :style="{ paddingLeft: (10 + h.depth * 15) + 'px' }"
                 @click="scrollToBlock(h.id)">
              <span class="t" :style="{ fontWeight: h.depth <= 1 ? 600 : 400 }">{{ h.zh || h.en }}</span>
            </div>
            <div v-if="!headings.length" class="muted" style="padding:10px">未识别到大纲</div>
          </template>
          <template v-else-if="sideTab === 'figures'">
            <div v-for="f in figures" :key="f.id" class="side-item" @click="scrollToBlock(f.id)">
              <img v-if="f.type === 'image' && f.meta.path"
                   :src="`/files/papers/${pid}/${f.meta.path}`" class="side-thumb" />
              <span class="t">
                <span class="badge gray" style="margin-right:4px">
                  {{ f.type === 'image' ? '图' : f.type === 'table' ? '表' : '式' }}{{ f.meta.page ? '·p' + f.meta.page : '' }}</span>
                {{ (f.zh || f.en || f.meta.latex || '').slice(0, 46) }}</span>
            </div>
            <div v-if="!figures.length" class="muted" style="padding:10px">无图表</div>
          </template>
          <template v-else-if="sideTab === 'notes'">
            <div v-for="a in annotations" :key="a.id" class="side-note"
                 :class="'kind-' + a.kind" @click="scrollToBlock(a.block_id)">
              <div class="muted">
                {{ { highlight: '高亮', bold: '加粗', note: '💬 批注', summary: '📌 小结', question: '❓ 问题' }[a.kind] }}
                <span v-if="anchorLost(a)" style="color:var(--warn)">（锚点未找到）</span>
              </div>
              <div class="c">{{ a.content || a.anchor_text }}</div>
            </div>
            <div v-if="!annotations.length" class="muted" style="padding:10px">
              选中正文文字可高亮/加粗；段落工具条可写批注、记问题、做小结</div>
          </template>
          <template v-else>
            <div v-for="(r, i) in refsEntries" :key="i" class="side-item" style="white-space:normal">
              {{ r }}
            </div>
            <div v-if="!refsEntries.length" class="muted" style="padding:10px">未解析到参考文献</div>
          </template>
        </div>
      </aside>

      <!-- 正文 -->
      <div class="reader-content" ref="contentRef" @scroll="onScroll" @click="onContentClick">
        <div class="inner">
          <!-- 导读卡片 -->
          <div v-if="guide" class="guide-card">
            <div class="g-title">🧭 导读
              <button class="btn sm" style="float:right" @click.stop="guideOpen = !guideOpen">
                {{ guideOpen ? '收起' : '展开' }}</button>
            </div>
            <template v-if="guideOpen">
              <div class="g-sec">🎯 为什么应该读这篇论文？</div>
              <div class="g-body" v-html="whyHtml"></div>
              <template v-if="guide.questions && guide.questions.length">
                <div class="g-sec">❓ 带着这些问题读</div>
                <ol class="g-list">
                  <li v-for="(q, i) in guide.questions" :key="i"
                      v-html="renderInline(q || '')"></li>
                </ol>
              </template>
              <template v-if="guide.structure && guide.structure.length">
                <div class="g-sec">🗺️ 结构速览</div>
                <div class="g-item" v-for="(s, i) in guide.structure" :key="i">
                  <b v-html="renderInline(s.title || '')"></b> —
                  <span v-html="renderInline(s.summary || '')"></span>
                </div>
              </template>
              <div class="g-regen">
                <button class="btn sm" :disabled="guideLoading" @click.stop="genGuide">↻ 重新生成导读</button>
              </div>
            </template>
          </div>
          <h1 style="font-size:24px; line-height:1.45; margin:10px 0 6px;">
            {{ paper.title_zh || paper.title }}</h1>
          <div v-if="paper.title && paper.title !== paper.title_zh" class="muted"
               style="margin-bottom:4px;" data-surface="en">{{ paper.title }}</div>
          <div class="muted" style="margin-bottom:26px;">
            {{ paper.authors || '作者未知' }}<template v-if="paper.year"> ｜ {{ paper.year }}</template>
            <template v-if="paper.venue"> ｜ {{ paper.venue }}</template>
          </div>
          <template v-for="f in flow" :key="f.kind === 'blk' ? 'b' + f.b.id : 's' + f.s.id">
            <BlockView v-if="f.kind === 'blk'" :block="f.b" :pid="pid" :mode="mode"
                       :annotations="annosByBlock[f.b.id] || []"
                       :translating="translating"
                       @retranslate="onRetranslate(f.b)"
                       @save-zh="(zh) => onSaveZh(f.b, zh)"
                       @add-anno="(d) => openDialog(d.kind, f.b.id, '', 'zh')"
                       @open-image="(s) => { imgViewer.open = true; imgViewer.src = s }" />
            <SectionCard v-else :section="f.s" :pid="pid" @updated="onSecUpdated" />
          </template>
          <div v-if="loading" class="empty-tip">加载中…</div>
          <div v-else-if="!blocks.length" class="empty-tip">
            暂无内容（{{ paper.error || '解析结果为空' }}）</div>
        </div>
      </div>

      <!-- 原版 PDF -->
      <PdfPane v-if="showPdf" :url="`/files/papers/${pid}/original.pdf`"
               :targetPage="targetPage" @page="(p) => currentPage = p" />
    </div>

    <!-- 选中文字浮动工具条 -->
    <div v-if="sel.show" class="sel-toolbar" :style="{ left: sel.x + 'px', top: (sel.y - 46) + 'px' }">
      <span v-for="[name, color] in HIGHLIGHTS" :key="name" class="sw"
            :style="{ background: color }" :title="'高亮'" @mousedown.prevent="doHighlight(name)"></span>
      <button class="tb" @mousedown.prevent="lookupSelection" title="查词典释义">📖</button>
      <button class="tb" @mousedown.prevent="doBold" title="加粗标记"><b>B</b></button>
      <button class="tb" @mousedown.prevent="openDialog('note', sel.blockId, sel.text, sel.surface)">💬 批注</button>
      <button class="tb" @mousedown.prevent="openDialog('question', sel.blockId, sel.text, sel.surface)">❓ 问题</button>
      <button class="tb" @mousedown.prevent="doCopy">⧉</button>
    </div>

    <!-- 批注/问题/小结 弹窗 -->
    <div v-if="dlg.open" class="modal-mask" @click.self="dlg.open = false">
      <div class="modal" style="width:520px">
        <h3>{{ dlg.editAnno ? '编辑' : '添加' }}{{ KIND_LABEL[dlg.kind] || '' }}</h3>
        <div v-if="dlg.anchorText" class="row">
          <div class="muted" style="border-left:3px solid var(--line); padding:4px 10px; max-height:80px; overflow:auto;">
            “{{ dlg.anchorText.slice(0, 160) }}{{ dlg.anchorText.length > 160 ? '…' : '' }}”
          </div>
        </div>
        <div class="row">
          <label>{{ dlg.kind === 'question' ? '我的问题' : dlg.kind === 'summary' ? '我的小结' : '批注内容' }}</label>
          <textarea class="input" v-model="dlg.content" rows="5"
                    :placeholder="dlg.kind === 'question' ? '例如：为什么消融实验中没有对比 XX？' : ''"></textarea>
        </div>
        <div class="actions">
          <button v-if="dlg.editAnno" class="btn danger" @click="deleteDialogAnno">删除</button>
          <div style="flex:1"></div>
          <button class="btn" @click="dlg.open = false">取消</button>
          <button class="btn primary" @click="saveDialog">保存</button>
        </div>
      </div>
    </div>

    <!-- 图片查看器 -->
    <div v-if="imgViewer.open" class="modal-mask" @click="imgViewer.open = false"
         style="align-items:center; justify-content:center;">
      <img :src="imgViewer.src" style="max-width:92vw; max-height:90vh; border-radius:8px; background:#fff;" />
    </div>

    <!-- 引用悬浮 -->
    <div v-if="citeTip.show" class="cite-popover"
         :style="{ left: citeLeftPx, top: (citeTip.y + 14) + 'px' }">
      {{ citeTip.text }}
    </div>

    <!-- 释义词卡（选中文字后从工具条打开） -->
    <div v-if="wordCard.show" class="word-card" :style="{ left: wordCard.x + 'px', top: wordCard.y + 'px' }"
         @mouseleave="hideWordCard">
      <div class="wc-head">
        <b class="wc-word">{{ wordCard.word }}</b>
        <span v-if="wordCard.phonetic" class="wc-phon">/{{ wordCard.phonetic }}/</span>
        <button class="wc-speak" title="发音" @click.stop="speakWord">🔊</button>
        <span style="flex:1"></span>
        <button class="wc-close" @click.stop="hideWordCard">✕</button>
      </div>
      <div v-if="wordCard.loading" class="wc-loading">查词中…</div>
      <template v-else-if="wordCard.notFound">
        <div class="wc-notfound">📖 本地词表没有「{{ wordCard.word }}」。<br>
          新术语/短语可以问问豆包，或者把它加到设置页的术语表里（翻译时统一应用）。</div>
      </template>
      <template v-else>
        <div class="wc-meaning">{{ wordCard.meaning }}</div>
        <div v-if="wordCard.note" class="wc-note">📌 本文语境：{{ wordCard.note }}</div>
      </template>
    </div>
  </div>
  <div v-else class="empty-tip" style="padding-top:80px;">加载中…</div>
</template>

<style scoped>
.seg { display: flex; border: 1px solid var(--line); border-radius: 8px; overflow: hidden; }
.seg button {
  border: none; background: var(--panel); padding: 5px 13px; font-size: 13px;
  cursor: pointer; color: var(--ink-2); border-right: 1px solid var(--line);
}
.seg button:last-child { border-right: none; }
.seg button.on { background: var(--accent); color: #fff; font-weight: 600; }
.cite-popover {
  position: fixed; z-index: 150; max-width: 360px; background: #2b3038; color: #e8eaed;
  padding: 10px 14px; border-radius: 9px; font-size: 12.5px; line-height: 1.6;
  box-shadow: 0 8px 24px rgba(0,0,0,.3);
}
</style>
