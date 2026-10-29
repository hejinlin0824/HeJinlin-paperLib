<script setup>
// 设置：翻译引擎 / 解析引擎 / 术语表 / 数据备份
import { onMounted, reactive, ref } from 'vue'
import api from '../api'
import { toast } from '../stores'

const PROVIDERS = [
  ['deepseek', 'DeepSeek（推荐，便宜好用）'],
  ['zhipu', '智谱 GLM（glm-4-flash 免费）'],
  ['moonshot', 'Kimi（月之暗面）'],
  ['openai', 'OpenAI'],
  ['ollama', 'Ollama 本地模型（需本机安装）'],
  ['mock', 'Mock 测试引擎（无需联网）'],
  ['custom', '自定义（任意 OpenAI 兼容接口）'],
]

const form = reactive({
  translate_provider: 'mock', translate_base_url: '', translate_api_key: '',
  translate_model: '', translate_temperature: '0.3', translate_concurrency: '4',
  auto_translate: '1', parse_engine: 'builtin', mineru_token: '',
})
const presets = ref({})
const testResult = ref('')
const parseResult = ref('')
const testing = ref(false)
const translateKeyConfigured = ref(false)
const mineruTokenConfigured = ref(false)
const clearTranslateKeyOnSave = ref(false)
const clearMineruTokenOnSave = ref(false)

const glossary = ref([])
const gnew = reactive({ term_en: '', term_zh: '', note: '' })

async function load() {
  const d = await api.settings()
  presets.value = d.presets || {}
  const safeSettings = { ...d.settings }
  translateKeyConfigured.value = Boolean(safeSettings.translate_api_key_configured)
  mineruTokenConfigured.value = Boolean(safeSettings.mineru_token_configured)
  delete safeSettings.translate_api_key_configured
  delete safeSettings.mineru_token_configured
  Object.assign(form, safeSettings)
  glossary.value = await api.glossary()
}

function onProviderChange() {
  const p = presets.value[form.translate_provider]
  if (p) {
    form.translate_base_url = p.base_url || ''
    form.translate_model = p.model || ''
  }
  testResult.value = ''
}

async function persistSettings() {
  const d = await api.saveSettings({
    ...form,
    clear_translate_api_key: clearTranslateKeyOnSave.value,
    clear_mineru_token: clearMineruTokenOnSave.value,
  })
  translateKeyConfigured.value = Boolean(d.translate_api_key_configured)
  mineruTokenConfigured.value = Boolean(d.mineru_token_configured)
  clearTranslateKeyOnSave.value = false
  clearMineruTokenOnSave.value = false
}
function clearTranslateKey() {
  form.translate_api_key = ''
  clearTranslateKeyOnSave.value = true
  translateKeyConfigured.value = false
}
function clearMineruToken() {
  form.mineru_token = ''
  clearMineruTokenOnSave.value = true
  mineruTokenConfigured.value = false
}
async function save() {
  try {
    await persistSettings()
    toast('设置已保存', 'ok')
  } catch (e) {
    toast('保存失败：' + e.message, 'err')
  }
}
async function testEngine() {
  testing.value = true
  try {
    await persistSettings()
    const r = await api.testTranslate()
    testResult.value = r.message + (r.latency ? `（${r.latency}s）` : '')
    toast(r.ok ? '测试通过' : '测试失败', r.ok ? 'ok' : 'err')
  } catch (e) {
    testResult.value = e.message
    toast('保存或测试失败：' + e.message, 'err')
  } finally {
    testing.value = false
  }
}
async function testParse() {
  try {
    await persistSettings()
    const r = await api.testParse()
    parseResult.value = r.message
    toast(r.ok ? '可用' : '不可用', r.ok ? 'ok' : 'err')
  } catch (e) {
    parseResult.value = e.message
    toast('保存或测试失败：' + e.message, 'err')
  }
}

async function addTerm() {
  if (!gnew.term_en.trim()) return
  await api.addGlossary({ ...gnew })
  gnew.term_en = ''; gnew.term_zh = ''; gnew.note = ''
  glossary.value = await api.glossary()
}
async function saveTerm(g) {
  await api.updateGlossary(g.id, { term_zh: g.term_zh, note: g.note })
  toast('已保存', 'ok')
}
async function delTerm(g) {
  await api.deleteGlossary(g.id)
  glossary.value = glossary.value.filter(x => x.id !== g.id)
}

onMounted(load)
</script>

<template>
  <div style="max-width:860px;">
    <h1 class="page-title">设置</h1>
    <p class="page-sub">所有数据保存在本地，不上传任何服务器</p>

    <!-- 翻译引擎 -->
    <div class="card set-section">
      <h3>🤖 翻译引擎</h3>
      <div class="desc">用于整篇翻译、逐段重译、标题翻译。走 OpenAI 兼容协议，换引擎只改这里。
        机器带不动本地模型的话，推荐 DeepSeek 或免费的 glm-4-flash。</div>
      <div class="form-grid">
        <label>服务商</label>
        <select class="select" v-model="form.translate_provider" @change="onProviderChange">
          <option v-for="[k, label] in PROVIDERS" :key="k" :value="k">{{ label }}</option>
        </select>
        <label>API 地址</label>
        <input class="input" v-model="form.translate_base_url" placeholder="https://api.deepseek.com/v1" />
        <label>API Key</label>
        <div>
          <input class="input" type="password" v-model="form.translate_api_key"
                 autocomplete="new-password" placeholder="输入新 Key；已有 Key 留空即可保留" />
          <div class="desc" style="margin-top:4px">
            {{ translateKeyConfigured && !form.translate_api_key ? '已保存。服务端不会回传密钥；留空代表不修改。' : '密钥仅保存在本机配置数据库中。' }}
            <button v-if="translateKeyConfigured" class="btn sm danger" style="margin-left:6px"
                    type="button" @click="clearTranslateKey">清除已保存 Key</button>
          </div>
        </div>
        <label>模型</label>
        <input class="input" v-model="form.translate_model" placeholder="deepseek-chat" />
        <label>温度</label>
        <input class="input" v-model="form.translate_temperature" style="width:120px;" />
        <label>并发数</label>
        <input class="input" v-model="form.translate_concurrency" style="width:120px;" />
        <label>解析后自动翻译</label>
        <label style="display:flex; align-items:center; gap:8px; font-weight:400;">
          <input type="checkbox" :checked="form.auto_translate === '1'"
                 @change="form.auto_translate = $event.target.checked ? '1' : '0'" />
          开启（关闭后解析完停在「待翻译」，手动点翻译）</label>
      </div>
      <div style="display:flex; gap:10px; margin-top:16px; align-items:center;">
        <button class="btn primary" @click="save">保存设置</button>
        <button class="btn" :disabled="testing" @click="testEngine">{{ testing ? '测试中…' : '测试连接' }}</button>
        <span v-if="testResult" class="muted">{{ testResult }}</span>
      </div>
    </div>

    <!-- 解析引擎 -->
    <div class="card set-section">
      <h3>📄 PDF 解析引擎</h3>
      <div class="desc">内置引擎零依赖、速度快，公式无法还原为 LaTeX；MinerU（云端）公式表格最佳，
        需在 mineru.net 申请 API Token（每日有免费额度）。</div>
      <div class="form-grid">
        <label>引擎</label>
        <select class="select" v-model="form.parse_engine">
          <option value="builtin">内置引擎（默认，零配置）</option>
          <option value="mineru_api">MinerU 云端（公式/表格最佳）</option>
          <option value="mineru_local">MinerU 本地（需 pip install mineru）</option>
        </select>
        <template v-if="form.parse_engine === 'mineru_api'">
          <label>MinerU Token</label>
          <div>
            <input class="input" type="password" v-model="form.mineru_token"
                   autocomplete="new-password" placeholder="输入新 Token；已有 Token 留空即可保留" />
            <div class="desc" style="margin-top:4px">
              {{ mineruTokenConfigured && !form.mineru_token ? '已保存。留空代表不修改。' : '云端解析时会将 PDF 上传给 MinerU。' }}
              <button v-if="mineruTokenConfigured" class="btn sm danger" style="margin-left:6px"
                      type="button" @click="clearMineruToken">清除已保存 Token</button>
            </div>
          </div>
        </template>
      </div>
      <div style="display:flex; gap:10px; margin-top:16px; align-items:center;">
        <button class="btn primary" @click="save">保存设置</button>
        <button class="btn" @click="testParse">测试解析引擎</button>
        <span v-if="parseResult" class="muted">{{ parseResult }}</span>
      </div>
    </div>

    <!-- 术语表 -->
    <div class="card set-section">
      <h3>📖 术语表</h3>
      <div class="desc">翻译时注入每次请求，保证全文术语一致。翻译完成后也会自动沉淀本文术语（来源=auto）。</div>
      <div style="display:flex; gap:8px; margin-bottom:12px;">
        <input class="input" v-model="gnew.term_en" placeholder="英文术语，如 attention" />
        <input class="input" v-model="gnew.term_zh" placeholder="中文译名，如 注意力机制" />
        <input class="input" v-model="gnew.note" placeholder="备注（可选）" />
        <button class="btn primary" @click="addTerm">添加</button>
      </div>
      <div class="tbl-wrap" style="max-height:300px;">
        <table class="tbl">
          <thead><tr><th style="width:200px;">英文</th><th style="width:220px;">中文</th><th>备注</th><th>来源</th><th style="width:110px;"></th></tr></thead>
          <tbody>
            <tr v-for="g in glossary" :key="g.id">
              <td><b>{{ g.term_en }}</b></td>
              <td><input class="input" v-model="g.term_zh" style="padding:4px 8px;" /></td>
              <td><input class="input" v-model="g.note" style="padding:4px 8px;" /></td>
              <td><span class="badge" :class="g.source === 'auto' ? 'blue' : 'gray'">{{ g.source === 'auto' ? '自动' : '手动' }}</span></td>
              <td style="white-space:nowrap;">
                <button class="btn sm" @click="saveTerm(g)">保存</button>
                <button class="btn sm danger" @click="delTerm(g)">删</button>
              </td>
            </tr>
          </tbody>
        </table>
        <div v-if="!glossary.length" class="empty-tip">术语表为空，添加第一条吧</div>
      </div>
    </div>

    <!-- 数据 -->
    <div class="card set-section">
      <h3>💾 数据备份</h3>
      <div class="desc">所有数据都在项目的 data/ 目录（SQLite + PDF + 图片）。
        一键打包下载；恢复 = 解压覆盖 data 目录后重启。</div>
      <a class="btn primary" href="/api/backup.zip" download>⤓ 下载完整备份（zip）</a>
    </div>
  </div>
</template>
