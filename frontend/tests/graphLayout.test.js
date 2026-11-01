import test from 'node:test'
import assert from 'node:assert/strict'
import { computeCameraFit, computeSemanticLayout, toFramedCameraPoint } from '../src/utils/graphLayout.js'
const nodes = (type, count, prefix = type) => Array.from({ length: count }, (_, i) => ({
  id: prefix + '-' + i, type, degree: count - i,
  label_zh: i === 0 ? 'Generalizability in LLM-based Agents' : '研究概念' + i,
}))
test('47 concepts wrap into four columns and preserve unique coordinates', () => {
  const result = computeSemanticLayout(nodes('Concept', 47))
  assert.equal(result.lanes[0].columns, 4)
  assert.equal(result.lanes[0].nodeCount, 47)
  const coords = Object.values(result.positions).map(p => p.x + ',' + p.y)
  assert.equal(new Set(coords).size, 47)
  assert.ok(Math.max(...Object.values(result.positions).map(p => p.y)) <= 506)
})
test('wrapped lane reserves its full width before the next semantic lane', () => {
  const result = computeSemanticLayout([...nodes('Concept', 47, 'concept'), ...nodes('Dataset', 4, 'dataset')])
  const concept = result.lanes.find(lane => lane.lane === 4)
  const dataset = result.lanes.find(lane => lane.lane === 5)
  assert.ok(dataset.startX > concept.endX)
  assert.ok(dataset.startX - concept.endX >= 104)
})
test('mixed semantic lanes never overlap horizontally', () => {
  const result = computeSemanticLayout([...nodes('Paper', 3), ...nodes('ResearchProblem', 5),
    ...nodes('Algorithm', 8), ...nodes('Concept', 47), ...nodes('Dataset', 10), ...nodes('Finding', 2)])
  for (let i = 1; i < result.lanes.length; i++) assert.ok(result.lanes[i].startX > result.lanes[i - 1].endX)
})
test('large graphs remain finite and every column respects the row limit', () => {
  const result = computeSemanticLayout([...nodes('Concept', 100), ...nodes('Algorithm', 60), ...nodes('Dataset', 40)])
  assert.equal(result.lanes.reduce((sum, lane) => sum + lane.columns, 0), 18)
  for (const p of Object.values(result.positions)) {
    assert.ok(Number.isFinite(p.x) && Number.isFinite(p.y))
    assert.ok(p.row >= 0 && p.row < 12)
  }
})
test('unknown categories use a dedicated miscellaneous lane', () => {
  assert.equal(computeSemanticLayout([{ id: 'x', type: 'Unknown' }]).positions.x.lane, 8)
})
test('empty graph returns safe bounds and camera defaults', () => {
  assert.deepEqual(computeSemanticLayout([]).bounds, null)
  assert.deepEqual(computeCameraFit(null, { width: 0, height: 0 }), { x: 0, y: 0, ratio: 1 })
})
test('fit-all camera centers non-origin bounds and includes both dimensions', () => {
  const fit = computeCameraFit({ minX: 1000, maxX: 5000, minY: -400, maxY: 400 },
    { width: 1000, height: 600 }, { padding: 50 })
  assert.equal(fit.x, 3000)
  assert.equal(fit.y, 0)
  assert.equal(fit.ratio, 4.1)
})

test('camera targets convert graph-space centers into Sigma framed-graph coordinates', () => {
  const renderer = { graphToFramedGraph: ({ x, y }) => ({ x: (x - 1000) / 4000, y: (y + 400) / 800 }) }
  assert.deepEqual(toFramedCameraPoint(renderer, { x: 3000, y: 0 }), { x: 0.5, y: 0.5 })
  assert.deepEqual(toFramedCameraPoint(null, { x: 3000, y: 0 }), { x: 0.5, y: 0.5 })
  assert.deepEqual(toFramedCameraPoint({ graphToFramedGraph: () => ({ x: NaN, y: 2 }) }, {}), { x: 0.5, y: 0.5 })
})
