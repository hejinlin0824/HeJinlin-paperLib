<script setup>
// 章节解读卡：通俗理解 + 本章要点 + 思考问题（作答 → AI 点评）
import { computed, ref, watch } from 'vue'
import api from '../api'
import { toast } from '../stores'
import { renderInline, renderRich } from '../inline'

const props = defineProps({ section: Object, pid: String })
const emit = defineEmits(['updated'])

const open = ref(true)
const genLoading = ref(false)
const submittingQid = ref(0)
const localQs = ref([])

watch(() => props.section, (s) => {
  localQs.value = (s?.questions || []).map(q => ({ ...q }))
}, { immediate: true, deep: false })

const summaryHtml = computed(() => renderRich(props.section.summary || ''))
const reviewHtml = (r) => renderRich(r || '')
const takeHtml = (t) => renderInline(t || '')
const takeaways = computed(() =>
  (props.section.takeaway || '').split(/[\n；]/).map(x => x.trim()).filter(Boolean))

async function gen() {
  genLoading.value = true
  try {
    const s = await api.generateSection(props.pid, props.section.id)
    emit('updated', s)
    toast('本章解读已生成', 'ok')
  } catch (e) { toast(e.message, 'err') } finally { genLoading.value = false }
}
async function submit(q) {
  if (!q.my.trim()) { toast('先写下你的思考再提交', 'err'); return }
  submittingQid.value = q.id
  try {
    const s = await api.answerQuestion(props.pid, props.section.id, q.id, q.my)
    emit('updated', s)
    toast('AI 点评已生成', 'ok')
  } catch (e) { toast(e.message, 'err') } finally { submittingQid.value = 0 }
}
</script>

<template>
  <div class="sec-card" v-if="!section.skip">
    <div class="sc-head">
      <span class="sc-title">📖 本章解读 · {{ section.title }}</span>
      <span style="flex:1"></span>
      <button v-if="!section.summary" class="btn sm primary" :disabled="genLoading"
              @click.stop="gen">{{ genLoading ? '✨ 解读生成中…' : '✨ 生成本章解读' }}</button>
      <button v-else class="btn sm" @click.stop="open = !open">{{ open ? '收起' : '展开' }}</button>
    </div>
    <template v-if="section.summary && open">
      <div class="sc-sec">💡 通俗理解</div>
      <div class="sc-body" v-html="summaryHtml"></div>
      <template v-if="takeaways.length">
        <div class="sc-sec">📌 本章要点</div>
        <ul class="sc-take">
          <li v-for="(t, i) in takeaways" :key="i" v-html="takeHtml(t)"></li>
        </ul>
      </template>
      <template v-if="localQs.length">
        <div class="sc-sec">🤔 思考问题
          <span class="muted">（写下你的回答并提交，AI 会点评、纠偏并补充参考答案）</span>
        </div>
        <div class="sc-q" v-for="q in localQs" :key="q.id">
          <div class="sc-qt">
            <span v-if="q.kind" class="sc-kind">{{ q.kind }}</span>
            Q{{ q.id }}. {{ q.q }}
          </div>
          <textarea class="input" v-model="q.my" rows="2"
                    placeholder="写下你的思考…（不用怕答错，AI 会帮你分析）"></textarea>
          <div style="margin-top:6px;">
            <button class="btn sm primary" :disabled="submittingQid === q.id" @click="submit(q)">
              {{ submittingQid === q.id ? '🤖 AI 分析中…' : (q.review ? '↻ 重新提交' : '提交，看 AI 点评') }}</button>
          </div>
          <div v-if="q.review" class="sc-review" v-html="reviewHtml(q.review)"></div>
        </div>
      </template>
    </template>
  </div>
</template>
