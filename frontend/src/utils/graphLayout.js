/**
 * Deterministic semantic-lane layout. Lanes encode categories, not semantic distance.
 */
export const DEFAULT_LAYOUT = Object.freeze({
  maxRows: 12, rowGap: 92, subcolumnGap: 264, laneGap: 104,
  nodeAllowance: 28, fontSize: 12, maxLabelWidth: 240,
})
export const TYPE_LANE = Object.freeze({
  Paper: 0, ResearchProblem: 1, Algorithm: 2, Model: 3,
  Concept: 4, Task: 4, Dataset: 5, Metric: 5,
  Finding: 6, ResearchArea: 6, Author: 7, Venue: 7, Institution: 7,
})
function labelWidth(label, fontSize, cap) {
  let units = 0
  for (const char of String(label ?? '')) units += /[\u2e80-\u9fff\uff00-\uffef]/u.test(char) ? 1 : 0.58
  return Math.min(cap, Math.max(fontSize * 3, units * fontSize))
}
function compareNodes(a, b) {
  const degree = (Number(b.degree) || 0) - (Number(a.degree) || 0)
  if (degree) return degree
  const al = String(a.label_zh || a.label || a.id)
  const bl = String(b.label_zh || b.label || b.id)
  return al.localeCompare(bl, 'zh-CN') || String(a.id).localeCompare(String(b.id))
}
export function computeSemanticLayout(nodes = [], options = {}) {
  const config = { ...DEFAULT_LAYOUT, ...options }
  const lanes = new Map()
  for (const node of nodes) {
    if (!node || node.id == null) continue
    const lane = TYPE_LANE[node.type] ?? 8
    if (!lanes.has(lane)) lanes.set(lane, [])
    lanes.get(lane).push(node)
  }
  const positions = {}
  const laneInfo = []
  let cursorX = 0
  let minY = Infinity
  let maxY = -Infinity
  for (const lane of [...lanes.keys()].sort((a, b) => a - b)) {
    const items = lanes.get(lane).slice().sort(compareNodes)
    const columns = Math.max(1, Math.ceil(items.length / config.maxRows))
    const maxWidth = Math.max(config.fontSize * 3,
      ...items.map(node => labelWidth(node.label_zh || node.label || node.id, config.fontSize, config.maxLabelWidth)))
    const labelReserve = maxWidth + config.nodeAllowance
    const columnPitch = Math.max(config.subcolumnGap, labelReserve + config.nodeAllowance)
    const laneStart = cursorX
    items.forEach((node, index) => {
      const column = Math.floor(index / config.maxRows)
      const row = index % config.maxRows
      const rowsInColumn = Math.min(config.maxRows, items.length - column * config.maxRows)
      const y = (row - (rowsInColumn - 1) / 2) * config.rowGap
      const x = laneStart + column * columnPitch
      positions[node.id] = { x, y, lane, column, row }
      minY = Math.min(minY, y)
      maxY = Math.max(maxY, y)
    })
    const laneEnd = laneStart + (columns - 1) * columnPitch + labelReserve
    laneInfo.push({ lane, startX: laneStart, endX: laneEnd, columns,
      nodeCount: items.length, labelReserve, columnPitch })
    cursorX = laneEnd + config.laneGap
  }
  const ids = Object.keys(positions)
  const bounds = ids.length ? { minX: 0, maxX: Math.max(0, cursorX - config.laneGap), minY, maxY } : null
  return { positions, lanes: laneInfo, bounds }
}
export function computeCameraFit(bounds, viewport, options = {}) {
  const padding = Number.isFinite(options.padding) ? options.padding : 90
  const minRatio = Number.isFinite(options.minRatio) ? options.minRatio : 0.06
  const maxRatio = Number.isFinite(options.maxRatio) ? options.maxRatio : 40
  const width = Math.max(1, Number(viewport?.width) || 1)
  const height = Math.max(1, Number(viewport?.height) || 1)
  if (!bounds || ![bounds.minX, bounds.maxX, bounds.minY, bounds.maxY].every(Number.isFinite)) {
    return { x: 0, y: 0, ratio: 1 }
  }
  const graphWidth = Math.max(1, bounds.maxX - bounds.minX + padding * 2)
  const graphHeight = Math.max(1, bounds.maxY - bounds.minY + padding * 2)
  const ratio = Math.max(graphWidth / width, graphHeight / height)
  return { x: (bounds.minX + bounds.maxX) / 2, y: (bounds.minY + bounds.maxY) / 2,
    ratio: Math.min(maxRatio, Math.max(minRatio, ratio)) }
}

/** Convert a graph-space point to the normalized coordinates expected by Sigma's camera. */
export function toFramedCameraPoint(renderer, point) {
  if (!renderer || typeof renderer.graphToFramedGraph !== 'function') return { x: 0.5, y: 0.5 }
  const framed = renderer.graphToFramedGraph(point)
  if (!framed || !Number.isFinite(framed.x) || !Number.isFinite(framed.y)) return { x: 0.5, y: 0.5 }
  return { x: framed.x, y: framed.y }
}
