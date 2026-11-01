<script setup>
// 科研知识关系图：以实体类别组织空间，以真实关系表达知识联系。
import { computed, onBeforeUnmount, onMounted, ref } from 'vue'
import * as SigmaPkg from 'sigma'
import Graph from 'graphology'
import api from '../api'
import { groupRelationsByEndpoints } from '../utils/graphEdges'
import { computeCameraFit, computeSemanticLayout, toFramedCameraPoint } from '../utils/graphLayout'
import { toast } from '../stores'

const Sigma = SigmaPkg.default ?? SigmaPkg.Sigma

const overview = ref(null)
const papers = ref([])
const view = ref('overview')
const selPaper = ref('')
const q = ref('')
const activeTypes = ref(new Set())
const selRelation = ref('')
const showDust = ref(false)
const graphData = ref(null)
const loading = ref(false)
const panel = ref({ kind: 'empty' })
const topics = ref([])
const sharedResources = ref([])
const activeTopic = ref(null)
const job = ref(null)
const jobsTimer = ref(null)
const nodeCount = ref(0)
const edgeCount = ref(0)

const REL_ZH = {
  PROPOSES: '提出', ADDRESSES: '针对问题', USES: '使用', USES_METRIC: '评测指标',
  BELONGS_TO: '属于', AUTHORED_BY: '作者', PUBLISHED_IN: '发表于', REPORTS: '报告发现',
  MITIGATES: '缓解', IMPROVES: '改进', EVALUATED_ON: '验证于', USED_FOR: '用于任务',
  COMPARES: '对比', EXTENDS: '扩展',
}
const REL_OPTIONS = Object.keys(REL_ZH)

const TYPE_STYLE = {
  Paper: { color: '#365f8d', zh: '论文' },
  Algorithm: { color: '#3f806b', zh: '算法' },
  ResearchProblem: { color: '#b65d56', zh: '研究问题' },
  Dataset: { color: '#a87535', zh: '数据集' },
  Model: { color: '#7761a8', zh: '模型' },
  Metric: { color: '#9b8139', zh: '指标' },
  Concept: { color: '#3d8384', zh: '概念' },
  Finding: { color: '#a45d83', zh: '发现' },
  ResearchArea: { color: '#4f8568', zh: '领域' },
  Author: { color: '#718096', zh: '作者人名' },
  Venue: { color: '#9a7b55', zh: '期刊会议' },
  Task: { color: '#587ca5', zh: '任务' },
  Institution: { color: '#718096', zh: '机构' },
}
const EVI_LABEL = { explicit: '原文', inferred: '推断', similarity: '相似' }

const presentTypes = computed(() => {
  if (!overview.value) return []
  return Object.entries(overview.value.entities || [])
    .map(([t, n]) => ({ t, n, ...(TYPE_STYLE[t] || { color: '#9aa7b8', zh: t }) }))
})

// 布局计算与相机适配由独立、可测试的 graphLayout 模块负责。

// ---------- sigma 渲染 ----------
let renderer = null
let graph = null
let hoveredNode = null
let hoveredNeighbors = new Set()
let hoveredEdge = null
let focusNode = ''
let layoutBounds = null
let fullViewMode = false

function fitCamera({ full = false, duration = 250 } = {}) {
  if (!renderer || !graph || !graph.order) return
  fullViewMode = full
  const el = renderer.getContainer()
  const fitBounds = layoutBounds || (() => {
    let minX = Infinity, minY = Infinity, maxX = -Infinity, maxY = -Infinity
    graph.forEachNode((n, attr) => {
      minX = Math.min(minX, attr.x); maxX = Math.max(maxX, attr.x)
      minY = Math.min(minY, attr.y); maxY = Math.max(maxY, attr.y)
    })
    return { minX, maxX, minY, maxY }
  })()
  const fit = computeCameraFit(
    fitBounds,
    { width: el.clientWidth || 1200, height: el.clientHeight || 700 },
    { padding: 90, minRatio: 0.06, maxRatio: 40 },
  )
  // 默认视图优先保持节点可辨；“适配全图”按钮完整适配整个边界框。
  const ratio = full ? fit.ratio : Math.min(fit.ratio, 1.6)
  // Sigma v3 camera x/y live in normalized framed-graph space (roughly 0..1),
  // while layout positions and computeCameraFit centers are in raw graph space.
  // Convert explicitly or large semantic-lane coordinates move the camera off-canvas.
  const cameraCenter = toFramedCameraPoint(renderer, { x: fit.x, y: fit.y })
  renderer.getCamera().animate({ ...cameraCenter, ratio, angle: 0 }, { duration })
}

function renderGraph(data) {
  graphData.value = data
  ensureRenderer()
  const visible = data.nodes.filter(n => showDust.value || n.degree > 0 || n.is_library_paper)
  const keep = new Set(visible.map(n => n.id))
  const shownEdges = data.edges.filter(e => keep.has(e.source) && keep.has(e.target))
  nodeCount.value = visible.length
  edgeCount.value = shownEdges.length
  const layout = computeSemanticLayout(visible)
  const pos = layout.positions
  layoutBounds = layout.bounds

  hoveredNode = null
  hoveredNeighbors = new Set()
  graph.clear()
  visible.forEach(n => {
    const st = TYPE_STYLE[n.type] || { color: '#718096' }
    const isPaper = n.type === 'Paper'
    graph.addNode(n.id, {
      x: pos[n.id]?.x ?? 0, y: pos[n.id]?.y ?? 0,
      size: isPaper ? 13 : Math.min(10, 6 + Math.sqrt(n.degree || 0)),
      color: isPaper ? '#294f78' : st.color,
      label: n.label_zh || n.label,
      _color: st.color,
      _paper: isPaper,
    })
  })
  // 同一对端点可能有多种关系（例如论文既“提出”又“对比”某算法）。
  // Sigma 的普通 Graph 不允许平行边，因此按有向端点聚合成一条可视连线，
  // 原始关系 ID 全部保留，点击后在详情面板中逐条选择。
  const edgeGroups = groupRelationsByEndpoints(shownEdges)
  for (const group of edgeGroups) {
    const relations = group.relations
    const kinds = new Set(relations.map(e => e.evidence_type))
    const color = kinds.has('inferred') ? '#c47a43'
      : kinds.has('similarity') ? '#8064a9' : '#6887a8'
    const labels = [...new Set(relations.map(e =>
      (REL_ZH[e.relation_type] || e.relation_type) + ' · ' +
      (EVI_LABEL[e.evidence_type] || e.evidence_type)))]
    graph.addEdgeWithKey('pair:' + group.key, group.source, group.target, {
      size: Math.min(3, 1.15 + Math.log2(relations.length)),
      color,
      _label: labels.join(' / '),
      _rids: relations.map(e => e.id),
      _relationCount: relations.length,
    })
  }
  // Expose the completed layout (not the previous graph state) for acceptance diagnostics.
  window.__kgLastRender = {
    nodes: visible.length, edges: shownEdges.length,
    graphOrder: graph.order, graphSize: graph.size,
    sample: visible[0] ? { id: visible[0].id, x: pos[visible[0].id]?.x, y: pos[visible[0].id]?.y } : null,
    lanes: layout.lanes,
    bounds: layout.bounds,
    t: new Date().toISOString().slice(17, 23),
  }
  fitCamera({ full: false, duration: focusNode ? 0 : 250 })
  if (focusNode && graph.hasNode(focusNode)) {
    const a = graph.getNodeAttributes(focusNode)
    const cameraCenter = toFramedCameraPoint(renderer, { x: a.x, y: a.y })
    renderer.getCamera().animate({ ...cameraCenter, ratio: 0.28 }, { duration: 350 })
    focusNode = ''
  }
}

async function loadGraph() {
  loading.value = true
  try {
    const params = {}
    if (view.value === 'global') {
      if (selPaper.value) params.paper_id = selPaper.value
      else if (q.value.trim()) params.q = q.value.trim()
      const ts = [...activeTypes.value]
      if (ts.length) params.types = ts.join(',')
      if (selRelation.value) params.relation_types = selRelation.value
    }
    renderGraph(await api.kgGraph(params))
  } catch (e) {
    window.__kgGraphError = String(e?.stack || e)
    toast('图谱加载失败：' + e.message, 'err')
  } finally {
    loading.value = false
  }
}

function toggleType(t) {
  const s = new Set(activeTypes.value)
  s.has(t) ? s.delete(t) : s.add(t)
  activeTypes.value = s
  loadGraph()
}

function toggleDust() {
  showDust.value = !showDust.value
  if (graphData.value) renderGraph(graphData.value)
}

async function openTopics() {
  try {
    const data = await api.kgTopics()
    topics.value = data.topics || []
    sharedResources.value = data.shared_resources || []
  } catch (e) {
    toast('主题加载失败：' + e.message, 'err')
  }
}

async function pickTopic(t) {
  activeTopic.value = t
  panel.value = { kind: 'topic', topic: t }
  loading.value = true
  try {
    renderGraph(await api.kgGraph({ entity_ids: t.entity_ids.join(',') }))
  } finally {
    loading.value = false
  }
}

function switchView(v) {
  view.value = v
  panel.value = { kind: 'empty' }
  activeTopic.value = null
  if (v === 'topics') { openTopics() }  // 主题视图不预载全图：pickTopic 的子图请求与全图请求并发时会互相覆盖
  else if (v === 'global') loadGraph()
}

const looksLikeTestPaper = p => /\b(test|dummy|mock|fixture|sample|demo)\b|测试|示例|样例/i.test(p.title_zh || p.title || '')

async function changePaperScope(p, role) {
  try {
    await api.kgSetPaperScope(p.id, role)
    p.library_role = role
    toast(role === 'normal' ? '已恢复纳入知识图谱' : '已从知识图谱中排除；原始论文和证据均保留', 'ok')
    await Promise.all([loadOverview(), openTopics()])
    if (view.value === 'global') await loadGraph()
  } catch (e) {
    toast('分类更新失败：' + e.message, 'err')
  }
}

// ---------- 详情面板 ----------
const zhName = d => d.name_zh || d.name

async function openRelation(rid) {
  try {
    panel.value = { kind: 'relation', d: await api.kgRelation(rid) }
  } catch { /* ignore */ }
}

async function openRelationGroup(rids) {
  try {
    const results = await Promise.allSettled(rids.map(rid => api.kgRelation(rid)))
    const relations = results.filter(result => result.status === 'fulfilled' && result.value)
      .map(result => result.value)
    if (!relations.length) throw new Error('没有成功读取到关系详情')
    panel.value = { kind: 'relationGroup', relations, failedCount: results.length - relations.length }
  } catch (e) {
    toast('关系详情加载失败：' + e.message, 'err')
  }
}

async function onNodeTap(id) {
  try {
    panel.value = { kind: 'entity', d: await api.kgEntity(id) }
  } catch (e) {
    toast('实体详情加载失败：' + e.message, 'err')
  }
}

// ---------- 抽取任务 ----------
async function pollJobs() {
  try {
    const jobs = await api.kgJobs()
    const active = jobs.find(j => j.status === 'running' || j.status === 'pending')
    job.value = active
      ? { stage: active.stage || (active.status === 'pending' ? '排队中' : ''),
          progress: Math.round((active.progress || 0) * 100),
          paper: active.paper_title_zh || active.paper_title, status: active.status }
      : null
    const had = jobs.some(j => j.status === 'completed')
    if (!active) {
      clearInterval(jobsTimer.value)
      jobsTimer.value = null
      await loadOverview()
      if (had) loadGraph()
    }
  } catch { /* 网络抖动忽略 */ }
}

function watchJobs() {
  if (!jobsTimer.value) jobsTimer.value = setInterval(pollJobs, 3000)
}

async function startRebuild() {
  try {
    const r = await api.kgRebuild()
    toast(r.created ? `已为 ${r.created} 篇论文创建抽取任务` : '所有论文都已有图谱数据', r.created ? 'ok' : 'info')
    if (r.created) watchJobs()
  } catch (e) {
    toast('触发失败：' + e.message, 'err')
  }
}

async function extractOne() {
  if (!selPaper.value) return
  try {
    await api.kgExtractPaper(selPaper.value)
    toast('已开始抽取，可在下方进度查看', 'ok')
    watchJobs()
  } catch (e) {
    toast('触发失败：' + e.message, 'err')
  }
}

async function loadOverview() {
  try {
    overview.value = await api.kgOverview()
  } catch { /* ignore */ }
}

function readerUrl(pid, blockId) {
  return blockId ? `/reader/${pid}?block=${blockId}` : `/reader/${pid}`
}

function onSearch() {
  focusNode = ''
  selPaper.value = ''
  loadGraph()
}

function onGraphResize() {
  fitCamera({ full: fullViewMode, duration: 150 })
}

function ensureRenderer() {
  // 惰性初始化：默认视图是"知识总览"，画布容器在 v-show 下 display:none（宽度 0），
  // Sigma 构造时会抛 "Container has no width"。因此只在第一次真正渲染图时创建；
  // allowInvalidContainer 兜底极端时序（refresh 时会重新量测容器尺寸）。
  if (renderer) return
  graph = new Graph()
  renderer = new Sigma(graph, document.getElementById('kg-canvas'), {
    allowInvalidContainer: true,
    enableEdgeEvents: true,
    minCameraRatio: 0.06,
    maxCameraRatio: 40,
    labelRenderedSizeThreshold: 7,
    labelDensity: 0.07,
    labelGridCellSize: 110,
    nodeLabelSize: 12,
    nodeLabelColor: '#263548',
    labelWeight: '600',
    edgeLabelSize: 11,
    edgeLabelColor: '#334155',
    renderEdgeLabels: true,
    defaultDrawNodeLabel(context, data, settings) {
      if (!data.label) return
      const s = settings.nodeLabelSize || 12
      const col = data._color || '#dfe7ff'
      context.font = `${settings.labelWeight || '600'} ${s}px "PingFang SC","Microsoft YaHei",system-ui,sans-serif`
      context.strokeStyle = 'rgba(255,255,255,.96)'
      context.lineWidth = 4
      context.strokeText(data.label, data.x + data.size + 5, data.y + s / 3)
      context.fillStyle = '#263548'
      context.fillText(data.label, data.x + data.size + 4, data.y + s / 3)
    },
    defaultDrawNodeHover(context, data, settings) {
      if (!data.label) return
      const s = settings.nodeLabelSize || 13
      const col = data._color || '#dfe7ff'
      context.font = `700 ${s}px "PingFang SC","Microsoft YaHei",system-ui,sans-serif`
      const w = context.measureText(data.label).width
      const x = data.x + data.size + 6, y = data.y - s
      context.fillStyle = 'rgba(255,255,255,.98)'
      context.beginPath()
      context.roundRect(x - 6, y - 4, w + 12, s + 10, 6)
      context.fill()
      context.strokeStyle = col + '88'
      context.lineWidth = 1
      context.stroke()
      context.fillStyle = '#1f2937'
      context.fillText(data.label, x, y + s - 2)
    },
    nodeReducer(node, data) {
      const res = { ...data }
      if (hoveredNode && node !== hoveredNode && !hoveredNeighbors.has(node)) {
        res.color = '#dce3ea'
        res.label = null
      }
      return res
    },
    edgeReducer(edge, data) {
      const res = { ...data, label: null }  // 默认保持画面简洁，悬停时显示关系语义
      if (hoveredNode) {
        const [s, t] = graph.extremities(edge)
        if (s !== hoveredNode && t !== hoveredNode) {
          res.hidden = true
        } else {
          res.size = 2
          res.color = '#315f8b'
          res.label = data._label
        }
      } else if (hoveredEdge === edge) {
        res.size = 2
        res.color = '#315f8b'
        res.label = data._label
      }
      return res
    },
  })
  renderer.on('clickNode', ({ node }) => onNodeTap(node))
  renderer.on('clickEdge', ({ edge }) => {
    const rids = graph.getEdgeAttribute(edge, '_rids') || []
    if (rids.length === 1) openRelation(rids[0])
    else if (rids.length > 1) openRelationGroup(rids)
  })
  renderer.on('enterNode', ({ node }) => {
    hoveredNode = node
    hoveredNeighbors = new Set(graph.neighbors(node))
    hoveredEdge = null
    renderer.refresh({ skipIndexation: true })
  })
  renderer.on('leaveNode', () => {
    hoveredNode = null
    hoveredNeighbors = new Set()
    renderer.refresh({ skipIndexation: true })
  })
  renderer.on('enterEdge', ({ edge }) => {
    if (!hoveredNode) { hoveredEdge = edge; renderer.refresh({ skipIndexation: true }) }
  })
  renderer.on('leaveEdge', () => {
    if (hoveredEdge) { hoveredEdge = null; renderer.refresh({ skipIndexation: true }) }
  })
}

onMounted(async () => {
  window.addEventListener('resize', onGraphResize)
  try {
    await Promise.all([loadOverview(), openTopics()])
    papers.value = await api.papers().catch(() => [])
    const jobs = await api.kgJobs().catch(() => [])
    if (jobs.some(j => j.status === 'running' || j.status === 'pending')) watchJobs()
  } catch (e) {
    window.__kgInitError = String(e?.stack || e)
    toast('初始化失败：' + (e?.message || e), 'err')
  }
})

onBeforeUnmount(() => {
  window.removeEventListener('resize', onGraphResize)
  if (jobsTimer.value) clearInterval(jobsTimer.value)
  if (renderer) { renderer.kill(); renderer = null }
})
</script>

<template>
  <div class="kg-page">
    <div class="kg-head">
      <div>
        <h1 class="page-title">科研知识地图</h1>
        <p class="page-sub" v-if="overview">
          {{ overview.entities_total }} 个知识点 · {{ overview.relations_total }} 条关系 ·
          已纳入 {{ overview.papers_extracted }}/{{ overview.papers_total }} 篇论文 ·
          每条关系均可追溯到论文证据<template v-if="overview.inferred_relations || overview.similarity_relations">
           （AI 推断 {{ overview.inferred_relations || 0 }} 条 · 语义相似 {{ overview.similarity_relations || 0 }} 条）</template>
        </p>
      </div>
      <div class="kg-actions">
        <div class="seg">
          <button :class="{ on: view === 'overview' }" @click="switchView('overview')">知识总览</button>
          <button :class="{ on: view === 'topics' }" @click="switchView('topics')">研究主题</button>
          <button :class="{ on: view === 'global' }" @click="switchView('global')">关系探索</button>
        </div>
        <button class="btn sm" @click="startRebuild" title="为还没有图谱数据的论文自动抽取">⚗ 抽取缺失论文</button>
      </div>
    </div>

    <div v-if="job" class="kg-jobbar">
      <span v-if="job.paper">{{ job.paper }}</span> {{ job.stage || '抽取中' }}
      <div class="progressbar"><div :style="{ width: job.progress + '%' }"></div></div>
      <span>{{ job.progress }}%</span>
    </div>

    <div v-if="view === 'global'" class="kg-toolbar">
      <select class="select" v-model="selPaper" @change="q = ''; loadGraph()" style="max-width:300px">
        <option value="">全部论文（星系全景）</option>
        <option v-for="p in papers" :key="p.id" :value="p.id">
          只看：{{ p.title_zh || p.title }}
        </option>
      </select>
      <input class="input" v-model="q" style="max-width:220px"
             placeholder="搜索知识点，回车定位" @keyup.enter="onSearch" />
      <button class="btn sm" :disabled="loading" @click="loadGraph">刷新</button>
      <button v-if="selPaper" class="btn sm primary" @click="extractOne">重抽此论文</button>
      <label class="dust-toggle" @click.prevent="toggleDust">
        <span class="box" :class="{ on: showDust }"></span>显示孤立知识点（{{ (overview?.entities_total || 0) - (nodeCount || 0) }} 个）
      </label>
      <div class="type-chips">
        <button v-for="t in presentTypes" :key="t.t" class="chip"
                :style="{ '--c': t.color }" :class="{ off: !activeTypes.has(t.t) }"
                :title="t.zh" @click="toggleType(t.t)">
          {{ t.zh }} <span class="cnt">{{ t.n }}</span>
        </button>
      </div>
      <select class="select" v-model="selRelation" @change="loadGraph()" style="max-width:160px">
        <option value="">全部关系</option>
        <option v-for="r in REL_OPTIONS" :key="r" :value="r">{{ REL_ZH[r] }}</option>
      </select>
    </div>

    <section v-if="view === 'overview'" class="kg-overview">
      <div class="kg-metrics">
        <article class="kg-metric"><span>纳入论文</span><strong>{{ overview?.papers_total || 0 }}</strong><small>仅统计正常文献</small></article>
        <article class="kg-metric"><span>已建立知识结构</span><strong>{{ overview?.papers_extracted || 0 }}</strong><small>有可追溯的抽取证据</small></article>
        <article class="kg-metric"><span>知识实体</span><strong>{{ overview?.entities_total || 0 }}</strong><small>方法、问题、数据集等</small></article>
        <article class="kg-metric"><span>有证据的关系</span><strong>{{ overview?.relations_total || 0 }}</strong><small>推断关系 {{ overview?.inferred_relations || 0 }} 条推断 · {{ overview?.similarity_relations || 0 }} 条相似，分别标记</small></article>
      </div>
      <div class="kg-overview-grid">
        <section class="kg-overview-card">
          <div class="kg-card-head"><h3>研究主题</h3><button class="btn sm" @click="switchView('topics')">查看全部</button></div>
          <p v-if="!topics.length" class="muted">目前还没有足够的明确研究领域关系形成主题。系统不会把“共同使用 CIFAR-10”误判成同一个研究方向。</p>
          <button v-for="t in topics.slice(0,6)" :key="t.name" class="kg-overview-topic" @click="switchView('topics'); pickTopic(t)">
            <span><b>{{ t.name_zh || t.name }}</b><small>{{ t.paper_count }} 篇论文 · {{ t.entity_ids.length }} 个相关知识点</small></span><span>›</span>
          </button>
        </section>
        <section class="kg-overview-card">
          <div class="kg-card-head"><h3>共享实验资源</h3><span class="muted">不是研究主题</span></div>
          <p v-if="!sharedResources.length" class="muted">暂未发现被多篇论文共同使用的数据集。</p>
          <div v-for="d in sharedResources.slice(0,6)" :key="d.id" class="kg-resource-row"><b>{{ d.name }}</b><span>{{ d.paper_count }} 篇论文使用</span></div>
        </section>
      </div>
      <section class="kg-overview-card kg-paper-audit">
        <div class="kg-card-head"><div><h3>论文纳入范围</h3><p class="muted">可将测试论文或演示数据排除出知识图谱。这里只改变图谱分类，不删除论文、原文或证据。</p></div><button class="btn sm" @click="startRebuild">抽取缺失论文</button></div>
        <div v-for="p in papers" :key="p.id" class="kg-paper-row">
          <div class="kg-paper-title"><b>{{ p.title_zh || p.title }}</b><small v-if="looksLikeTestPaper(p)" class="kg-review-flag">标题疑似测试数据，请人工确认</small><small>{{ p.year || '年份未知' }} · {{ p.status }}</small></div>
          <select class="select kg-scope-select" :value="p.library_role || 'normal'" @change="changePaperScope(p, $event.target.value)">
            <option value="normal">正常文献（纳入）</option><option value="test">测试数据（排除）</option><option value="excluded">其他排除项</option>
          </select>
        </div>
      </section>
    </section>

    <div v-show="view !== 'overview'" class="kg-body">
      <div v-if="view === 'topics'" class="kg-topiclist">
        <div v-if="!topics.length" class="muted" style="padding:12px">
          还没有明确主题。先抽取论文并形成 ResearchArea 关系；共享数据集只代表实验资源重合，不会被伪装成研究主题。</div>
        <div v-for="(t, i) in topics" :key="i" class="topic-item"
             :class="{ on: activeTopic === t }" @click="pickTopic(t)">
          <div class="t-name">{{ t.name_zh || t.name }}</div>
          <div class="t-meta">{{ t.paper_count }} 篇论文 · {{ t.entity_ids.length }} 个知识点</div>
        </div>
      </div>

      <div class="kg-canvas-wrap">
        <div class="kg-canvas-controls">
          <button class="btn sm" type="button" @click="fitCamera({ full: true })">适配全图</button>
          <button class="btn sm" type="button" @click="fitCamera({ full: false })">可读视图</button>
        </div>
        <div id="kg-canvas"></div>
        <div v-if="loading" class="kg-loading">知识关系图加载中…</div>
        <div v-if="graphData && !graphData.nodes.length && !loading" class="kg-empty">
          还没有知识关系图。<br>点右上「⚗ 抽取缺失论文」，系统会从已纳入论文建立知识网络。<br>
          <span class="muted">每篇论文几分钟；之后新导入的论文自动加入。</span>
        </div>
        <div class="kg-legend">
          <span v-for="(st, t) in TYPE_STYLE" :key="t" class="lg-item" :title="t">
            <i :style="{ background: st.color }"></i>{{ st.zh }}
          </span>
          <span class="lg-item"><i class="lg-line"></i>蓝线 = 论文原话</span>
          <span class="lg-item"><i class="lg-line inf"></i>橙线 = AI 推断</span>
          <span class="lg-item"><i class="lg-line sim"></i>紫线 = 语义相似（不代表技术继承）</span>
        </div>
        <div class="kg-tip">位置仅表示知识类别 · 悬停查看关系 · 点击查看原文证据</div>
      </div>

      <aside class="kg-panel">
        <!-- 实体详情 -->
        <template v-if="panel.kind === 'entity'">
          <div class="p-head">
            <span class="badge" :style="{ background: (TYPE_STYLE[panel.d.entity_type] || {}).color + '22', color: (TYPE_STYLE[panel.d.entity_type] || {}).color }">
              {{ (TYPE_STYLE[panel.d.entity_type] || {}).zh || panel.d.entity_type }}
            </span>
            <h3>{{ zhName(panel.d) }}</h3>
            <div v-if="panel.d.name_zh && panel.d.name_zh !== panel.d.name" class="muted en-name">{{ panel.d.name }}</div>
            <router-link v-if="panel.d.library_paper_id" class="btn sm" :to="readerUrl(panel.d.library_paper_id)">
              ↗ 打开这篇论文
            </router-link>
          </div>
          <p v-if="panel.d.description" class="p-desc">{{ panel.d.description }}</p>
          <p v-if="panel.d.aliases.length" class="p-alias">别名：{{ panel.d.aliases.join('、') }}</p>
          <div class="p-sec">
            <h4>关系（{{ panel.d.relations.length }}）</h4>
            <div v-for="r in panel.d.relations" :key="r.id" class="rel-item" @click="openRelation(r.id)">
              <span class="rel-dir">
                {{ r.source_name_zh || r.source_name }}
                <b class="rel-type" :class="{ inf: r.evidence_type !== 'explicit' }">{{ REL_ZH[r.relation_type] || r.relation_type }}</b>
                {{ r.target_name_zh || r.target_name }}
              </span>
              <span class="muted">{{ r.evidence_count }} 条证据</span>
            </div>
            <div v-if="!panel.d.relations.length" class="muted">暂无</div>
          </div>
          <div class="p-sec">
            <h4>原文证据（{{ panel.d.evidence.length }}）</h4>
            <div v-for="e in panel.d.evidence" :key="e.id" class="ev-item">
              <div class="ev-meta">
                <span class="ev-paper">{{ e.paper_title }}</span>
                <span v-if="e.section_name" class="muted">· {{ e.section_name }}</span>
                <span class="ev-badge" :class="e.evidence_type">{{ EVI_LABEL[e.evidence_type] || e.evidence_type }}</span>
              </div>
              <div class="ev-text">“{{ e.evidence_text }}”</div>
              <a v-if="!e.stale" class="ev-jump" :href="readerUrl(e.paper_id, e.block_id)" target="_blank">定位原文 ↗</a>
              <span v-else class="muted" style="font-size:12px">原文已重解析，待重新抽取后可定位</span>
            </div>
            <div v-if="!panel.d.evidence.length" class="muted">暂无</div>
          </div>
        </template>

        <!-- 关系详情 -->
        <template v-else-if="panel.kind === 'relation'">
          <div class="p-head">
            <h3 style="font-size:15px">
              {{ zhName(panel.d.source) }}
              <b class="rel-type" :class="{ inf: panel.d.evidence_type !== 'explicit' }">{{ REL_ZH[panel.d.relation_type] || panel.d.relation_type }}</b>
              {{ zhName(panel.d.target) }}</h3>
            <div class="muted" style="font-size:12px">
              <span :class="{ 'ev-warn': panel.d.evidence_type !== 'explicit' }">
                {{ EVI_LABEL[panel.d.evidence_type] || panel.d.evidence_type }}</span>
              <template v-if="panel.d.evidence_type === 'inferred'">（AI 根据论文上下文推断，需结合原文判断）</template>
              <template v-else-if="panel.d.evidence_type === 'similarity'">（语义相似，不代表技术继承或因果关系）</template>
            </div>
          </div>
          <div class="p-sec">
            <h4>原文证据（{{ panel.d.evidence.length }}）</h4>
            <div v-for="e in panel.d.evidence" :key="e.id" class="ev-item">
              <div class="ev-meta">
                <span class="ev-paper">{{ e.paper_title }}</span>
                <span v-if="e.section_name" class="muted">· {{ e.section_name }}</span>
              </div>
              <div class="ev-text">“{{ e.evidence_text }}”</div>
              <a v-if="!e.stale" class="ev-jump" :href="readerUrl(e.paper_id, e.block_id)" target="_blank">定位原文 ↗</a>
              <span v-else class="muted" style="font-size:12px">原文已重解析，待重新抽取后可定位</span>
            </div>
          </div>
        </template>

        <!-- 同一对实体之间的多种关系：保留每条原始关系和证据入口 -->
        <template v-else-if="panel.kind === 'relationGroup'">
          <div v-if="panel.failedCount" class="kg-partial-warning">
            有 {{ panel.failedCount }} 条关系详情读取失败；其余已成功加载的关系仍可查看。
          </div>
          <div class="p-head">
            <span class="badge">多重关系</span>
            <h3>这两个知识点之间有 {{ panel.relations.length }} 种关系</h3>
            <p class="p-desc">图中将同一对节点的连线合并显示，以避免重叠；这里仍保留每条关系的独立语义和原文证据。</p>
          </div>
          <div class="p-sec">
            <div v-for="r in panel.relations" :key="r.id" class="rel-item relation-group-item" @click="openRelation(r.id)">
              <span class="rel-dir">
                <b class="rel-type" :class="{ inf: r.evidence_type !== 'explicit' }">{{ REL_ZH[r.relation_type] || r.relation_type }}</b>
                <small>{{ EVI_LABEL[r.evidence_type] || r.evidence_type }} · {{ r.evidence.length }} 条证据</small>
              </span>
              <span>查看证据 ›</span>
            </div>
          </div>
        </template>

        <!-- 主题详情 -->
        <template v-else-if="panel.kind === 'topic'">
          <div class="p-head"><h3>{{ panel.topic.name_zh || panel.topic.name }}</h3>
            <div class="muted" style="font-size:12px">{{ panel.topic.paper_count }} 篇论文</div></div>
          <div class="p-sec">
            <h4>论文</h4>
            <div v-for="pid in panel.topic.paper_ids" :key="pid" class="rel-item">
              <router-link :to="readerUrl(pid)" style="flex:1">
                {{ (papers.find(p => p.id === pid) || {}).title_zh || (papers.find(p => p.id === pid) || {}).title || pid }}
              </router-link>
            </div>
          </div>
          <div class="p-sec">
            <h4>关键关系</h4>
            <div v-for="r in panel.topic.key_relations" :key="r.id" class="rel-item" @click="openRelation(r.id)">
              <span class="rel-dir">{{ r.source_name }} <b class="rel-type"
                :class="{ inf: r.evidence_type !== 'explicit' }">{{ REL_ZH[r.relation_type] || r.relation_type }}</b> {{ r.target_name }}</span>
            </div>
            <div v-if="!panel.topic.key_relations.length" class="muted">暂无</div>
          </div>
        </template>

        <div v-else class="kg-panel-hint">
          <p><b>这张图怎么读</b></p>
          <ul>
            <li>节点按类别分列：论文、研究问题、算法、模型与概念、实验资源、研究发现等</li>
            <li><b>悬停</b>任意节点：只点亮与它直接相关的连线，线上标出中文关系名和置信度</li>
            <li><b>点击</b>节点或连线 → 右栏看详情和<b>论文原句证据</b>，可一键跳回原文位置</li>
            <li><b>橙线</b> = AI 推断（论文没直说），<b>蓝线</b> = 论文原话，绝不混淆</li>
            <li>同一对节点存在多种关系时，画面合并连线但保留全部关系；点击连线可逐条查看证据</li>
            <li>滚轮缩放、拖动平移，用左侧搜索可以定位某个知识点</li>
          </ul>
          <p class="muted" style="font-size:12px;margin-top:10px">
            图谱完全由论文原文抽取，没有证据的内容不会出现在这里。</p>
        </div>
      </aside>
    </div>
  </div>
</template>

<style scoped>
.kg-page{display:flex;flex-direction:column;min-height:calc(100vh - 60px);color:var(--text,#253449)}
.kg-head{display:flex;justify-content:space-between;align-items:flex-start;gap:16px}
.kg-actions{display:flex;gap:8px;align-items:center;padding-top:6px}
.kg-jobbar{display:flex;align-items:center;gap:10px;margin-top:10px;padding:7px 14px;background:var(--accent-soft);border-radius:8px;font-size:13px;color:var(--accent)}
.kg-jobbar .progressbar{width:160px}
.kg-toolbar{display:flex;flex-wrap:wrap;gap:8px;align-items:center;margin:14px 0 10px}
.type-chips{display:flex;flex-wrap:wrap;gap:5px}
.chip{--c:#999;display:inline-flex;align-items:center;gap:4px;padding:3px 9px;border-radius:14px;border:1px solid var(--line);background:#fff;cursor:pointer;font-size:12px;color:var(--c);font-weight:600}
.chip .cnt{color:var(--ink-3);font-weight:400}.chip.off{opacity:.38;filter:grayscale(.6)}
.dust-toggle{display:inline-flex;align-items:center;gap:6px;font-size:12.5px;color:var(--ink-2);cursor:pointer;user-select:none}
.dust-toggle .box{width:14px;height:14px;border-radius:4px;border:1px solid var(--line);background:#fff;display:inline-block}
.dust-toggle .box.on{background:var(--accent);border-color:var(--accent)}
.kg-body{display:flex;gap:14px;flex:1;min-height:0}
.kg-topiclist{width:230px;flex-shrink:0;overflow:auto}
.topic-item{padding:10px 12px;border:1px solid var(--line);border-radius:8px;margin-bottom:8px;cursor:pointer;background:var(--panel)}
.topic-item:hover,.topic-item.on{border-color:var(--accent)}.topic-item.on{background:var(--accent-soft)}
.t-name{font-weight:600;font-size:13.5px}.t-meta{font-size:12px;color:var(--ink-3);margin-top:2px}
.kg-canvas-wrap{position:relative;flex:1;min-width:0;height:calc(100vh - 265px);min-height:460px;border-radius:12px;overflow:hidden;background:linear-gradient(rgba(94,116,142,.045) 1px,transparent 1px),linear-gradient(90deg,rgba(94,116,142,.045) 1px,transparent 1px),linear-gradient(180deg,#fbfcfe 0%,#f4f7fb 100%);background-size:32px 32px,32px 32px,auto;border:1px solid #d8e1eb;box-shadow:inset 0 1px 0 rgba(255,255,255,.9),0 8px 24px rgba(35,55,80,.06)}
#kg-canvas{position:absolute;inset:0;z-index:1}
.kg-canvas-controls{position:absolute;top:10px;right:12px;z-index:3;display:flex;gap:6px}
.kg-canvas-controls .btn{background:rgba(255,255,255,.96);border:1px solid #d8e1eb;color:#334155;box-shadow:0 1px 3px rgba(35,55,80,.08)}
.kg-loading{position:absolute;top:12px;left:16px;z-index:2;font-size:12px;color:#526174;background:rgba(255,255,255,.92);padding:4px 8px;border-radius:6px}
.kg-empty{position:absolute;inset:0;z-index:2;display:flex;flex-direction:column;align-items:center;justify-content:center;text-align:center;color:#526174;line-height:2;font-size:14px;padding:24px}
.kg-legend{position:absolute;bottom:12px;left:14px;right:14px;z-index:2;display:flex;flex-wrap:wrap;gap:10px;font-size:11.5px;color:#526174;background:rgba(255,255,255,.94);border:1px solid #dbe3ec;border-radius:8px;padding:7px 10px;pointer-events:none}
.lg-item{display:inline-flex;align-items:center;gap:5px}.lg-item i{width:10px;height:10px;border-radius:50%;display:inline-block}
.lg-line{width:16px!important;height:0!important;border-radius:0!important;border-top:1.5px solid #6887a8}
.lg-line.inf{border-top-color:#c47a43}.lg-line.sim{border-top-color:#8064a9}
.kg-tip{position:absolute;top:48px;left:16px;z-index:2;font-size:12px;color:#526174;background:rgba(255,255,255,.94);border:1px solid #dbe3ec;padding:4px 10px;border-radius:12px;pointer-events:none}
.kg-panel{width:360px;flex-shrink:0;overflow:auto;background:#fff;color:#253449;border:1px solid #dbe3ec;border-radius:12px;padding:16px;box-shadow:0 8px 24px rgba(35,55,80,.06)}
.kg-panel-hint{font-size:13px;line-height:1.9;color:#526174}.kg-panel-hint ul{margin:6px 0;padding-left:18px}
.kg-panel .muted{color:#718096!important}.p-head h3{margin:8px 0 6px;font-size:16px;color:#1e293b}
.en-name{font-size:12px}.p-desc{font-size:13px;color:#526174;margin:6px 0}.p-alias{font-size:12.5px;color:#718096;margin:4px 0}
.p-sec{margin-top:14px}.p-sec h4{margin:0 0 8px;font-size:13px;color:#68788c;font-weight:600}
.rel-item{display:flex;justify-content:space-between;gap:8px;align-items:center;padding:8px;border-radius:7px;cursor:pointer;font-size:12.8px;color:#334155}
.rel-item:hover{background:#f1f5f9}.rel-dir{flex:1;min-width:0}.rel-type{color:#315f8b;font-size:11px;padding:0 3px;font-weight:700}.rel-type.inf{color:#b65d36}
.ev-warn{color:#b65d36}.ev-item{border-left:3px solid #8ca5bf;background:#f8fafc;border-radius:0 7px 7px 0;padding:8px 10px;margin-bottom:10px}
.ev-meta{font-size:12px;color:#718096;display:flex;gap:6px;flex-wrap:wrap}.ev-paper{color:#334b66;font-weight:600}
.ev-badge{padding:0 6px;border-radius:8px;background:#eaf1f8;color:#315f8b;font-size:11px}.ev-badge.inferred{background:#fff1e8;color:#a85a32}
.ev-text{font-size:12.8px;color:#334155;margin:4px 0;line-height:1.6}.ev-jump{font-size:12px;color:#315f8b!important}
.seg{display:inline-flex;border:1px solid var(--line);border-radius:8px;overflow:hidden;background:var(--panel)}
.seg button{border:0;background:transparent;padding:6px 14px;font-size:13px;cursor:pointer;color:var(--ink-2)}
.seg button.on{background:var(--accent-soft);color:var(--accent);font-weight:600}
.progressbar{height:6px;border-radius:4px;background:#e5e3dc;overflow:hidden}.progressbar>div{height:100%;background:var(--accent);transition:width .4s}
.muted{color:var(--ink-3)}
.kg-overview{display:flex;flex-direction:column;gap:16px;padding:18px 0 28px;color:var(--text,#202938)}
.kg-metrics{display:grid;grid-template-columns:repeat(4,minmax(0,1fr));gap:12px}
.kg-metric,.kg-overview-card{background:var(--card,#fff);border:1px solid var(--line,#e5e9ef);border-radius:12px}
.kg-metric{padding:16px 18px;display:flex;flex-direction:column;gap:5px}.kg-metric span{color:var(--muted,#77808e);font-size:12px}
.kg-metric strong{font-size:28px;line-height:1.2;font-weight:700;color:var(--text,#202938)}
.kg-metric small,.kg-overview-topic small,.kg-paper-title small{color:var(--muted,#77808e);font-size:11px}
.kg-overview-grid{display:grid;grid-template-columns:1fr 1fr;gap:12px}.kg-overview-card{padding:16px;min-width:0}
.kg-card-head{display:flex;align-items:center;justify-content:space-between;gap:12px;margin-bottom:12px}
.kg-card-head h3{margin:0;font-size:15px;font-weight:700}.kg-card-head p{margin:5px 0 0;font-size:12px}
.kg-overview-topic{width:100%;border:0;border-top:1px solid var(--line,#e5e9ef);background:transparent;padding:12px 2px;display:flex;align-items:center;justify-content:space-between;text-align:left;color:inherit;cursor:pointer}
.kg-overview-topic:hover{background:var(--hover,#f7f9fc)}.kg-overview-topic span:first-child{display:flex;flex-direction:column;gap:4px}
.kg-overview-topic span:last-child{font-size:20px;color:var(--muted,#77808e)}
.kg-resource-row{display:flex;justify-content:space-between;gap:10px;padding:11px 2px;border-top:1px solid var(--line,#e5e9ef);font-size:13px}
.kg-resource-row span{color:var(--muted,#77808e);font-size:12px}.kg-paper-audit{padding-bottom:8px}
.kg-paper-row{display:flex;align-items:center;justify-content:space-between;gap:16px;border-top:1px solid var(--line,#e5e9ef);padding:10px 2px}
.kg-paper-title{min-width:0;display:flex;flex-direction:column;gap:3px}
.kg-paper-title b{overflow:hidden;text-overflow:ellipsis;white-space:nowrap;font-size:13px}
.kg-scope-select{width:190px;flex:none}.kg-review-flag{color:#a15b19!important}
.relation-group-item{border:1px solid #e5ebf2;border-radius:8px;margin:8px 0;padding:12px}
.relation-group-item small{display:block;color:#718096;margin-top:4px}
.kg-partial-warning{padding:8px 10px;border:1px solid #f0d6a8;background:#fff8e8;color:#8a5a16;border-radius:7px;font-size:12px}
@media(max-width:1100px){.kg-body{flex-wrap:wrap}.kg-canvas-wrap{flex-basis:100%;order:1}.kg-topiclist{width:100%;max-height:180px;order:0}.kg-panel{width:100%;max-height:420px;order:2}}
@media(max-width:900px){.kg-metrics{grid-template-columns:repeat(2,minmax(0,1fr))}.kg-overview-grid{grid-template-columns:1fr}}
@media(max-width:600px){.kg-head{flex-direction:column}.kg-actions{width:100%;flex-wrap:wrap}.kg-paper-row{align-items:flex-start;flex-direction:column}.kg-scope-select{width:100%}.kg-canvas-wrap{min-height:380px;height:55vh}.kg-legend{bottom:8px;left:8px;right:8px;gap:6px;font-size:10px}.kg-tip{display:none}.kg-canvas-controls{top:8px;right:8px}}
</style>
