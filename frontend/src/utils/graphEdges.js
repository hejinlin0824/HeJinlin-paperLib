/**
 * Collapse parallel semantic relations into one drawable connector per directed
 * endpoint pair. Keep every original relation so the UI can expose its own evidence.
 */
export function groupRelationsByEndpoints(edges = []) {
  const groups = new Map()
  for (const edge of edges) {
    if (!edge?.source || !edge?.target || !edge?.id) continue
    const key = JSON.stringify([edge.source, edge.target])
    if (!groups.has(key)) {
      groups.set(key, {
        key,
        source: edge.source,
        target: edge.target,
        relations: [],
      })
    }
    groups.get(key).relations.push(edge)
  }
  return [...groups.values()]
}
