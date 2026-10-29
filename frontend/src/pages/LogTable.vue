<script setup>
// 研究总表：每篇论文一行，记录研究问题/方案/创新点/思考/收获 + 问题追踪
import { computed, onMounted, reactive, ref } from 'vue'
import { useRouter } from 'vue-router'
import api from '../api'
import { toast } from '../stores'

const router = useRouter()
const rows = ref([])
const kw = ref('')
const statusFilter = ref('')

const edit = reactive({ open: false, id: '', f: {} })
const drawer = reactive({ open: false, id: '', title: '', items: [], newQ: '' })

const COLS = [
  ['research_problem', '研究问题'],
  ['solution', '解决方案'],
  ['innovation', '创新点 / 可借鉴'],
  ['my_thoughts', '我的思考'],
  ['takeaway', '一句话收获'],
]

const filtered = computed(() => rows.value.filter(r => {
  if (statusFilter.value && (r.log_status || '未记录') !== statusFilter.value) return false
  if (!kw.value) return true
  const hay = [r.title, r.title_zh, ...COLS.map(c => r[c[0]]), r.tags].join(' ').toLowerCase()
  return hay.includes(kw.value.toLowerCase())
}))

async function refresh() {
  rows.value = await api.logList()
}

function openEdit(r) {
  edit.open = true
  edit.id = r.id
  edit.f = {
    research_problem: r.research_problem || '', solution: r.solution || '',
    innovation: r.innovation || '', my_thoughts: r.my_thoughts || '',
    takeaway: r.takeaway || '', status: r.log_status || '在读',
    tags: r.tags || '', rating: r.rating || 0,
  }
}
async function saveEdit() {
  const { tags, rating, ...log } = edit.f
  await api.saveLog(edit.id, log)
  await api.patchPaper(edit.id, { tags, rating })
  toast('已保存', 'ok')
  edit.open = false
  refresh()
}

async function setStatus(r, status) {
  await api.saveLog(r.id, { status })
  refresh()
}
async function setRating(r, rating) {
  await api.patchPaper(r.id, { rating })
  r.rating = rating
  refresh()
}

async function openDrawer(r) {
  drawer.open = true
  drawer.id = r.id
  drawer.title = r.title_zh || r.title
  drawer.items = await api.questions(r.id)
  drawer.newQ = ''
}
async function addQ() {
  if (!drawer.newQ.trim()) return
  await api.addQuestion({ paper_id: drawer.id, content: drawer.newQ.trim() })
  drawer.newQ = ''
  drawer.items = await api.questions(drawer.id)
  refresh()
}
async function toggleQ(q) {
  await api.updateQuestion(q.id, { status: q.status === 'open' ? 'resolved' : 'open' })
  drawer.items = await api.questions(drawer.id)
  refresh()
}
async function saveAnswer(q) {
  await api.updateQuestion(q.id, { answer: q.answer })
  toast('已保存', 'ok')
}
async function delQ(q) {
  await api.deleteQuestion(q.id)
  drawer.items = await api.questions(drawer.id)
  refresh()
}

onMounted(refresh)
</script>

<template>
  <div>
    <div style="display:flex; align-items:center; gap:12px; flex-wrap:wrap;">
      <div>
        <h1 class="page-title" style="margin-bottom:2px;">研究总表</h1>
        <p class="page-sub" style="margin-bottom:0;">每读一篇，记录研究问题、方案、创新点与你的思考</p>
      </div>
      <div style="flex:1"></div>
      <input class="input" v-model="kw" placeholder="🔍 搜索标题 / 内容…" style="width:220px;" />
      <select class="select" v-model="statusFilter" style="width:110px;">
        <option value="">全部状态</option>
        <option v-for="s in ['在读', '读完', '搁置', '未记录']" :key="s" :value="s">{{ s }}</option>
      </select>
      <a class="btn" href="/api/log/export?format=csv">⤓ CSV</a>
      <a class="btn" href="/api/log/export?format=md">⤓ Markdown</a>
    </div>

    <div class="tbl-wrap" style="margin-top:16px;">
      <table class="tbl">
        <thead>
          <tr>
            <th style="min-width:220px;">论文</th>
            <th>标签</th>
            <th v-for="[, label] in COLS" :key="label" style="min-width:170px;">{{ label }}</th>
            <th>评分</th>
            <th>问题</th>
            <th>状态</th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="r in filtered" :key="r.id">
            <td>
              <a href="javascript:void(0)" style="font-weight:600;"
                 @click="router.push(`/reader/${r.id}`)">{{ r.title_zh || r.title }}</a>
              <div class="muted" style="margin-top:2px;">
                {{ [r.authors, r.year, r.venue].filter(Boolean).join(' ｜ ') || '—' }}</div>
            </td>
            <td>
              <span v-for="t in (r.tags || '').split(/[,，]/).filter(Boolean)" :key="t"
                    class="badge gray" style="margin:1px 2px 1px 0;">{{ t.trim() }}</span>
            </td>
            <td v-for="[k] in COLS" :key="k" style="cursor:pointer; max-width:240px;"
                :title="r[k]" @click="openEdit(r)">
              <div style="display:-webkit-box; -webkit-line-clamp:3; -webkit-box-orient:vertical; overflow:hidden;
                          font-size:12.5px; color:var(--ink-2); white-space:pre-wrap;">
                {{ r[k] || '＋ 点击填写' }}</div>
            </td>
            <td style="white-space:nowrap; cursor:pointer;" title="点击评分">
              <span v-for="n in 5" :key="n" @click="setRating(r, n)"
                    :style="{ color: n <= (r.rating || 0) ? '#e0a53d' : '#d5d2c9' }">★</span>
            </td>
            <td>
              <span class="badge" :class="r.open_questions ? 'red' : 'green'"
                    style="cursor:pointer;" @click="openDrawer(r)" title="打开问题追踪">
                {{ r.open_questions }} / {{ r.total_questions }}</span>
            </td>
            <td>
              <select class="select" :value="r.log_status || '在读'" style="width:82px; padding:4px 6px;"
                      @change="(e) => setStatus(r, e.target.value)">
                <option v-for="s in ['在读', '读完', '搁置']" :key="s">{{ s }}</option>
              </select>
            </td>
          </tr>
        </tbody>
      </table>
      <div v-if="!filtered.length" class="empty-tip">
        {{ rows.length ? '没有符合筛选条件的论文' : '还没有论文，先去论文库上传吧' }}
      </div>
    </div>

    <!-- 编辑弹窗 -->
    <div v-if="edit.open" class="modal-mask" @click.self="edit.open = false">
      <div class="modal" style="width:720px;">
        <h3>阅读记录 · {{ rows.find(r => r.id === edit.id)?.title_zh || '' }}</h3>
        <div class="row" v-for="[k, label] in COLS" :key="k">
          <label>{{ label }}</label>
          <textarea class="input" v-model="edit.f[k]" rows="2"
                    :placeholder="k === 'research_problem' ? '这篇论文要解决什么问题？为什么重要？'
                      : k === 'solution' ? '核心方法 / 方案是什么？关键思路？'
                      : k === 'innovation' ? '创新点在哪？有什么我可以借鉴的？'
                      : k === 'my_thoughts' ? '你的思考、质疑、联想…'
                      : '用一句话总结你的收获'"></textarea>
        </div>
        <div style="display:grid; grid-template-columns:1fr 110px 110px; gap:10px;">
          <div class="row"><label>标签（逗号分隔）</label><input class="input" v-model="edit.f.tags" /></div>
          <div class="row"><label>状态</label>
            <select class="select" v-model="edit.f.status">
              <option v-for="s in ['在读', '读完', '搁置']" :key="s">{{ s }}</option>
            </select>
          </div>
          <div class="row"><label>评分</label>
            <select class="select" v-model.number="edit.f.rating">
              <option v-for="n in 5" :key="n" :value="n">{{ '★'.repeat(n) }}</option>
              <option :value="0">未评</option>
            </select>
          </div>
        </div>
        <div class="actions">
          <button class="btn" @click="edit.open = false">取消</button>
          <button class="btn primary" @click="saveEdit">保存</button>
        </div>
      </div>
    </div>

    <!-- 问题追踪抽屉 -->
    <template v-if="drawer.open">
      <div class="drawer-mask" @click="drawer.open = false"></div>
      <div class="drawer">
        <div class="d-head">
          <b style="max-width:340px; overflow:hidden; text-overflow:ellipsis; white-space:nowrap;">
            ❓ 问题追踪 · {{ drawer.title }}</b>
          <button class="btn sm" @click="drawer.open = false">✕ 关闭</button>
        </div>
        <div class="d-body">
          <div style="display:flex; gap:8px; margin-bottom:14px;">
            <input class="input" v-model="drawer.newQ" placeholder="记一个新问题…"
                   @keydown.enter="addQ" />
            <button class="btn primary" @click="addQ">添加</button>
          </div>
          <div v-for="q in drawer.items" :key="q.id" class="q-card" :class="{ resolved: q.status === 'resolved' }">
            <div style="display:flex; justify-content:space-between; gap:8px; align-items:flex-start;">
              <div class="q">{{ q.content }}</div>
              <div style="white-space:nowrap;">
                <button class="btn sm" @click="toggleQ(q)">
                  {{ q.status === 'open' ? '✓ 已解决' : '↩ 重新打开' }}</button>
                <button class="btn sm danger" @click="delQ(q)">🗑</button>
              </div>
            </div>
            <div class="muted">{{ q.created_at }}</div>
            <textarea class="input" v-model="q.answer" rows="2" placeholder="解答 / 思路（怎么解决的）"
                      v-if="q.answer || q.status === 'resolved'"
                      @blur="saveAnswer(q)"></textarea>
            <button v-else class="btn sm" style="margin-top:6px;"
                    @click="q.answer = ''">＋ 记录解答</button>
          </div>
          <div v-if="!drawer.items.length" class="empty-tip">这篇论文还没有记录问题</div>
        </div>
      </div>
    </template>
  </div>
</template>
