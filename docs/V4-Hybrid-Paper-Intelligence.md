# PaperLib V4 · Hybrid Paper Intelligence

V4 is a local-first, optional hybrid layer. It does not replace the existing V2 graph
or V3/V3.1 drafts. New V4 understanding drafts are versioned as 4.0 in the isolated
`kg_understanding_drafts` table; no automatic promotion to the production graph occurs.

## Processing architecture

1. **Document structure** — reuses MinerU/built-in parser blocks, section boundaries and
   original block IDs. No second PDF parser is introduced.
2. **Retrieval index** — SQLite stores block text and token lists. BM25-style lexical
   retrieval is always available, with no extra package and no network calls.
3. **Optional dense retrieval** — BGE-M3 is opt-in. Set `PAPERLIB_BGE_M3_PATH` to an
   already downloaded local model directory and install `backend/requirements-intelligence.txt`.
   The app never downloads a model automatically. Without this setting, retrieval is lexical.
4. **Academic NLP candidates** — conservative patterns detect definitions, assumptions,
   theorems/propositions/lemmas, necessary/sufficient conditions, proofs and limitations.
   Every candidate points to its source block and remains a candidate, not a verified fact.
5. **Bounded LLM reasoning** — V4 packs up to two adjacent short chunks into one request (with a character budget),
   including across section boundaries; each chunk keeps its own section, ID and
   independent JSON result. Missing/malformed batch items fall back to single-chunk calls,
   so partial batch output does not discard valid work. Then V4 performs one deterministic
   JSON compaction step in Python (no model call), followed by a single synthesis call and
   at most one batched relation-verification call. It does not call an LLM once per edge.
6. **Knowledge modeling** — evidence is checked against the original block map; ordinary
   directed relations and multi-premise theory hyperedges remain separate. Unverified
   edges are explicitly marked `unverified`.
7. **Graph context retrieval** — combines block retrieval with graph neighborhoods from human-approved V4 drafts only. Unreviewed candidates and noisy V2/V3 graph records are excluded; relation paths must also retain an explicit `entailed` verdict and pass exact-source revalidation.
8. **Evaluation** — the Harness reference fixture records expected concepts and negative
   cases. Automated tests verify pipeline contracts; semantic precision/recall still
   requires a human-checked gold annotation.

## 模型选型边界

- **BGE-M3**：用于中英混合的 Block 级语义检索；这是 V4 当前可选 dense retrieval 后端。
- **GLiNER**：用于可配置标签的实体/术语候选发现；输出必须回链到原始 Block，不能直接当作理论关系。
- **SciBERT**：是科学文本预训练编码器，不是开箱即用的 theorem/assumption 分类器。若没有人工标注训练集和任务头，直接把它放入线上推理只会增加依赖，不能可靠地产生结构标签，因此 V4 暂不把它作为必需运行时组件。
- **SPECTER2**：主要面向论文级学术文档表示和相关论文检索，不是段落级逻辑解析器。未来可用于跨论文推荐/研究主题聚类；当前 V4 先用 BGE-M3 解决中英混合 Block 检索，避免重复维护两个 embedding 空间。
- **Reranker**：可在建立标注查询集后评估 BGE reranker；当前 RRF 排序先提供稳定、无额外服务的 baseline。

这些是有意的工程取舍：专业模型应解决明确任务，而不是仅仅因为“学术模型”这个标签就全部部署。

## API

- `POST /api/knowledge-graph/v4/papers/{paper_id}/index` — build local lexical index.
- `POST /api/knowledge-graph/v4/papers/{paper_id}/index` with
  `{"include_embeddings": true}` — build lexical + local BGE-M3 vectors.
- `GET /api/knowledge-graph/v4/search?q=necessary%20conditions&paper_id=...` — BM25 + optional BGE-M3 hybrid block search.
- `GET /api/knowledge-graph/v4/graph-context?q=agent%20harness&paper_id=...` — retrieve related nodes and relation/hyperedge neighborhoods from approved V4 drafts.
- `GET /api/knowledge-graph/v4/papers/{paper_id}/structure-candidates` — rule-based candidates.
- `POST /api/knowledge-graph/v4/papers/{paper_id}/understand` — create an isolated V4 draft.
- `GET /api/knowledge-graph/v4/drafts` — list V4 and earlier isolated drafts.

## BGE-M3 setup (optional)

Download the BGE-M3 model yourself using an approved model source, then configure the
local directory before starting PaperLib:

```powershell
pip install -r backend/requirements-intelligence.txt
$env:PAPERLIB_BGE_M3_PATH = "E:\models\bge-m3"
$env:PAPERLIB_GLINER_PATH = "E:\models\gliner"  # optional
```

If the model path or optional package is unavailable, lexical-only mode remains the
default. FAISS/Qdrant/Neo4j are deliberately not required for the current single-user
SQLite library: block-level vectors are stored in SQLite and cosine-ranked in process.
A dedicated vector database can be introduced later if measured corpus size requires it.

## Failure and data-safety contract

- Failed chunks do not erase successful chunks or chapter summaries. Chunk requests run with bounded concurrency (maximum 3) and a V4-specific 90-second timeout / 2-attempt retry policy; legacy translation defaults are unchanged.
- Synthesis failure falls back to evidence-linked rule candidates and a recoverable draft. Rule outputs are explicitly tagged `rule_candidate`, never mislabeled as formal definitions.
- No model failure is allowed to write to V2 `kg_entities`, `kg_relations`, or `kg_evidence`.
- Index tables are separate from paper content and old graph tables.
- Dense embeddings are optional; no network request occurs during lexical indexing/search.
- The UI/API confirmation flow remains separate from formal graph import.
- Draft pipeline metadata records actual chunk-batch sizes, LLM operation count, and elapsed seconds for chunk analysis, synthesis, verification, and the overall run.

## Harness 论文质量评测

先在 V4 工作台生成一份新档案，再从 V4 任务列表复制 draft ID。执行只读评测：

```powershell
.\.venv\Scripts\python.exe tools\evaluate_paper_understanding.py --draft-id YOUR_DRAFT_ID
```

评测报告包含：
- `concept_recall_proxy`：目标概念的词面覆盖代理指标，不是语义召回率；
- `evidence_exact_match_rate`：节点/关系引文在原始 Block 中逐字定位的比例；
- `joint_sufficiency_hyperedge_shape_rate`：共同充分关系是否至少有两个前提；
- `unverified_edge_count`：尚未完成语义核验的关系数；
- 必须人工核对的负面案例清单。

要建立真正的 precision/recall 基准，仍需人工为 Harness PDF 标注 gold 节点、关系方向和逐字证据，再将 gold 数据扩展为可机器比对的标注集。当前 fixture 是验收规格，不伪装成完整金标准。

## Known limitations

- Regex academic candidates are intentionally conservative and are not a substitute for
  SciBERT/GLiNER fine-tuning or theorem parsing.
- The first dense adapter is BGE-M3 through FlagEmbedding; model quality and memory use
  need local measurement on the user's Windows machine.
- RRF combines lexical and dense rankings; weights should be tuned against a labeled set,
  not guessed from a single paper.
- The Harness fixture is a target specification, not a substitute for checking the source PDF.


## V4.1 engineering follow-up: bounded synthesis and shared primitives

- V3.1 and V4 now import section loading, chunk splitting, prompt construction, chunk/system prompts, and theory-logic evidence validation from `backend/app/services/paper_understanding_common.py`. V4 no longer imports or calls the V3.1 orchestration module; V3.1 keeps its existing pipeline and public helper names through imports, preserving historical behavior.
- Synthesis input is compacted locally with a 48,000-character budget by default. Low-priority open questions, candidate lists, and argument steps are removed before claim/evidence detail or summaries are shortened. Each successfully analyzed chunk retains a compact record and its section/chunk identity. The resulting input/output character counts, chunk counts, truncation flag, and retained evidence count are saved under `payload.pipeline.synthesis_input`.
- This is a character budget, not an exact tokenizer token budget. Different providers tokenize Chinese/English and JSON differently; the metrics are exposed so a provider-specific token budget can be added if needed.

### One-command Windows regression test

From the repository root, run:

```powershell
.\scripts\test_v4.ps1
```

The script runs backend pytest, frontend graph tests, and a Vite production build. It uses the repository's autouse pytest fixture, which redirects each test to a disposable SQLite database; it does not run a real model or modify the formal library database. If frontend dependencies are absent, it runs `npm ci` automatically.

Optional switches:

```powershell
.\scripts\test_v4.ps1 -InstallDependencies
.\scripts\test_v4.ps1 -LauncherPreflight
.\scripts\test_v4.ps1 -SkipFrontend
.\scripts\test_v4.ps1 -RunLiveModel -PaperId "<论文ID>"
```

The optional live mode starts a temporary local API server if port 8300 is not already serving PaperLib, submits one V4 understanding job for the selected paper, polls its draft, and writes `live-model-report.json`. It uses the model/provider configured in PaperLib settings and can incur API charges. It adds a V4 draft and local search-index rows to the selected paper's library database; it does not approve/import the draft into the formal knowledge graph. Do not use a paper ID unless you intend to create this test draft.

Logs and a JSON summary are written to a timestamped directory under `$env:TEMP\paperlib-v4-test-*`. The default command makes no real model API calls.
