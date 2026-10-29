// 行内内容渲染引擎：markdown 行内元素 + LaTeX 公式 + 引用标记 + 用户批注标记
import katex from 'katex'

const PATTERNS = [
  { re: /\$\$([^$]+?)\$\$/g, type: 'math', display: true },
  { re: /\$([^$\n]+?)\$/g, type: 'math', display: false },
  { re: /\\\((.+?)\\\)/g, type: 'math', display: false },
  { re: /\[([^\]\n]+)\]\(([^)\s]+)\)/g, type: 'link' },
  { re: /\[(\d+(?:\s*[,;\-–]\s*\d+)*)\]/g, type: 'cite' },
  { re: /\*\*([^*\n]+?)\*\*/g, type: 'strong' },
  { re: /==([^=\n]+?)==/g, type: 'aihl' },
  { re: /`([^`\n]+?)`/g, type: 'code' },
  { re: /\*([^*\n]+?)\*/g, type: 'em' },
]

export function escapeHtml(s) {
  return String(s ?? '').replace(/&/g, '&amp;').replace(/</g, '&lt;')
    .replace(/>/g, '&gt;').replace(/"/g, '&quot;')
}

function escapeRe(s) {
  return s.replace(/[.*+?^${}()|[\]\\]/g, '\\function escapeRe(s) {
  return s.replace(/[.*+?^${}()|[\]\\]/g, '\\$&')
}
')
}

function safeHref(raw) {
  const value = String(raw ?? '').trim()
  if (!value || /[\u0000-\u001f\u007f]/.test(value)) return null
  if (/^(https?:\/\/|mailto:)/i.test(value)) return value
  if (/^(?:#|\/(?!\/)|\.{1,2}\/)/.test(value)) return value
  return null
}

// 在原文中定位锚文本：精确 → 忽略大小写 → 忽略空白差异
export function findAnchor(raw, anchor) {
  if (!raw || !anchor) return -1
  let i = raw.indexOf(anchor)
  if (i >= 0) return i
  i = raw.toLowerCase().indexOf(anchor.toLowerCase())
  if (i >= 0) return i
  try {
    const re = new RegExp(anchor.trim().split(/\s+/).map(escapeRe).join('\\s+'), 'i')
    const m = re.exec(raw)
    if (m) return m.index
  } catch { /* ignore */ }
  return -1
}

function tokenize(text) {
  const tokens = []
  let pos = 0
  while (pos < text.length) {
    let bestIdx = Infinity, best = null
    for (const p of PATTERNS) {
      p.re.lastIndex = pos
      const m = p.re.exec(text)
      if (m && m.index < bestIdx) { bestIdx = m.index; best = { p, m } }
    }
    if (!best) {
      tokens.push({ type: 'text', value: text.slice(pos), start: pos, end: text.length })
      break
    }
    if (bestIdx > pos) tokens.push({ type: 'text', value: text.slice(pos, bestIdx), start: pos, end: bestIdx })
    const { p, m } = best
    tokens.push({ type: p.type, value: m[1], value2: m[2], display: p.display, start: bestIdx, end: bestIdx + m[0].length })
    pos = bestIdx + m[0].length
  }
  return tokens
}

function mathHtml(latex, display) {
  try {
    return katex.renderToString(latex, { throwOnError: false, displayMode: display, output: 'html' })
  } catch {
    return `<code class="math-err">${escapeHtml(latex)}</code>`
  }
}

export function renderDisplayMath(latex) {
  return mathHtml(latex, true)
}

/**
 * 渲染一段行内文本为 HTML。
 * marks: [{ anchor_text, cls: 'hl'|'ub', color, id }] —— 用户高亮/加粗标记
 */
export function renderInline(text, marks = []) {
  if (!text) return ''
  const tokens = tokenize(text)
  // 计算 marks 在全文中的区间
  const spans = []
  for (const mk of marks) {
    if (!mk?.anchor_text) continue
    const i = findAnchor(text, mk.anchor_text)
    if (i >= 0) spans.push({ s: i, e: i + mk.anchor_text.length, mk })
  }
  spans.sort((a, b) => a.s - b.s || b.e - a.e)
  const kept = []
  let lastEnd = -1
  for (const sp of spans) {
    if (sp.s >= lastEnd) { kept.push(sp); lastEnd = sp.e }
  }
  let html = ''
  for (const tok of tokens) {
    if (tok.type === 'text') {
      // 与该文本 token 相交的标记
      const local = kept.filter(sp => sp.e > tok.start && sp.s < tok.end)
      if (!local.length) { html += escapeHtml(tok.value); continue }
      let out = '', cur = tok.start
      for (const sp of local) {
        const ms = Math.max(sp.s, tok.start), me = Math.min(sp.e, tok.end)
        if (ms > cur) out += escapeHtml(text.slice(cur, ms))
        const piece = escapeHtml(text.slice(ms, me))
        out += sp.mk.cls === 'ub'
          ? `<span class="ub" data-anno="${sp.mk.id}">${piece}</span>`
          : `<mark class="hl" data-anno="${sp.mk.id}" style="background:${sp.mk.color || 'var(--hl-yellow)'}">${piece}</mark>`
        cur = me
      }
      if (cur < tok.end) out += escapeHtml(text.slice(cur, tok.end))
      html += out
    } else if (tok.type === 'math') {
      html += `<span class="imath">${mathHtml(tok.value, false)}</span>`
    } else if (tok.type === 'cite') {
      html += `<sup class="cite" data-cite="${tok.value}">[${tok.value}]</sup>`
    } else if (tok.type === 'link') {
      const href = safeHref(tok.value2)
      html += href
        ? `<a href="${escapeHtml(href)}" target="_blank" rel="noopener noreferrer">${escapeHtml(tok.value)}</a>`
        : escapeHtml(tok.value)
    } else if (tok.type === 'strong') {
      html += `<strong>${escapeHtml(tok.value)}</strong>`
    } else if (tok.type === 'aihl') {
      html += `<mark class="ai-hl">${escapeHtml(tok.value)}</mark>`
    } else if (tok.type === 'em') {
      html += `<em>${escapeHtml(tok.value)}</em>`
    } else if (tok.type === 'code') {
      html += `<code>${escapeHtml(tok.value)}</code>`
    }
  }
  return html
}

/**
 * 富文本渲染（AI 输出的解读/点评）：逐行状态机——markdown 表格行（| 开头）任意位置
 * 聚合成表格；"> " 行聚合为高亮引用条；其余按段落渲染行内元素（加粗/荧光/公式/引用等）。
 */
export function renderRich(text) {
  if (!text) return ''
  const out = []
  let para = []
  let tableBuf = []
  let quoteBuf = []

  const flushPara = () => {
    if (para.length) {
      out.push(`<p>${renderInline(para.join('\n')).replace(/\n/g, '<br>')}</p>`)
      para = []
    }
  }
  const flushTable = () => {
    if (tableBuf.length >= 2) {
      out.push(mdTableToHtml(tableBuf.join('\n')))
    } else if (tableBuf.length) {
      para.push(...tableBuf)
    }
    tableBuf = []
  }
  const flushQuote = () => {
    if (quoteBuf.length) {
      const inner = quoteBuf.map(l => renderInline(l.replace(/^>\s?/, ''))).join('<br>')
      out.push(`<blockquote class="rich-quote">${inner}</blockquote>`)
      quoteBuf = []
    }
  }

  for (const line of String(text).split('\n')) {
    const t = line.trim()
    if (t.startsWith('|')) {
      flushPara()
      flushQuote()
      tableBuf.push(line)
      continue
    }
    flushTable()
    if (t.startsWith('>')) {
      flushPara()
      quoteBuf.push(t)
      continue
    }
    flushQuote()
    if (!t) {
      flushPara()
      continue
    }
    para.push(line)
  }
  flushTable()
  flushQuote()
  flushPara()
  return out.join('')
}

// markdown 表格 → HTML 表格（用于 meta.md）
export function mdTableToHtml(md) {
  const lines = md.split('\n').filter(l => l.trim().startsWith('|'))
  if (!lines.length) return ''
  const rows = lines.map(l => l.trim().replace(/^\|/, '').replace(/\|$/, '').split('|').map(c => c.trim()))
  // 去掉分隔行 |---|---|
  const body = rows.filter(r => !r.every(c => /^:?-{2,}:?$/.test(c)))
  const head = body.shift()
  let html = '<table><thead><tr>' + head.map(c => `<th>${escapeHtml(c)}</th>`).join('') + '</tr></thead><tbody>'
  for (const r of body) html += '<tr>' + r.map(c => `<td>${escapeHtml(c)}</td>`).join('') + '</tr>'
  return html + '</tbody></table>'
}

const SAFE_HTML_TAGS = new Set([
  'table', 'thead', 'tbody', 'tfoot', 'tr', 'th', 'td', 'caption',
  'div', 'p', 'br', 'span', 'em', 'strong', 'b', 'i', 'sub', 'sup',
  'code', 'pre', 'ul', 'ol', 'li', 'a',
])
const DROP_HTML_CONTENT = new Set([
  'script', 'style', 'iframe', 'object', 'embed', 'svg', 'math',
  'form', 'input', 'button', 'textarea', 'select', 'option',
  'meta', 'link', 'base', 'template', 'noscript',
])

function appendSafeNode(node, target, doc) {
  if (node.nodeType === 3) {
    target.appendChild(doc.createTextNode(node.nodeValue || ''))
    return
  }
  if (node.nodeType !== 1) return
  const tag = node.tagName.toLowerCase()
  if (DROP_HTML_CONTENT.has(tag)) return
  if (!SAFE_HTML_TAGS.has(tag)) {
    for (const child of [...node.childNodes]) appendSafeNode(child, target, doc)
    return
  }

  const clean = doc.createElement(tag)
  if (tag === 'a') {
    const href = safeHref(node.getAttribute('href'))
    if (href) {
      clean.setAttribute('href', href)
      clean.setAttribute('target', '_blank')
      clean.setAttribute('rel', 'noopener noreferrer')
    }
    const title = node.getAttribute('title')
    if (title) clean.setAttribute('title', title.slice(0, 200))
  }
  if (tag === 'td' || tag === 'th') {
    for (const attr of ['colspan', 'rowspan']) {
      const value = node.getAttribute(attr)
      if (value && /^\d{1,3}$/.test(value) && Number(value) >= 1 && Number(value) <= 100) {
        clean.setAttribute(attr, value)
      }
    }
    const scope = node.getAttribute('scope')
    if (['row', 'col', 'rowgroup', 'colgroup'].includes(scope)) clean.setAttribute('scope', scope)
    const align = node.getAttribute('align')
    if (['left', 'center', 'right'].includes((align || '').toLowerCase())) {
      clean.setAttribute('align', align.toLowerCase())
    }
  }
  for (const child of [...node.childNodes]) appendSafeNode(child, clean, doc)
  target.appendChild(clean)
}

/**
 * MinerU may provide table HTML derived from document content. Parse it detached
 * and rebuild from a strict allowlist; regex-only removal of <script> and on* is
 * insufficient for malformed HTML, SVG, dangerous URLs and attribute injection.
 */
export function sanitizeHtml(h) {
  const source = String(h ?? '')
  if (typeof DOMParser === 'undefined') return escapeHtml(source)
  const parsed = new DOMParser().parseFromString(source, 'text/html')
  const out = parsed.createElement('div')
  for (const child of [...parsed.body.childNodes]) appendSafeNode(child, out, parsed)
  return out.innerHTML
}
