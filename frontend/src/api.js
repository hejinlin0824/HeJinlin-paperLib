// 统一 API 封装
async function request(path, { method = 'GET', body, form } = {}) {
  const opts = { method }
  if (form) {
    opts.body = form
  } else if (body !== undefined) {
    opts.headers = { 'Content-Type': 'application/json' }
    opts.body = JSON.stringify(body)
  }
  const r = await fetch(path, opts)
  if (!r.ok) {
    let msg = `HTTP ${r.status}`
    try { msg = (await r.json()).detail || msg } catch { /* ignore */ }
    throw new Error(msg)
  }
  return r.json()
}

export const api = {
  // 论文
  uploadPdf(file) {
    const fd = new FormData()
    fd.append('file', file)
    return request('/api/papers/upload', { method: 'POST', form: fd })
  },
  papers: () => request('/api/papers'),
  paper: (id) => request(`/api/papers/${id}`),
  patchPaper: (id, body) => request(`/api/papers/${id}`, { method: 'PATCH', body }),
  deletePaper: (id) => request(`/api/papers/${id}`, { method: 'DELETE' }),
  reparse: (id, engine) => request(`/api/papers/${id}/reparse`, { method: 'POST', body: { engine } }),
  translatePaper: (id, force = false) =>
    request(`/api/papers/${id}/translate`, { method: 'POST', body: { force } }),
  blocks: (id) => request(`/api/papers/${id}/blocks`),
  retranslateBlock: (id, bid) => request(`/api/papers/${id}/blocks/${bid}/retranslate`, { method: 'POST' }),
  editBlockZh: (id, bid, zh) => request(`/api/papers/${id}/blocks/${bid}`, { method: 'PATCH', body: { zh } }),
  generateGuide: (id) => request(`/api/papers/${id}/guide`, { method: 'POST' }),
  explainPaper: (id) => request(`/api/papers/${id}/explain`, { method: 'POST' }),
  sections: (id) => request(`/api/papers/${id}/sections`),
  generateSection: (id, sid) => request(`/api/papers/${id}/sections/${sid}/generate`, { method: 'POST' }),
  answerQuestion: (id, sid, qid, answer) =>
    request(`/api/papers/${id}/sections/${sid}/answer`, { method: 'PATCH', body: { qid, answer } }),
  saveProgress: (id, block_idx, ratio) =>
    request(`/api/papers/${id}/progress`, { method: 'PUT', body: { block_idx, ratio } }),
  dashboard: () => request('/api/papers/dashboard'),
  // 批注
  annotations: (pid) => request(`/api/papers/${pid}/annotations`),
  addAnnotation: (pid, body) => request(`/api/papers/${pid}/annotations`, { method: 'POST', body }),
  updateAnnotation: (pid, aid, body) => request(`/api/papers/${pid}/annotations/${aid}`, { method: 'PATCH', body }),
  deleteAnnotation: (pid, aid) => request(`/api/papers/${pid}/annotations/${aid}`, { method: 'DELETE' }),
  // 问题
  questions: (paperId) => request(`/api/questions?paper_id=${paperId}`),
  addQuestion: (body) => request('/api/questions', { method: 'POST', body }),
  updateQuestion: (qid, body) => request(`/api/questions/${qid}`, { method: 'PATCH', body }),
  deleteQuestion: (qid) => request(`/api/questions/${qid}`, { method: 'DELETE' }),
  // 研究总表
  logList: () => request('/api/log'),
  saveLog: (pid, body) => request(`/api/log/${pid}`, { method: 'PUT', body }),
  // 设置
  settings: () => request('/api/settings'),
  saveSettings: (body) => request('/api/settings', { method: 'PUT', body }),
  testTranslate: (body = {}) => request('/api/settings/test-translate', { method: 'POST', body }),
  testParse: (body = {}) => request('/api/settings/test-parse', { method: 'POST', body }),
  glossary: () => request('/api/glossary'),
  dictCard: (word, ctx = '') => request(`/api/dict?word=${encodeURIComponent(word)}&ctx=${encodeURIComponent(ctx)}`),
  addGlossary: (body) => request('/api/glossary', { method: 'POST', body }),
  updateGlossary: (gid, body) => request(`/api/glossary/${gid}`, { method: 'PATCH', body }),
  deleteGlossary: (gid) => request(`/api/glossary/${gid}`, { method: 'DELETE' }),
}

export default api
