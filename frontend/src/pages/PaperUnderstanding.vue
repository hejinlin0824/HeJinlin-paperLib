<script setup>
import { computed, onMounted, ref } from 'vue'
import api from '../api'
import { toast } from '../stores'

const papers = ref([])
const drafts = ref([])
const selectedPaper = ref('')
const selectedId = ref('')
const current = ref(null)
const busy = ref(false)
const edited = ref(false)
const payload = ref(null)
const jsonText = ref('')
const jsonValid = ref(true)

const selectedDraft = computed(() => drafts.value.find(x => x.id === selectedId.value))
const contributions = computed(() => payload.value?.contributions || [])
const relations = computed(() => payload.value?.relations || [])
const paperName = (id) => {
  const p = papers.value.find(x => x.id === id)
  return p?.title_zh || p?.title || id
}
const importanceLabel = (v) => ({core:'核心贡献',supporting:'支撑知识',context:'背景信息'}[v] || v || '未分类')
function nodeDepth(id) {
  const byId = new Map(contributions.value.map(n => [n.id, n]))
  const seen = new Set([id])
  let node = byId.get(id), depth = 0
  while (node?.parent_id && byId.has(node.parent_id) && depth < 8 && !seen.has(node.parent_id)) {
    seen.add(node.parent_id); depth++; node = byId.get(node.parent_id)
  }
  return depth
}

async function refresh() {
  const [p, d] = await Promise.all([api.papers(), api.kgV3Drafts()])
  papers.value = (Array.isArray(p) ? p : (p.items || p.papers || []))
    .filter(item => (!item.library_role || item.library_role === 'normal') && !['pending_parse','parsing'].includes(item.status))
  drafts.value = d || []
}
async function openDraft(id) {
  selectedId.value = id
  current.value = await api.kgV3Draft(id)
  payload.value = JSON.parse(JSON.stringify(current.value.payload || {}))
  jsonText.value = JSON.stringify(payload.value, null, 2)
  jsonValid.value = true
  edited.value = false
}
async function start() {
  if (!selectedPaper.value) return
  busy.value = true
  try {
    const result = await api.kgV3Understand(selectedPaper.value)
    await refresh()
    await openDraft(result.draft.id)
    toast('已启动全文理解任务；稍后刷新查看结果')
  } catch (e) { toast(e.message || '启动失败') }
  finally { busy.value = false }
}
async function reload() {
  if (!selectedId.value) return
  busy.value = true
  try { await refresh(); await openDraft(selectedId.value) }
  catch (e) { toast(e.message || '刷新失败') }
  finally { busy.value = false }
}
async function save() {
  if (!current.value || !payload.value) return
  busy.value = true
  try {
    current.value = await api.kgV3Save(current.value.id, payload.value)
    payload.value = JSON.parse(JSON.stringify(current.value.payload || {}))
    jsonText.value = JSON.stringify(payload.value, null, 2)
    jsonValid.value = true
    edited.value = false
    await refresh()
    toast('审阅修改已保存；尚未写入正式知识图谱')
  } catch (e) { toast(e.message || '保存失败') }
  finally { busy.value = false }
}
async function review(status) {
  if (!current.value) return
  busy.value = true
  try {
    current.value = await api.kgV3Review(current.value.id, status, payload.value)
    payload.value = JSON.parse(JSON.stringify(current.value.payload || {}))
    jsonText.value = JSON.stringify(payload.value, null, 2)
    jsonValid.value = true
    edited.value = false
    await refresh()
    toast(status === 'approved' ? '理解档案已确认（仍未写入旧图谱）' : '理解档案已驳回；旧图谱不受影响')
  } catch (e) { toast(e.message || '审核失败') }
  finally { busy.value = false }
}
function parseEditor() {
  edited.value = true
  try { payload.value = JSON.parse(jsonText.value); jsonValid.value = true }
  catch { jsonValid.value = false }
}

onMounted(async () => {
  try { await refresh() } catch (e) { toast(e.message || '加载失败') }
})
</script>

<template>
  <main class="understanding-page">
    <header class="hero">
      <div>
        <div class="eyebrow">PAPER UNDERSTANDING · V3</div>
        <h1>先理解论文，再构建知识</h1>
        <p>全文理解 → 贡献树 → 关系语义核验 → 人工审阅。此处的草稿与现有知识图谱完全隔离。</p>
      </div>
      <a href="/graph">返回知识图谱</a>
    </header>

    <section class="start-card">
      <label>选择已解析论文</label>
      <div class="start-row">
        <select v-model="selectedPaper">
          <option value="">请选择论文</option>
          <option v-for="p in papers" :key="p.id" :value="p.id">{{ p.title_zh || p.title || p.id }}</option>
        </select>
        <button :disabled="!selectedPaper || busy" @click="start">{{ busy ? '处理中…' : '生成理解档案' }}</button>
        <button class="secondary" :disabled="busy" @click="refresh">刷新列表</button>
      </div>
      <small>会调用你在设置页配置的模型；按章节摘要后进行全文综合，并对关系候选进行独立语义复核。不会自动重抽旧图谱。</small>
    </section>

    <div class="workspace">
      <aside class="draft-list">
        <h2>理解任务</h2>
        <button v-for="d in drafts" :key="d.id" class="draft-item" :class="{active:selectedId===d.id}" @click="openDraft(d.id)">
          <strong>{{ paperName(d.paper_id) }}</strong>
          <span>{{ d.status }} · {{ new Date(d.created_at).toLocaleString() }}</span>
        </button>
        <p v-if="!drafts.length" class="muted">还没有理解档案。选择论文并生成。</p>
      </aside>

      <section class="detail" v-if="current && payload">
        <div class="detail-head">
          <div>
            <div class="eyebrow">V3 / {{ current.version }} / {{ current.model_name || '模型信息待定' }}</div>
            <h2>{{ payload.paper_title || paperName(current.paper_id) }}</h2>
            <p>任务状态：<b>{{ current.status }}</b>。{{ current.status==='running' ? '模型正在分析，稍后刷新。' : current.status==='failed' ? '任务失败，请查看告警。' : '请核对贡献与关系，修改后保存。' }}</p>
          </div>
          <button class="secondary" :disabled="busy" @click="reload">刷新结果</button>
        </div>
        <div v-if="current.status==='running'" class="notice">全文理解正在运行。章节摘要、贡献树和关系核验完成后会显示在这里。</div>
        <template v-else>
          <div class="summary-grid">
            <article><label>论文类型</label><strong>{{ payload.paper_type || '未识别' }}</strong></article>
            <article><label>核心问题</label><p>{{ payload.problem || '未提取' }}</p></article>
            <article><label>核心主张</label><p>{{ payload.thesis || '未提取' }}</p></article>
          </div>

          <section class="block">
            <div class="section-title"><h3>贡献树</h3><span>{{ contributions.length }} 个节点</span></div>
            <p class="hint">核心贡献是作者真正提出的内容；支撑知识包括定义、条件、证明、模块和关键结果；背景信息默认不进入核心图谱。</p>
            <article v-for="node in contributions" :key="node.id" class="contribution" :style="{ marginLeft: (nodeDepth(node.id) * 18) + 'px' }">
              <div class="node-title">
                <strong>{{ node.title }}</strong>
                <span :class="['tag',node.importance]">{{ importanceLabel(node.importance) }}</span>
                <span class="kind">{{ node.kind }}</span>
              </div>
              <p>{{ node.description }}</p>
              <small v-for="ev in (node.evidence || [])" :key="ev.block_id + ev.quote" class="evidence">块 #{{ ev.block_id }}：{{ ev.quote }}</small>
            </article>
            <p v-if="!contributions.length" class="muted">没有通过证据校验的贡献节点。请检查原文解析或模型输出，不会用噪声填充图谱。</p>
          </section>

          <section class="block">
            <div class="section-title"><h3>理论结构</h3><span>{{ (payload.theoretical_framework || []).length }} 项</span></div>
            <article v-for="x in (payload.theoretical_framework || [])" :key="x.name" class="compact">
              <strong>{{ x.name }}</strong><span>{{ x.role }}</span>
              <small v-for="ev in (x.evidence || [])" :key="ev.block_id+ev.quote" class="evidence">块 #{{ ev.block_id }}：{{ ev.quote }}</small>
            </article>
            <p v-if="!(payload.theoretical_framework || []).length" class="muted">未识别到有证据支持的理论结构。</p>
          </section>

          <section class="block">
            <div class="section-title"><h3>候选关系与语义验证</h3><span>{{ relations.length }} 条</span></div>
            <article v-for="r in relations" :key="r.source_id+r.target_id+r.relation_type" class="relation">
              <strong>{{ contributions.find(n=>n.id===r.source_id)?.title || r.source_id }}</strong>
              <span class="predicate">{{ r.relation_type }}</span>
              <strong>{{ contributions.find(n=>n.id===r.target_id)?.title || r.target_id }}</strong>
              <span :class="['tag',r.verification]">{{ ({entailed:'语义蕴含',not_entailed:'不蕴含',uncertain:'不确定',unverified:'未核验'})[r.verification] || r.verification }}</span>
              <p>{{ r.reason }}</p>
              <small class="evidence">块 #{{ r.evidence.block_id }}：{{ r.evidence.quote }}</small>
            </article>
            <p v-if="!relations.length" class="muted">没有足够证据支持的候选关系。空关系图优于错误关系图。</p>
          </section>

          <section class="block">
            <div class="section-title"><h3>章节结构与结论</h3><span>{{ (payload.section_map || []).length }} 章</span></div>
            <article v-for="s in (payload.section_map || [])" :key="s.title" class="compact">
              <strong>{{ s.title }}</strong><span>{{ s.section_role }}</span><p>{{ s.summary }}</p>
            </article>
            <h4>关键发现</h4>
            <article v-for="f in (payload.key_findings || [])" :key="f.claim" class="compact">
              <p>{{ f.claim }}</p><small v-for="ev in (f.evidence || [])" :key="ev.block_id+ev.quote" class="evidence">块 #{{ ev.block_id }}：{{ ev.quote }}</small>
            </article>
            <h4>局限与不确定项</h4>
            <ul><li v-for="x in (payload.limitations || [])" :key="x">{{ x }}</li><li v-for="x in (payload.uncertainties || [])" :key="x">待确认：{{ x }}</li></ul>
          </section>

          <section class="block">
            <div class="section-title"><h3>人工修订</h3><span>编辑 JSON 后保存</span></div>
            <p class="hint">你可以调整贡献名称、层级、描述、证据和关系。保存只更新 v3 草稿，不写入 v2 正式图谱。</p>
            <textarea v-model="jsonText" @input="parseEditor" spellcheck="false"></textarea>
            <p v-if="!jsonValid" class="editor-error">JSON 格式错误，请修正后再保存或确认。</p>
            <div class="actions">
              <button :disabled="busy || !edited || !jsonValid" @click="save">保存修改</button>
              <button class="approve" :disabled="busy || current.status==='running' || !jsonValid" @click="review('approved')">确认理解档案</button>
              <button class="reject" :disabled="busy || current.status==='running' || !jsonValid" @click="review('rejected')">驳回</button>
            </div>
            <p class="hint">确认仅表示你认可论文理解结果。正式图谱写入/转换是后续独立步骤，不会在此处自动发生。</p>
          </section>
          <section v-if="current.warnings?.length" class="block warnings">
            <h3>校验告警</h3><ul><li v-for="(w,i) in current.warnings" :key="i">{{ w }}</li></ul>
          </section>
        </template>
      </section>
      <section v-else class="empty-detail"><h2>选择一个理解任务</h2><p>先选择论文生成全文理解档案，完成后在这里审阅。</p></section>
    </div>
  </main>
</template>

<style scoped>
.understanding-page{max-width:1500px;margin:0 auto;padding:30px 34px 60px;color:#263445}.hero{display:flex;justify-content:space-between;align-items:flex-start;gap:24px;margin-bottom:24px}.hero h1{font-size:28px;margin:8px 0}.hero p,.muted,.hint{color:#718096}.eyebrow{font-size:11px;letter-spacing:.13em;color:#718096;font-weight:700}.hero a{color:#456b91;text-decoration:none}.start-card,.draft-list,.detail,.empty-detail{background:#fff;border:1px solid #e3e9ef;border-radius:12px;padding:20px}.start-card{margin-bottom:18px}.start-card label,.summary-grid label{display:block;font-size:12px;color:#718096;margin-bottom:8px}.start-row{display:flex;gap:10px;margin-bottom:10px}.start-row select{min-width:300px;flex:1}.start-row select,button,textarea{font:inherit}.start-row select{border:1px solid #d6dee7;border-radius:7px;padding:9px}.start-row button,.actions button{border:0;border-radius:7px;background:#315f87;color:#fff;padding:9px 15px;cursor:pointer}.button:disabled,button:disabled{opacity:.5;cursor:not-allowed}.secondary{background:#edf2f7!important;color:#40566d!important}.workspace{display:grid;grid-template-columns:290px minmax(0,1fr);gap:18px;align-items:start}.draft-list{position:sticky;top:18px}.draft-list h2,.detail h2{margin:0 0 14px;font-size:18px}.draft-item{display:block;width:100%;text-align:left;background:#f7f9fb;border:1px solid #e6ebf0;border-radius:8px;padding:11px;margin:8px 0;color:#34465a}.draft-item.active{border-color:#7898b6;background:#edf4fa}.draft-item strong,.draft-item span{display:block}.draft-item span{font-size:11px;color:#7b8794;margin-top:5px}.detail-head{display:flex;justify-content:space-between;gap:18px;border-bottom:1px solid #e7edf2;padding-bottom:15px}.detail-head p{font-size:13px;color:#718096}.summary-grid{display:grid;grid-template-columns:1fr 1fr 1fr;gap:10px;margin:18px 0}.summary-grid article,.contribution,.compact,.relation{border:1px solid #e7edf2;border-radius:8px;padding:13px;margin:8px 0}.summary-grid article{background:#f8fafc}.summary-grid p,.contribution p,.compact p,.relation p{margin:5px 0;color:#526174;font-size:13px;line-height:1.6}.block{margin-top:22px}.section-title{display:flex;align-items:center;justify-content:space-between}.section-title h3{margin:0;font-size:16px}.section-title span,.kind{font-size:11px;color:#7b8794}.hint{font-size:12px;line-height:1.6}.node-title{display:flex;gap:10px;align-items:center;flex-wrap:wrap}.tag{font-size:11px;padding:3px 7px;border-radius:99px;background:#edf2f7;color:#526174}.tag.core,.tag.entailed{background:#e4f2ea;color:#286344}.tag.supporting{background:#e9f0f8;color:#3f6387}.tag.context,.tag.uncertain{background:#f2f0e8;color:#796b3d}.tag.not_entailed,.tag.rejected{background:#f8e9e8;color:#9a4943}.evidence{display:block;color:#66788a;background:#f7f9fb;padding:7px 9px;border-radius:5px;margin-top:7px;font-size:11px;line-height:1.5}.compact{display:grid;grid-template-columns:minmax(140px,220px) auto;gap:5px 12px}.compact p,.compact small{grid-column:1/-1}.relation{display:flex;align-items:center;gap:9px;flex-wrap:wrap}.relation p,.relation small{width:100%}.predicate{font-size:11px;color:#416887;background:#edf4fa;padding:4px 7px;border-radius:5px}.editor-error{color:#a14e49;font-size:12px}.detail textarea{width:100%;min-height:330px;border:1px solid #dce4eb;border-radius:8px;padding:12px;font:12px/1.55 ui-monospace,Consolas,monospace;color:#34465a;resize:vertical}.actions{display:flex;gap:9px;margin-top:12px}.actions .approve{background:#347653}.actions .reject{background:#a14e49}.warnings{background:#fffaf1;border:1px solid #f1dfb9;border-radius:8px;padding:12px}.notice{padding:14px;background:#edf4fa;border-radius:8px;margin:15px 0}.empty-detail{min-height:300px;display:grid;place-content:center;text-align:center;color:#718096}
@media(max-width:900px){.workspace{grid-template-columns:1fr}.draft-list{position:static}.summary-grid{grid-template-columns:1fr}.understanding-page{padding:18px}.start-row{flex-wrap:wrap}.start-row select{min-width:100%}}
</style>
