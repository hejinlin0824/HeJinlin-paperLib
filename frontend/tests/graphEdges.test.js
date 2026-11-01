import test from 'node:test'
import assert from 'node:assert/strict'
import { groupRelationsByEndpoints } from '../src/utils/graphEdges.js'

test('parallel relation types between the same directed endpoints are grouped', () => {
  const grouped = groupRelationsByEndpoints([
    { id: 'r1', source: 'paper-a', target: 'algorithm-x', relation_type: 'PROPOSES' },
    { id: 'r2', source: 'paper-a', target: 'algorithm-x', relation_type: 'COMPARES' },
    { id: 'r3', source: 'paper-b', target: 'dataset-y', relation_type: 'USES' },
  ])
  assert.equal(grouped.length, 2)
  assert.deepEqual(grouped[0].relations.map(r => r.id), ['r1', 'r2'])
  assert.equal(grouped[0].source, 'paper-a')
  assert.equal(grouped[0].target, 'algorithm-x')
  assert.deepEqual(grouped[1].relations.map(r => r.id), ['r3'])
})

test('opposite directions remain separate because relation direction is meaningful', () => {
  const grouped = groupRelationsByEndpoints([
    { id: 'forward', source: 'paper-a', target: 'paper-b' },
    { id: 'reverse', source: 'paper-b', target: 'paper-a' },
  ])
  assert.equal(grouped.length, 2)
})

test('invalid edges are ignored instead of creating an unusable graph edge', () => {
  const grouped = groupRelationsByEndpoints([
    { id: 'missing-target', source: 'paper-a' },
    { source: 'paper-a', target: 'dataset-a' },
    null,
  ])
  assert.equal(grouped.length, 0)
})
