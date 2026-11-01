<script setup>
// 知识图谱：全局网络 / 主题视图 / 实体·关系详情与原文证据定位。
// 只做浏览与探索：抽取在后台任务里跑，页面轮询任务状态并自动刷新。
import { computed, onBeforeUnmount, onMounted, ref } from 'vue'
import cytoscape from 'cytoscape'
import api from '../api'
import { toast } from '../stores'

const overview = ref(null)
const papers = ref([])
const view = ref('global')            // global | topics
const selPaper = ref('')              // 单篇聚焦
const q = ref('')
const activeTypes = ref(new Set())    // 实体类型过滤
const selRelation = ref('')           // 关系类型过滤
const graphData = ref(null)
const loading = ref(false)
const panel = ref({ kind: 'empty' })  // empty | entity | relation | topic
const topics = ref([])
const activeTopic = ref(null)
const job = ref(null)                 // 进行中的抽取任务（用于进度条）
const jobsTimer = ref(null)

const REL_TYPES = ['PROPOSES', 'ADDRESSES', 'USES', 'USES_METRIC', 'BELONGS_TO',
  'AUTHORED_BY', 'PUBLISHED_IN', 'REPORTS', 'MITIGATES', 'IMPROVES',
  'EVALUATED_ON', 'USED_FOR', 'COMPARES', 'EXTENDS']

const TYPE_STYLE = {
  Paper: { color: '#2f5e8e', shape: 'round-rectangle', zh: '论文' },
  Algorithm: { color: '#3d7d4f', shape: 'ellipse', zh: '算法' },
  ResearchProblem: { color: '#b3423a', shape: 'diamond', zh: '研究问题' },
  Dataset: { color: '#a06014', shape: 'square', zh: '数据集' },
  Model: { color: '#5a4f9e', shape: 'round-rectangle', zh: '模型' },
  Metric: { color: '#7a6a3a', shape: 'ellipse', zh: '指标' },
  Concept: { color: '#3a7d8c', shape: 'diamond', zh: '概念' },
  Finding: { color: '#8c3a6e', shape: 'star', zh: '发现' },
  ResearchArea: { color: '#286e60', shape: 'hexagon', zh: '领域' },
  Author: { color: '#6b7280', shape: 'ellipse', zh: '作者' },
  Venue: { color: '#8b6f47', shape: 'tag', zh: '期刊会议' },
  Task: { color: '#4a6fa5', shape: 'ellipse', zh: '任务' },
  Institution: { color: '#8b93a0', shape: 'ellipse', zh: '机构' },
}
const EVI_LABEL = { explicit: '原文', inferred: '推断', similarity: '相似' }

const presentTypes = computed(() => {
  if (!overview.value) return []
  return Object.entries(overview.value.entities || [])
    .map(([t, n]) => ({ t, n, ...(TYPE_STYLE[t] || { color: '#999', zh: t }) }))
})

let cy = null

function renderGraph(data) {
  graphData.value = data
  if (!cy) return
  const els = [
    ...data.nodes.map(n => ({
      data: {
        id: n.id, label: n.label, ntype: n.type, degree: n.degree,
        lib: !!n.is_library_paper, desc: n.description,
      },
    })),
    ...data.edges.map(e => ({
      data: {
        id: 'e_' + e.id, source: e.source, target: e.target,
        rtype: e.relation_type, etype: e.evidence_type,
        conf: e.confidence, rid: e.id, evc: e.evidence_count,
      },
    })),
  ]
  cy.elements().remove()
  cy.add(els)
  cy.nodes().forEach(n => {
    const st = TYPE_STYLE[n.data('ntype')] || { color: '#999', shape: 'ellipse' }
    n.style('background-color', st.color)
    n.style('shape', st.shape)
  })
  const layout = cy.elements().layout({
    name: 'cose', animate: false, randomize: true,
    idealEdgeLength: () => 90, nodeOverlap: 12, nodeRepulsion: () => 9000,
  })
  layout.run()
  cy.fit(undefined, 40)
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

async function openTopics() {
  try {
    topics.value = (await api.kgTopics()).topics
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
  if (v === 'topics') openTopics()
  else loadGraph()
}

// ---------- 详情面板 ----------

async function onNodeTap(evt) {
  const id = evt.target.data('id')
  try {
    const d = await api.kgEntity(id)
    panel.value = { kind: 'entity', d }
  } catch (e) {
    toast('实体详情加载失败：' + e.message, 'err')
  }
}

async function onEdgeTap(evt) {
  const rid = evt.target.data('rid')
  try {
    panel.value = { kind: 'relation', d: await api.kgRelation(rid) }
  } catch (e) {
    toast('关系详情加载失败：' + e.message, 'err')
  }
}

async function openRelation(rid) {
  try {
    panel.value = { kind: 'relation', d: await api.kgRelation(rid) }
  } catch { /* ignore */ }
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
      if (had) loadGraph()  // 任务全部结束后刷新当前视图
    }
  } catch { /* 网络抖动忽略，下一轮再取 */ }
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

onMounted(async () => {
  cy = cytoscape({
    container: document.getElementById('kg-canvas'),
    style: [
      {
        selector: 'node',
        style: {
          label: 'data(label)', 'font-size': 11, color: '#23272d',
          'background-color': '#999', width: 26, height: 26,
          'border-width': 1, 'border-color': '#fff',
          'text-valign': 'bottom', 'text-margin-y': 4,
          'text-wrap': 'ellipsis', 'text-max-width': 110,
          'min-zoomed-font-size': 6,
        },
      },
      {
        selector: 'node[lib]',
        style: { 'border-width': 2.5, 'border-color': '#2f5e8e', width: 34, height: 34, 'font-size': 12 },
      },
      {
        selector: 'edge',
        style: {
          width: 1.6, 'line-color': '#b9b4a8', 'curve-style': 'bezier',
          label: 'data(rtype)', 'font-size': 8.5, 'color': '#8b93a0',
          'text-background-color': '#f6f5f1', 'text-background-opacity': 0.85,
          'text-background-padding': 1, 'min-zoomed-font-size': 9,
        },
      },
      { selector: 'edge[etype="inferred"]', style: { 'line-style': 'dashed', 'line-color': '#b3423a' } },
      { selector: 'edge[etype="similarity"]', style: { 'line-style': 'dotted', 'line-color': '#8b93a0' } },
      { selector: 'node:selected', style: { 'overlay-color': '#eaf1f8', 'overlay-opacity': 0.5 } },
      { selector: 'edge:selected', style: { width: 3, 'line-color': '#2f5e8e' } },
    ],
    wheelSensitivity: 0.25,
  })
  cy.on('tap', 'node', onNodeTap)
  cy.on('tap', 'edge', onEdgeTap)
  await Promise.all([loadOverview(), loadGraph(), openTopics()])
  const jobs = await api.kgJobs().catch(() => [])
  if (jobs.some(j => j.status === 'running' || j.status === 'pending')) watchJobs()
  papers.value = await api.papers().catch(() => [])
})

onBeforeUnmount(() => {
  if (jobsTimer.value) clearInterval(jobsTimer.value)
  if (cy) { cy.destroy(); cy = null }
})
</script>

<template>
  <div class="kg-page">
    <div class="kg-head">
      <div>
        <h1 class="page-title">知识图谱</h1>
        <p class="page-sub" v-if="overview">
          {{ overview.entities_total }} 个实体 · {{ overview.relations_total }} 条关系 ·
          覆盖 {{ overview.papers_extracted }}/{{ overview.papers_total }} 篇论文 ·
          每条关系都可回溯原文证据<template v-if="overview.inferred_relations">
            （{{ overview.inferred_relations }} 条为推断，虚线显示）</template>
        </p>
      </div>
      <div class="kg-actions">
        <div class="seg">
          <button :class="{ on: view === 'global' }" @click="switchView('global')">全局网络</button>
          <button :class="{ on: view === 'topics' }" @click="switchView('topics')">主题视图</button>
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
        <option value="">全部论文（全局核心网络）</option>
        <option v-for="p in papers" :key="p.id" :value="p.id">
          单篇：{{ p.title_zh || p.title }}
        </option>
      </select>
      <input class="input" v-model="q" style="max-width:220px"
             placeholder="搜实体名，回车定位" @keyup.enter="selPaper = ''; loadGraph()" />
      <button class="btn sm" :disabled="loading" @click="loadGraph">刷新</button>
      <button v-if="selPaper" class="btn sm primary" @click="extractOne">重抽此论文</button>
      <div class="type-chips">
        <button v-for="t in presentTypes" :key="t.t" class="chip"
                :style="{ '--c': t.color }" :class="{ off: !activeTypes.has(t.t) }"
                :title="t.zh" @click="toggleType(t.t)">
          {{ t.zh }} <span class="cnt">{{ t.n }}</span>
        </button>
      </div>
      <select class="select" v-model="selRelation" @change="loadGraph()" style="max-width:170px">
        <option value="">全部关系类型</option>
        <option v-for="r in REL_TYPES" :key="r" :value="r">{{ r }}</option>
      </select>
    </div>

    <div class="kg-body">
      <div v-if="view === 'topics'" class="kg-topiclist">
        <div v-if="!topics.length" class="muted" style="padding:12px">
          还没有主题。先在全局视图抽取论文，研究领域实体出现后会自动聚类。</div>
        <div v-for="(t, i) in topics" :key="i" class="topic-item"
             :class="{ on: activeTopic === t }" @click="pickTopic(t)">
          <div class="t-name">{{ t.name }}</div>
          <div class="t-meta">{{ t.paper_count }} 篇论文 · {{ t.entity_ids.length }} 个核心实体</div>
        </div>
      </div>

      <div class="kg-canvas-wrap">
        <div id="kg-canvas"></div>
        <div v-if="loading" class="kg-loading">加载中…</div>
        <div v-if="graphData && !graphData.nodes.length && !loading" class="kg-empty">
          还没有图谱数据。<br>点右上「⚗ 抽取缺失论文」，等任务跑完就能看到知识网络。<br>
          <span class="muted">抽取按章节调用 AI，每篇论文几分钟；之后新导入的论文会自动抽取。</span>
        </div>
        <div class="kg-legend">
          <span v-for="(st, t) in TYPE_STYLE" :key="t" class="lg-item" :title="t">
            <i :style="{ background: st.color }"></i>{{ st.zh }}
          </span>
          <span class="lg-item"><i class="lg-line"></i>实线=原文</span>
          <span class="lg-item"><i class="lg-line dash"></i>虚线=推断</span>
        </div>
      </div>

      <aside class="kg-panel">
        <!-- 实体详情 -->
        <template v-if="panel.kind === 'entity'">
          <div class="p-head">
            <span class="badge" :style="{ background: (TYPE_STYLE[panel.d.entity_type] || {}).color + '22', color: (TYPE_STYLE[panel.d.entity_type] || {}).color }">
              {{ (TYPE_STYLE[panel.d.entity_type] || {}).zh || panel.d.entity_type }}
            </span>
            <h3>{{ panel.d.name }}</h3>
            <router-link v-if="panel.d.library_paper_id" class="btn sm" :to="readerUrl(panel.d.library_paper_id)">
              ↗ 打开这篇论文
            </router-link>
          </div>
          <p v-if="panel.d.description" class="p-desc">{{ panel.d.description }}</p>
          <p v-if="panel.d.aliases.length" class="p-alias">别名：{{ panel.d.aliases.join('、') }}</p>
          <div class="p-sec">
            <h4>关系（{{ panel.d.relations.length }}）</h4>
            <div v-for="r in panel.d.relations" :key="r.id" class="rel-item" @click="openRelation(r.id)">
              <span class="rel-dir">{{ r.source_name }} <b class="rel-type"
                :class="{ inf: r.evidence_type !== 'explicit' }">{{ r.relation_type }}</b> {{ r.target_name }}</span>
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
              {{ panel.d.source.name }} <b class="rel-type"
                :class="{ inf: panel.d.evidence_type !== 'explicit' }">{{ panel.d.relation_type }}</b>
              {{ panel.d.target.name }}</h3>
            <div class="muted" style="font-size:12px">
              {{ (TYPE_STYLE[panel.d.source.type] || {}).zh || panel.d.source.type }} →
              {{ (TYPE_STYLE[panel.d.target.type] || {}).zh || panel.d.target.type }} ·
              置信度 {{ Math.round(panel.d.confidence * 100) }}% ·
              <span :class="{ 'ev-warn': panel.d.evidence_type !== 'explicit' }">
                {{ EVI_LABEL[panel.d.evidence_type] || panel.d.evidence_type }}</span>
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

        <!-- 主题详情 -->
        <template v-else-if="panel.kind === 'topic'">
          <div class="p-head"><h3>{{ panel.topic.name }}</h3>
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
                :class="{ inf: r.evidence_type !== 'explicit' }">{{ r.relation_type }}</b> {{ r.target_name }}</span>
            </div>
            <div v-if="!panel.topic.key_relations.length" class="muted">暂无</div>
          </div>
        </template>

        <div v-else class="kg-panel-hint">
          <p><b>怎么用</b></p>
          <ul>
            <li>点<b>节点</b>看实体详情：是什么、出现在哪些论文、原文在哪</li>
            <li>点<b>边</b>看关系证据：AI 为什么说 A 改进了 B——原句在此</li>
            <li>虚线边 = <b>推断关系</b>（论文未明说，AI 谨慎推理），实线边 = 原文明说</li>
            <li>搜实体名 / 按类型过滤 / 单篇聚焦，都是即时重排</li>
            <li>阅读器侧边栏新增「图谱」页签，可看当前论文的知识结构</li>
          </ul>
          <p class="muted" style="font-size:12px;margin-top:10px">
            图谱完全由论文原文抽取，每条关系带逐字证据；没有证据的内容不会入库。</p>
        </div>
      </aside>
    </div>
  </div>
</template>

<style scoped>
.kg-page { display: flex; flex-direction: column; min-height: calc(100vh - 60px); }
.kg-head { display: flex; justify-content: space-between; align-items: flex-start; gap: 16px; }
.kg-actions { display: flex; gap: 8px; align-items: center; padding-top: 6px; }
.kg-jobbar {
  display: flex; align-items: center; gap: 10px; margin-top: 10px; padding: 7px 14px;
  background: var(--accent-soft); border-radius: 8px; font-size: 13px; color: var(--accent);
}
.kg-jobbar .progressbar { width: 160px; }
.kg-toolbar { display: flex; flex-wrap: wrap; gap: 8px; align-items: center; margin: 14px 0 10px; }
.type-chips { display: flex; flex-wrap: wrap; gap: 5px; }
.chip {
  --c: #999; display: inline-flex; align-items: center; gap: 4px; padding: 3px 9px;
  border-radius: 14px; border: 1px solid var(--line); background: #fff; cursor: pointer;
  font-size: 12px; color: var(--c); font-weight: 600;
}
.chip .cnt { color: var(--ink-3); font-weight: 400; }
.chip.off { opacity: .38; filter: grayscale(.6); }
.kg-body { display: flex; gap: 14px; flex: 1; min-height: 0; }
.kg-topiclist { width: 230px; flex-shrink: 0; overflow: auto; }
.topic-item { padding: 10px 12px; border: 1px solid var(--line); border-radius: 8px;
  margin-bottom: 8px; cursor: pointer; background: var(--panel); }
.topic-item:hover { border-color: var(--accent); }
.topic-item.on { border-color: var(--accent); background: var(--accent-soft); }
.t-name { font-weight: 600; font-size: 13.5px; }
.t-meta { font-size: 12px; color: var(--ink-3); margin-top: 2px; }
.kg-canvas-wrap { position: relative; flex: 1; min-width: 0; height: calc(100vh - 260px);
  min-height: 420px; background: var(--panel); border: 1px solid var(--line);
  border-radius: var(--radius); overflow: hidden; }
#kg-canvas { width: 100%; height: 100%; }
.kg-loading { position: absolute; top: 12px; left: 14px; font-size: 12px; color: var(--ink-3); }
.kg-empty { position: absolute; inset: 0; display: flex; flex-direction: column;
  align-items: center; justify-content: center; text-align: center; color: var(--ink-2);
  line-height: 2; font-size: 14px; }
.kg-legend { position: absolute; bottom: 10px; left: 12px; right: 12px; display: flex;
  flex-wrap: wrap; gap: 10px; font-size: 11.5px; color: var(--ink-3); pointer-events: none; }
.lg-item { display: inline-flex; align-items: center; gap: 5px; }
.lg-item i { width: 10px; height: 10px; border-radius: 3px; display: inline-block; }
.lg-line { width: 16px !important; height: 0 !important; border-top: 2px solid #b9b4a8; border-radius: 0 !important; }
.lg-line.dash { border-top-style: dashed; border-top-color: #b3423a; }
.kg-panel { width: 360px; flex-shrink: 0; overflow: auto; background: var(--panel);
  border: 1px solid var(--line); border-radius: var(--radius); padding: 16px; }
.kg-panel-hint { font-size: 13px; line-height: 1.9; color: var(--ink-2); }
.kg-panel-hint ul { margin: 6px 0; padding-left: 18px; }
.p-head h3 { margin: 8px 0 6px; font-size: 16px; }
.p-desc { font-size: 13px; color: var(--ink-2); margin: 6px 0; }
.p-alias { font-size: 12.5px; color: var(--ink-3); margin: 4px 0; }
.p-sec { margin-top: 14px; }
.p-sec h4 { margin: 0 0 8px; font-size: 13px; color: var(--ink-3); font-weight: 600; }
.rel-item { display: flex; justify-content: space-between; gap: 8px; align-items: center;
  padding: 6px 8px; border-radius: 6px; cursor: pointer; font-size: 12.8px; }
.rel-item:hover { background: var(--accent-soft); }
.rel-dir { flex: 1; min-width: 0; }
.rel-type { color: var(--accent); font-size: 11px; padding: 0 3px; }
.rel-type.inf { color: var(--danger); }
.ev-warn { color: var(--warn); }
.ev-item { border-left: 3px solid var(--line); padding: 6px 10px; margin-bottom: 10px; }
.ev-meta { font-size: 12px; color: var(--ink-3); display: flex; gap: 6px; flex-wrap: wrap; }
.ev-paper { color: var(--ink-2); font-weight: 600; }
.ev-badge { padding: 0 6px; border-radius: 8px; background: var(--accent-soft); color: var(--accent); font-size: 11px; }
.ev-badge.inferred { background: #fbeae8; color: var(--danger); }
.ev-text { font-size: 12.8px; color: var(--ink); margin: 4px 0; line-height: 1.6; }
.ev-jump { font-size: 12px; }
.seg { display: inline-flex; border: 1px solid var(--line); border-radius: 8px; overflow: hidden; background: var(--panel); }
.seg button { border: 0; background: transparent; padding: 6px 14px; font-size: 13px; cursor: pointer; color: var(--ink-2); }
.seg button.on { background: var(--accent-soft); color: var(--accent); font-weight: 600; }
.progressbar { height: 6px; border-radius: 4px; background: #e5e3dc; overflow: hidden; }
.progressbar > div { height: 100%; background: var(--accent); transition: width .4s; }
.muted { color: var(--ink-3); }
</style>
