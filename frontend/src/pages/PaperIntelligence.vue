<script setup>
import { computed, onMounted, ref } from 'vue'
import api from '../api'
import { toast } from '../stores'

const papers = ref([])
const drafts = ref([])
const paperId = ref('')
const draftId = ref('')
const current = ref(null)
const payload = ref(null)
const jsonText = ref('')
const query = ref('')
const searchResults = ref([])
const candidates = ref([])
const indexInfo = ref(null)
const busy = ref(false)
const dense = ref(false)
const paperOptions = computed(() => papers.value.filter(p => (!p.library_role || p.library_role === 'normal') && !['pending_parse','parsing'].includes(p.status)))
const paperTitle = (id) => {
  const p = papers.value.find(x => x.id === id)
  return p?.title_zh || p?.title || id
}

async function refresh() {
  const [p, d] = await Promise.all([api.papers(), api.kgV4Drafts()])
  papers.value = Array.isArray(p) ? p : (p.items || p.papers || [])
  drafts.value = (d || []).filter(item => item.version === '4.0')
}
async function loadDraft(id) {
  draftId.value = id
  current.value = await api.kgV4Draft(id)
  payload.value = JSON.parse(JSON.stringify(current.value.payload || {}))
  jsonText.value = JSON.stringify(payload.value, null, 2)
  paperId.value = current.value.paper_id
}
async function buildIndex() {
  if (!paperId.value) return
  busy.value = true
  try {
    indexInfo.value = await api.kgV4Index(paperId.value, dense.value)
    toast('本地论文索引已建立')
  } catch (e) { toast(e.message || '索引建立失败') }
  finally { busy.value = false }
}
async function search() {
  if (!query.value.trim()) return
  busy.value = true
  try {
    const result = await api.kgV4Search(query.value, paperId.value || undefined, 10)
    searchResults.value = result.results || []
    indexInfo.value = { ...(indexInfo.value || {}), mode: result.mode }
  } catch (e) { toast(e.message || '检索失败') }
  finally { busy.value = false }
}
async function findCandidates() {
  if (!paperId.value) return
  busy.value = true
  try {
    const result = await api.kgV4Candidates(paperId.value)
    candidates.value = result.candidates || []
    toast('规则候选识别完成；候选仍需人工审查')
  } catch (e) { toast(e.message || '候选识别失败') }
  finally { busy.value = false }
}
async function startUnderstanding() {
  if (!paperId.value) return
  busy.value = true
  try {
    const result = await api.kgV4Understand(paperId.value)
    await refresh()
    await loadDraft(result.draft.id)
    toast('V4 理解任务已启动；可稍后刷新查看')
  } catch (e) { toast(e.message || '启动失败') }
  finally { busy.value = false }
}
async function reloadDraft() {
  if (!draftId.value) return
  busy.value = true
  try { await refresh(); await loadDraft(draftId.value) }
  catch (e) { toast(e.message || '刷新失败') }
  finally { busy.value = false }
}
async function saveDraft() {
  if (!current.value) return
  busy.value = true
  try {
    const edited = JSON.parse(jsonText.value)
    current.value = await api.kgV4Save(current.value.id, edited)
    payload.value = JSON.parse(JSON.stringify(current.value.payload || {}))
    jsonText.value = JSON.stringify(payload.value, null, 2)
    toast('V4 草稿修改已保存；未写入正式图谱')
  } catch (e) { toast(e.message || 'JSON 无效或保存失败') }
  finally { busy.value = false }
}
async function review(status) {
  if (!current.value || !payload.value) return
  busy.value = true
  try {
    const edited = JSON.parse(jsonText.value)
    current.value = await api.kgV4Review(current.value.id, status, edited)
    payload.value = JSON.parse(JSON.stringify(current.value.payload || {}))
    jsonText.value = JSON.stringify(payload.value, null, 2)
    await refresh()
    toast(status === 'approved' ? 'V4 理解档案已确认，尚未写入正式图谱' : '档案已驳回')
  } catch (e) { toast(e.message || '审核失败') }
  finally { busy.value = false }
}
onMounted(async () => {
  try { await refresh() } catch (e) { toast(e.message || '加载失败') }
})
</script>

<template>
  <main class="intelligence-page">
    <header class="hero">
      <div>
        <div class="eyebrow">PAPERLIB · V4 HYBRID INTELLIGENCE</div>
        <h1>混合论文理解工作台</h1>
        <p>本地结构解析、BM25 检索、学术规则候选与有限 LLM 推理。所有理解草稿与正式知识图谱隔离。</p>
      </div>
      <nav><a href="/kg-understanding">V3 历史工作台</a><a href="/graph">知识图谱</a></nav>
    </header>

    <section class="panel">
      <h2>论文与索引</h2>
      <div class="toolbar">
        <select v-model="paperId">
          <option value="">选择已解析论文</option>
          <option v-for="p in paperOptions" :key="p.id" :value="p.id">{{ p.title_zh || p.title || p.id }}</option>
        </select>
        <label class="check"><input v-model="dense" type="checkbox"> 使用本地 BGE-M3（需自行安装并配置模型路径）</label>
        <button :disabled="!paperId || busy" @click="buildIndex">建立检索索引</button>
        <button class="secondary" :disabled="!paperId || busy" @click="findCandidates">识别结构候选</button>
        <button :disabled="!paperId || busy" @click="startUnderstanding">生成 V4 理解档案</button>
      </div>
      <p class="hint">默认只运行本地词法索引，不下载模型、不调用向量云服务。勾选 BGE-M3 后，必须在启动 PaperLib 前配置本机模型目录。</p>
      <div v-if="indexInfo" class="status-line">索引模式：{{ indexInfo.mode || indexInfo.index_status }} · 已索引块数：{{ indexInfo.indexed_blocks ?? '—' }}</div>
    </section>

    <section class="panel">
      <h2>论文片段检索</h2>
      <div class="toolbar">
        <input v-model="query" placeholder="例如：necessary and sufficient conditions" @keydown.enter="search">
        <button :disabled="busy || !query.trim()" @click="search">检索</button>
      </div>
      <p class="hint">默认使用 BM25 风格词法检索；若索引包含本地向量，则自动融合稠密检索排序。结果保留原始 Block ID。</p>
      <article v-for="item in searchResults" :key="item.paper_id + '-' + item.block_id" class="result">
        <div class="result-head"><strong>{{ paperTitle(item.paper_id) }}</strong><span>{{ item.section_title }} · Block #{{ item.block_id }}</span></div>
        <p>{{ item.text }}</p>
        <small>BM25 {{ item.lexical_score }} · Dense {{ item.dense_score }} · RRF {{ item.rrf_score }}</small>
        <a :href="`/reader/${item.paper_id}?block=${item.block_id}`">定位原文 ↗</a>
      </article>
      <p v-if="!searchResults.length" class="muted">暂无检索结果。先选择论文并建立索引。</p>
    </section>

    <section v-if="candidates.length" class="panel">
      <h2>规则识别候选 <span class="count">{{ candidates.length }}</span></h2>
      <p class="hint">这些是基于学术表达模式发现的候选，不代表模型或人工已经确认其理论地位。</p>
      <article v-for="(item, i) in candidates" :key="item.block_id + '-' + item.kind + '-' + i" class="result">
        <div class="result-head"><strong>{{ item.kind }} · {{ item.matched_term }}</strong><span>Block #{{ item.block_id }}</span></div>
        <p>{{ item.excerpt }}</p>
        <a :href="`/reader/${paperId}?block=${item.block_id}`">定位原文 ↗</a>
      </article>
    </section>

    <section class="workspace">
      <aside class="panel drafts">
        <h2>V4 理解任务</h2>
        <button v-for="d in drafts" :key="d.id" class="draft" :class="{active:draftId===d.id}" @click="loadDraft(d.id)">
          <strong>{{ paperTitle(d.paper_id) }}</strong><span>V{{ d.version }} · {{ d.status }} · {{ new Date(d.created_at).toLocaleString() }}</span>
        </button>
        <p v-if="!drafts.length" class="muted">尚无理解档案。</p>
      </aside>
      <section v-if="current && payload" class="panel detail">
        <div class="result-head">
          <div><div class="eyebrow">V{{ current.version }} · {{ current.model_name || 'local-first' }}</div><h2>{{ payload.paper_title || paperTitle(current.paper_id) }}</h2><p>任务状态：<b>{{ current.status }}</b></p></div>
          <button class="secondary" :disabled="busy" @click="reloadDraft">刷新结果</button>
        </div>
        <div class="metrics">
          <div><strong>{{ payload.coverage?.completed_chunks ?? 0 }}/{{ payload.coverage?.chunk_count ?? 0 }}</strong><small>完成分块</small></div>
          <div><strong>{{ Math.round((payload.coverage?.input_coverage || 0)*100) }}%</strong><small>成功分析输入覆盖率</small></div>
          <div><strong>{{ payload.contributions?.length || 0 }}</strong><small>贡献候选</small></div>
          <div><strong>{{ payload.pipeline?.llm_operations ?? 0 }}</strong><small>LLM 阶段调用</small></div>
        </div>
        <p class="hint">覆盖率只表示成功完成分析的分块正文比例，不等同于语义正确率。未核验关系不会自动视为成立。</p>
        <p v-if="payload.coverage?.not_attempted_chunks" class="hint">尚未尝试分析：{{ payload.coverage.not_attempted_chunks }} 个分块（模型未就绪或未启动分析），不计入失败分块。</p>
        <h3>核心问题</h3><p>{{ payload.problem || '尚未提取' }}</p>
        <h3>核心主张</h3><p>{{ payload.thesis || '尚未提取' }}</p>
        <h3>贡献树 / 结构候选</h3>
        <article v-for="node in (payload.contributions || [])" :key="node.id" class="result">
          <div class="result-head"><strong>{{ node.title }}</strong><span>{{ node.importance }} · {{ node.knowledge_role }} · {{ node.origin || "LLM/人工候选" }}</span></div>
          <p>{{ node.description }}</p>
          <a v-for="ev in (node.evidence || [])" :key="ev.block_id + ev.quote" :href="`/reader/${current.paper_id}?block=${ev.block_id}`">Block #{{ ev.block_id }}：{{ ev.quote }} · 定位原文 ↗</a>
        </article>
        <h3>关系候选</h3>
        <article v-for="edge in (payload.relations || [])" :key="edge.source_id + edge.target_id + edge.relation_type" class="result">
          <strong>{{ edge.source_id }} — {{ edge.relation_type }} → {{ edge.target_id }}</strong>
          <span>{{ edge.verification || 'unverified' }}</span><p>{{ edge.reason }}</p>
        </article>
        <h3>理论逻辑超边</h3>
        <article v-for="edge in (payload.theory_logic || [])" :key="edge.id" class="result">
          <strong>{{ (edge.source_ids || []).join(' ∧ ') }} — {{ edge.relation_type }} → {{ edge.target_id }}</strong>
          <span>{{ edge.verification || 'unverified' }}</span><p>{{ edge.statement }}</p>
        </article>
        <h3>章节摘要</h3>
        <article v-for="section in (payload.section_map || [])" :key="section.title" class="result">
          <div class="result-head"><strong>{{ section.title }}</strong><span>{{ section.coverage_status }}</span></div><p>{{ section.summary }}</p>
        </article>
        <details class="editor">
          <summary>人工修订 JSON（高级）</summary>
          <p class="hint">可编辑贡献、证据和关系；保存仅更新 V4 隔离草稿。</p>
          <textarea v-model="jsonText" spellcheck="false"></textarea>
          <button :disabled="busy" @click="saveDraft">校验并保存 JSON</button>
        </details>
        <h3>人工审核</h3>
        <p class="hint">确认仅确认理解档案，不会自动写入 V2 正式知识图谱。</p>
        <div class="toolbar"><button :disabled="busy || current.status==='running'" @click="review('approved')">确认档案</button><button class="danger" :disabled="busy || current.status==='running'" @click="review('rejected')">驳回</button></div>
        <section v-if="current.warnings?.length" class="warnings"><h3>告警</h3><ul><li v-for="(w,i) in current.warnings" :key="i">{{ w }}</li></ul></section>
      </section>
      <section v-else class="panel empty"><h2>选择一个 V4 理解任务</h2><p>生成后可在此检查覆盖率、贡献、关系与原文证据。</p></section>
    </section>
  </main>
</template>

<style scoped>
.intelligence-page{max-width:1500px;margin:auto;padding:28px 32px 60px;color:#263445}.hero{display:flex;justify-content:space-between;gap:20px;margin-bottom:22px}.hero h1{font-size:28px;margin:8px 0}.hero p,.hint,.muted{color:#718096}.eyebrow{font-size:11px;letter-spacing:.12em;color:#718096;font-weight:700}.hero nav{display:flex;gap:18px}.hero a,.result a{color:#456b91;text-decoration:none}.panel{background:#fff;border:1px solid #e3e9ef;border-radius:12px;padding:18px;margin-bottom:16px}.panel h2{font-size:18px;margin:0 0 14px}.panel h3{font-size:15px;margin:18px 0 8px}.toolbar{display:flex;gap:10px;align-items:center;flex-wrap:wrap}.toolbar select,.toolbar input{border:1px solid #d6dee7;border-radius:7px;padding:10px;min-width:240px;flex:1;font:inherit}.toolbar button{border:0;border-radius:7px;padding:10px 14px;background:#315f87;color:white;cursor:pointer}.toolbar button:disabled{opacity:.5}.toolbar .secondary{background:#edf2f7;color:#40566d}.toolbar .danger{background:#9a3e3e}.check{font-size:12px;color:#667789}.status-line{margin-top:12px;color:#456b91;font-size:13px}.result{border:1px solid #e7edf2;border-radius:8px;padding:12px;margin:9px 0}.result p{font-size:13px;line-height:1.65;color:#526174;white-space:pre-wrap}.result small,.result-head span{color:#718096;font-size:11px}.result-head{display:flex;justify-content:space-between;align-items:center;gap:12px}.result>a{display:block;font-size:12px;margin-top:6px}.workspace{display:grid;grid-template-columns:290px minmax(0,1fr);gap:16px;align-items:start}.draft{display:block;width:100%;text-align:left;border:1px solid #e5ebf0;background:#f8fafc;border-radius:8px;padding:10px;margin:8px 0;color:#34465a}.draft.active{border-color:#7898b6;background:#edf4fa}.draft strong,.draft span{display:block}.draft span{font-size:11px;color:#718096;margin-top:5px}.metrics{display:grid;grid-template-columns:repeat(4,1fr);gap:9px;margin:16px 0}.metrics div{background:#f7f9fb;border:1px solid #e7edf2;border-radius:8px;padding:12px}.metrics strong,.metrics small{display:block}.metrics strong{font-size:20px}.metrics small{font-size:11px;color:#718096}.detail{min-width:0}.empty{min-height:200px}.editor{margin-top:16px}.editor summary{cursor:pointer;font-weight:600}.editor textarea{display:block;width:100%;min-height:240px;margin:10px 0;border:1px solid #d6dee7;border-radius:8px;padding:10px;font:12px/1.5 ui-monospace,monospace}.editor button{border:0;border-radius:7px;padding:9px 14px;background:#315f87;color:white;cursor:pointer}.warnings{background:#fff9f0;border:1px solid #f0dfc7;border-radius:8px;padding:12px;margin-top:18px}.count{background:#edf2f7;border-radius:12px;padding:2px 8px;font-size:12px}@media(max-width:900px){.workspace{grid-template-columns:1fr}.hero{display:block}.metrics{grid-template-columns:repeat(2,1fr)}}
</style>
