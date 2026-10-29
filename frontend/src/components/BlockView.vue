<script setup>
// 单个内容块的渲染：三种模式（zh/en/bilingual）+ 批注标记 + 段落工具条
import { computed, ref } from 'vue'
import { renderInline, renderDisplayMath, mdTableToHtml, sanitizeHtml } from '../inline'

const props = defineProps({
  block: Object,
  pid: String,
  mode: String,            // 'zh' | 'en' | 'bilingual'
  annotations: { type: Array, default: () => [] },
  translating: Boolean,
})
const emit = defineEmits(['retranslate', 'save-zh', 'add-anno', 'anno-click', 'open-image'])

// 展开/收起另一种语言（段后按钮控制；不再用整段点击，避免与划词标注冲突）
const revealed = ref(false)

const HL_COLORS = { yellow: 'var(--hl-yellow)', green: 'var(--hl-green)', blue: 'var(--hl-blue)', pink: 'var(--hl-pink)' }

const editing = ref(false)
const editVal = ref('')
const showRaw = ref(false)

const type = computed(() => props.block.type)
const meta = computed(() => props.block.meta || {})
const translatable = computed(() => ['text', 'heading', 'image', 'table', 'list'].includes(type.value))

const zhText = computed(() => (props.block.zh || '').trim())
const pending = computed(() => !zhText.value && !!props.block.en && props.translating)
// 单语模式下是否存在可展开的另一语言
const peerAvailable = computed(() => {
  if (props.mode === 'bilingual') return false
  return primary.value === 'zh' ? !!htmlEn.value : !!htmlZh.value
})
const revealLabel = computed(() => {
  if (revealed.value) return '收起'
  return primary.value === 'zh' ? 'EN 原文' : '中文译文'
})

// 主显示语言：中文模式下无译文时回退显示英文原文
const primary = computed(() => {
  if (props.mode === 'en') return 'en'
  return zhText.value ? 'zh' : 'en'
})

const marksFor = (surface) => props.annotations
  .filter(a => a.surface === surface && (a.kind === 'highlight' || a.kind === 'bold'))
  .map(a => ({
    anchor_text: a.anchor_text,
    cls: a.kind === 'bold' ? 'ub' : 'hl',
    color: HL_COLORS[a.color] || a.color || 'var(--hl-yellow)',
    id: a.id,
  }))

const PIN_ICON = { note: '💬', summary: '📌', question: '❓' }
const pinsHtml = computed(() => props.annotations
  .filter(a => ['note', 'summary', 'question'].includes(a.kind))
  .map(a => {
    const label = { note: '批注', summary: '小结', question: '问题' }[a.kind]
    const tip = (a.content || '').slice(0, 100).replace(/"/g, "'")
    return `<span class="anno-pin anno-${a.kind}-pin" data-anno="${a.id}" title="${label}：${tip}">${PIN_ICON[a.kind]}</span>`
  }).join(''))

const htmlZh = computed(() => {
  if (!zhText.value) return ''
  let h = renderInline(zhText.value, marksFor('zh')).replace(/\n/g, '<br>')
  if (primary.value === 'zh') h += pinsHtml.value
  return h
})
const htmlEn = computed(() => {
  if (!props.block.en) return ''
  let h = renderInline(props.block.en, marksFor('en')).replace(/\n/g, '<br>')
  if (primary.value === 'en') h += pinsHtml.value
  return h
})

// 图注/表注（无 pins，可带 marks）
const capZhHtml = computed(() => zhText.value ? renderInline(zhText.value, marksFor('zh')) : '')
const capEnHtml = computed(() => props.block.en ? renderInline(props.block.en, marksFor('en')) : '')

const level = computed(() => Math.min(Math.max(parseInt(meta.value.level || 2) || 2, 1), 3))
const authorLines = computed(() => (props.block.en || '').split('\n').map(l => l.trim()).filter(Boolean))
const latex = computed(() => meta.value.latex || props.block.en || '')
const mathHtml = computed(() => renderDisplayMath(latex.value))
const imgSrc = computed(() => `/files/papers/${props.pid}/${meta.value.path || ''}`)

const tableHtml = computed(() => {
  if (meta.value.html) return sanitizeHtml(meta.value.html)
  if (meta.value.md) return mdTableToHtml(meta.value.md)
  return props.block.en ? mdTableToHtml(props.block.en) : ''
})
// en/zh 字段本身是 markdown 表格（内置引擎）：分别渲染
const isMdTable = (s) => !!(s || '').trim().startsWith('|')
const enTableHtml = computed(() => isMdTable(props.block.en) ? mdTableToHtml(props.block.en) : '')
const zhTableHtml = computed(() => isMdTable(props.block.zh) ? mdTableToHtml(props.block.zh) : '')
const refs = computed(() => meta.value.entries?.length ? meta.value.entries : (props.block.en || '').split('\n').filter(Boolean))

function startEdit() {
  editVal.value = props.block.zh || ''
  editing.value = true
}
function saveEdit() {
  emit('save-zh', editVal.value)
  editing.value = false
}
</script>

<template>
  <div class="blk" :class="'blk-' + type" :data-block-id="block.id">
    <div class="blk-bar" v-if="translatable">
      <button v-if="block.en" title="重译本段" @click.stop="emit('retranslate')">↻</button>
      <button v-if="block.en" title="编辑译文" @click.stop="startEdit">✎</button>
      <button title="写批注" @click.stop="emit('add-anno', { kind: 'note' })">💬</button>
      <button title="记问题" @click.stop="emit('add-anno', { kind: 'question' })">❓</button>
      <button title="段落小结" @click.stop="emit('add-anno', { kind: 'summary' })">📝</button>
    </div>

    <!-- 论文头部：作者 / 单位（居中卡片，保留原文，支持悬停取词） -->
    <div v-else-if="type === 'authors'" class="blk-authors" data-surface="en">
      <div v-for="(line, i) in authorLines" :key="i" class="al"
           :class="{ affil: /university|institute|college|department|laborator|school of|@|inc\.|corp/i.test(line) }">
        {{ line }}
      </div>
    </div>

    <!-- 标题：主行跟随阅读模式，副行小字显示另一语言 -->
    <div v-if="type === 'heading'" class="blk-heading" :class="'lv' + level">
      <div v-if="primary === 'zh' ? htmlZh : htmlEn" :data-surface="primary"
           v-html="primary === 'zh' ? htmlZh : htmlEn"></div>
      <div v-if="(primary === 'zh' ? htmlEn : htmlZh) && (mode !== 'zh' || revealed)"
           :data-surface="primary === 'zh' ? 'en' : 'zh'" class="sub"
           v-html="primary === 'zh' ? htmlEn : htmlZh"></div>
    </div>

    <!-- 正文段落 -->
    <div v-else-if="type === 'text' || type === 'list'" class="blk-text">
      <template v-if="editing">
        <textarea class="input" v-model="editVal" rows="5" @click.stop></textarea>
        <div style="margin-top:6px; display:flex; gap:8px;">
          <button class="btn primary sm" @click.stop="saveEdit">保存</button>
          <button class="btn sm" @click.stop="editing = false">取消</button>
        </div>
      </template>
      <template v-else-if="mode === 'bilingual' && primary === 'zh'">
        <div class="bilingual-grid">
          <div class="en-only" data-surface="en" v-if="htmlEn" v-html="htmlEn"></div>
          <div data-surface="zh" v-html="htmlZh"></div>
        </div>
      </template>
      <template v-else>
        <div v-if="htmlZh && primary === 'zh'" data-surface="zh" v-html="htmlZh"></div>
        <div v-else-if="htmlEn" class="en-only" data-surface="en" v-html="htmlEn"></div>
        <div v-if="revealed && primary === 'zh' && htmlEn" class="peer" data-surface="en" v-html="htmlEn"></div>
        <div v-if="revealed && primary === 'en' && htmlZh" class="peer" data-surface="zh" v-html="htmlZh"></div>
        <div v-if="peerAvailable" class="reveal-row">
          <button class="reveal-toggle" @click.stop="revealed = !revealed">{{ revealLabel }} ▾</button>
        </div>
      </template>
      <div v-if="pending" class="muted">⏳ 本段翻译中…</div>
    </div>

    <!-- 图片 -->
    <div v-else-if="type === 'image' && meta.path" class="blk-image">
      <img :src="imgSrc" loading="lazy" @click="emit('open-image', imgSrc)" />
      <div v-if="zhText || block.en" class="cap">
        <span v-if="zhText" data-surface="zh" v-html="capZhHtml"></span>
        <span v-if="block.en && (!zhText || mode !== 'zh' || revealed)" data-surface="en" class="muted"
              v-html="capEnHtml"></span>
      </div>
    </div>

    <!-- 公式 -->
    <div v-else-if="type === 'formula'" class="blk-formula" :class="{ 'show-raw': showRaw }"
         :title="meta.raw ? '内置引擎解析的公式原文字符（完整 LaTeX 还原请用 MinerU 引擎）' : '点击切换 LaTeX 源码'"
         @click.stop="!meta.raw && (showRaw = !showRaw)">
      <div v-if="meta.raw" class="raw-formula">{{ latex }}</div>
      <template v-else>
        <div class="rendered" v-html="mathHtml"></div>
        <div class="raw">{{ latex }}</div>
      </template>
    </div>

    <!-- 表格 -->
    <div v-else-if="type === 'table'" class="blk-table">
      <!-- MinerU 结构：en/zh 为标题说明，表格 HTML 在 meta 里 -->
      <template v-if="meta.html">
        <div v-if="zhText || block.en" class="cap">
          <span v-if="zhText" data-surface="zh" v-html="capZhHtml"></span>
          <span v-if="block.en && (!zhText || mode !== 'zh' || revealed)" data-surface="en" class="muted"
                v-html="capEnHtml"></span>
        </div>
        <div data-surface="en" v-html="tableHtml"></div>
      </template>
      <!-- 内置引擎：en/zh 字段本身是 markdown 表格 -->
      <template v-else>
        <template v-if="mode === 'bilingual' && primary === 'zh'">
          <div v-if="enTableHtml" class="en-only" data-surface="en" v-html="enTableHtml"></div>
          <div v-if="zhTableHtml" data-surface="zh" v-html="zhTableHtml"></div>
        </template>
        <template v-else>
          <div v-if="primary === 'zh' && zhTableHtml" data-surface="zh" v-html="zhTableHtml"></div>
          <div v-else-if="enTableHtml" class="en-only" data-surface="en" v-html="enTableHtml"></div>
          <div v-if="revealed && primary === 'zh' && enTableHtml" class="peer" data-surface="en"
               v-html="enTableHtml"></div>
          <div v-if="revealed && primary === 'en' && zhTableHtml" class="peer" data-surface="zh"
               v-html="zhTableHtml"></div>
        </template>
      </template>
    </div>

    <!-- 参考文献 -->
    <div v-else-if="type === 'references'">
      <div class="blk-heading lv2">参考文献</div>
      <ol class="blk-refs">
        <li v-for="(r, i) in refs" :key="i">{{ r }}</li>
      </ol>
    </div>
  </div>
</template>
