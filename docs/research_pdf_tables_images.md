# How PDFs with tables and images are ingested for vector databases

Researched 2026-10-09 for this project's Docling pipeline. Each claim names its source and how strong it is. **Primary** means a paper abstract or official documentation I fetched. **Secondary** means a blog or summary I only saw through search. I could not verify numbers that appear only in vendor blogs.

## 1. The patterns people use

| Element | Pattern | Evidence |
|---|---|---|
| Layout | Parse the PDF into typed elements (text, table, picture) first. Structure-aware parsers such as Docling do this; one pipeline also drops text fragments under 60 characters as parsing noise. | Docling docs (primary); [arXiv 2511.16654](https://arxiv.org/abs/2511.16654) (primary) |
| Tables | Serialize to text (Markdown, HTML, CSV, or natural-language rows), embed the text, and hand the table to the generator. | Secondary guides; the table-format paper below |
| Images, option A: caption then embed | A vision model writes a description, only the text is embedded, and (in the LangChain variant) the raw image is kept in a document store and returned at answer time. | [LangChain post](https://blog.langchain.com/semi-structured-multi-modal-rag) (secondary) |
| Images, option B: multimodal embedding | Embed the image itself with CLIP-style or other multimodal models into a shared vector space, with no text step. | Same LangChain post; arXiv 2511.16654 |
| Whole pages as images | ColPali/ColQwen render each page to an image and store one vector per image patch, scored by late interaction (MaxSim), skipping OCR and parsing. | [ColPali paper, ICLR 2025](https://arxiv.org/abs/2407.01449) (primary, abstract only) |

## 2. What the evidence says

**Images: direct multimodal embedding beat caption-then-embed, on a small test.**
[Lumer et al., arXiv 2511.16654](https://arxiv.org/abs/2511.16654) compared storing LLM summaries of images against embedding the images natively. On a new benchmark of 40 question-answer pairs from financial earnings calls, with 6 LLMs and 2 multimodal embedding models, the native approach gained an absolute 13% mAP@5 and 11% nDCG (32% and 20% relative), and an LLM judge rated its answers more accurate. Treat it as directional: it is 40 questions in one domain, and the authors state no limitations. The cost they accept is a multimodal embedding model and index.

**Captioning is the older, cheaper pattern and has known loss.** Summaries can lose visual context, spatial relations and numeric precision (secondary). LangChain describes three variants: CLIP embeddings, text-only summaries, and summaries that return the raw image for a multimodal LLM to read ([source](https://blog.langchain.com/semi-structured-multi-modal-rag)). A practitioner write-up there says CLIP-style retrieval can struggle with charts and tables, and that summary plus raw image is better for complex figures. No benchmark compares these on one dataset.

**Tables: the serialization format changes retrieval, and no format wins everywhere.**
[arXiv 2604.24040](https://arxiv.org/abs/2604.24040) tested CSV, TSV, HTML, Markdown and DDL with MPNet, BGE-M3, ReasonIR and SPLADE. Equivalent formats produced "substantially different" embeddings and results, and averaging across formats outperformed any single one. The abstract gives no numbers. So Markdown is a reasonable default, not a proven best. Secondary sources add two practical points:
- A row without its column headers is meaningless once split from the table (a Q3 revenue row with no company or period), so each row or chunk should carry the headers.
- Enriched serialization, meaning rows rewritten as natural-language statements, beat key-value formats in a Chalmers thesis comparing nine strategies. One vendor blog claims a 20.6% accuracy gain from header-value pairs but publishes no method. I treat both as unverified.

**LLM summaries of tables help routing but not exact lookups (anecdotal).** The common pattern is to embed a summary and pass the raw table to the generator. A forum practitioner reported summaries were not enough for value-level lookups and too costly across tens of thousands of chunks.

**Docling's own chunkers handle the table problem directly** ([chunking docs](https://docling-project.github.io/docling/concepts/chunking/), primary):
- `HierarchicalChunker` makes one chunk per element, so a table is one chunk, with headers and captions attached as metadata.
- `HybridChunker` splits only chunks over the token limit and merges small neighbours that share headings and captions.
- When a table spans chunks, `repeat_table_header` (on by default) repeats the header in each; `omit_header_on_overflow` drops it for rows that would otherwise overflow.
- `contextualize()` returns the chunk text enriched with its metadata (such as the heading path) for embedding.
- The tokenizer should match the embedding model so token limits are measured correctly.
- The page says nothing specific about pictures.

**Docling can also describe and classify pictures itself** ([enrichment docs](https://docling-project.github.io/docling/usage/enrichments/), primary), all off by default:
- `do_picture_classification` labels figures (chart, flow diagram, logo, signature) with `DocumentFigureClassifier-v2.5`.
- `do_picture_description` uses SmolVLM-256M, Granite Vision, any Hugging Face VLM, or a remote endpoint including Ollama (`PictureDescriptionApiOptions`, which needs `enable_remote_services=True`).
- These add extra model runs and noticeable processing time.

**Page-image retrieval (ColPali/ColQwen) is the strongest claim and the heaviest.** The paper says it "largely outperforms modern document retrieval pipelines" on ViDoRe; the abstract has no numbers and I did not read the full paper. Blog claims of 15 to 40 point gains are unverified. Secondary sources agree on the cost: many patch vectors per page (roughly 1,000 to 2,000 depending on the model), more GPU memory, and a multi-vector index with MaxSim scoring, which a single-vector `pgvector` column does not provide.

## 3. How this project compares

| This project | Literature | Gap |
|---|---|---|
| Table = one Markdown chunk, truncated at 6,000 characters, embedded with bge-m3 | Split long tables by row groups and repeat the header; Docling does this | Rows past 6,000 characters are lost. A table over the limit is silently cut. |
| Image = caption from `gemma4:e2b`, embedded; the answer model only reads the caption | Option A. LangChain's variant also returns the raw image to a multimodal LLM. | The answer model never sees the figure. `gemma4:e2b` is vision-capable, so this is possible, but the image would have to be stored with the same access control as its chunks. |
| Prose = 1,200-character window, all-minilm | Structure-aware, tokenizer-aligned chunks with heading paths | Chunks can cut across sections and carry no heading context. |
| Logos removed by a 100 px size rule | Docling's classifier labels logos and signatures | The size rule is a heuristic; the classifier is more accurate but costs another model. |
| Markdown only | Format sensitivity is model-dependent | Never measured on our own tables. |

## 4. Suggested next steps, in order of value for effort

1. **Stop truncating tables.** Split a long table into row groups, each with its header and caption, one chunk per group (Docling's `repeat_table_header` behaviour). Small change in `loaders.py` and the indexer.
2. **Put context in table and figure chunks.** Prepend the caption and nearest heading (already partly there for tables) so a chart caption such as "Figure 10" or a figure's title travels with its image description.
3. **Build a small table and figure benchmark**, like the existing 19-query prose set, then compare Markdown against natural-language rows with bge-m3. The literature says the winner varies by model, so measure rather than guess.
4. **Optionally return the raw image to the answer model** (LangChain's variant 3). It needs image storage under the same authorization filter, and answers get slower on CPU.
5. **Not now:** page-image retrieval (ColPali). It needs a GPU and multi-vector storage, and the gain is unverified for our documents.

## Limitations of this research

- Most numbers come from one small paper (40 questions) and from abstracts; I did not read the full ColPali or table-format papers.
- No source compares captioning, CLIP-style embedding and page retrieval on one dataset.
- Hybrid (keyword plus vector) search for numeric table lookups is a common remedy, but I found no source in this pass, so I have left it out.

## Status (2026-10-10)

Done: long tables are split into row groups that repeat caption and header (`split_table`, 2,000 characters); table and figure chunks carry their section heading; figure chunks carry the PDF's own caption, and a figure whose model description fails is still indexed from that caption; a table benchmark exists (`evaluation/table_eval.py`).

Benchmark result (64 questions, 8 look-alike tables, `bge-m3`): Markdown Recall@1 0.906 / MRR 0.953 at 600-character groups and 0.875 / 0.927 at 300; CSV 0.891 / 0.945 and 0.906 / 0.953; natural-language rows 0.797 / 0.898 and 0.875 / 0.932. No format wins consistently, so Markdown stays. This is synthetic and small; it does not cover figures.

Not done: returning the raw image to the answer model (needs image storage under the same authorization), and page-image retrieval (ColPali).
