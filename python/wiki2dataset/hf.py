import hashlib, json, os, sqlite3, subprocess, tomllib
from datetime import datetime, timezone
from pathlib import Path

import pyarrow as pa, pyarrow.parquet as pq

from .pipeline import build_dataset


U64,U32,STR = pa.uint64(),pa.uint32(),pa.string()
ARTICLE_SCHEMA = pa.schema(dict(article_id=U64, page_id=U64, revision_id=U64, title=STR, timestamp=STR, md=STR, warnings=pa.list_(STR)))
CHUNK_SCHEMA = pa.schema(dict(article_id=U64, chunk_id=U32, start_byte=U64, byte_len=U64, prefix_md=STR, start_kind=STR, word_count=U32))
SOURCE_REPO = "https://github.com/AnswerDotAI/wiki2dataset"


def _records(path, batch_rows=10_000):
    batch = []
    with Path(path).open(encoding="utf-8") as f:
        for line in f:
            batch.append(json.loads(line))
            if len(batch) == batch_rows:
                yield batch
                batch = []
    if batch: yield batch


def jsonl2parquet(src, dest, schema, shard_bytes=500_000_000, batch_rows=10_000):
    "Convert JSONL to compressed, size-limited Parquet shards."
    dest = Path(dest)
    dest.mkdir(parents=True, exist_ok=True)
    for path in dest.glob("train-*.parquet"): path.unlink()
    writer = path = None
    paths = []
    try:
        for records in _records(src, batch_rows):
            if writer is None:
                path = dest/f"train-{len(paths):05d}.parquet"
                writer = pq.ParquetWriter(path, schema, compression="zstd")
                paths.append(path)
            writer.write_table(pa.Table.from_pylist(records, schema=schema))
            if path.stat().st_size >= shard_bytes:
                writer.close()
                writer = None
    finally:
        if writer is not None: writer.close()
    return paths


def materializer_source():
    return '''"Reconstruct chunk text from the two dataset configurations."

def materialize(article, chunk):
    body = article["md"].encode()
    start = chunk["start_byte"]
    return chunk["prefix_md"] + body[start:start+chunk["byte_len"]].decode()
'''


def dataset_card(wiki, dump_date, stats):
    name = {"simplewiki":"Simple English Wikipedia", "enwiki":"English Wikipedia"}.get(wiki, wiki)
    subdomain = wiki.removesuffix("wiki")
    repo = f"answerdotai/{wiki}"
    return f'''---
license: cc-by-sa-4.0
language:
- en
configs:
- config_name: articles
  default: true
  data_files: data/articles/*.parquet
- config_name: chunks
  data_files: data/chunks/*.parquet
---

# {name} as clean md

This dataset is a cleaned, structurally faithful approximation of the {name} article corpus in Answer.AI's canonical `md` dialect. It was produced from the Wikimedia dump dated **{dump_date}** by [Answer.AI's wiki2dataset pipeline]({SOURCE_REPO}).

It is designed for language-model training and for agent/RAG systems. The `articles` configuration provides complete documents for continued pretraining, corpus analysis, rechunking, and task-specific dataset creation. The `chunks` configuration provides section-aware retrieval units without duplicating article text: every chunk is a UTF-8 byte range into an article plus a small Markdown heading prefix. This keeps the corpus compact while preserving useful article and section breadcrumbs.

## Contents

- **articles** — {stats['articles']:,} cleaned articles with dense `article_id`, Wikimedia page and revision IDs, title, revision timestamp, complete `md`, and conversion warnings.
- **chunks** — {stats['chunks']:,} chunk indexes targeting about {stats['target_words']} words, with `article_id`, per-article `chunk_id`, UTF-8 `start_byte`/`byte_len`, `prefix_md`, `start_kind`, and `word_count`.

The article row number equals `article_id`, and chunk rows are ordered by `article_id` then `chunk_id`. UTF-8 byte offsets—not Python character offsets—are used so ranges are stable across languages and tools.

```python
from datasets import load_dataset

articles = load_dataset("{repo}", "articles", split="train")
chunks = load_dataset("{repo}", "chunks", split="train")

def materialize(article, chunk):
    body = article["md"].encode()
    start = chunk["start_byte"]
    return chunk["prefix_md"] + body[start:start + chunk["byte_len"]].decode()

chunk = chunks[100]
text = materialize(articles[chunk["article_id"]], chunk)
```

`materialize.py` contains the same dependency-free reconstruction helper. Retrieval systems can store chunk rows in their vector index, fetch the corresponding article by dense row ID, and reconstruct only selected chunks. Training pipelines can stream complete article rows or materialize chunk rows on demand.

## Processing

Redirects, disambiguation pages, and very short pages are excluded. References, footnotes, navigation boxes, sidebars, maintenance notices, citations, and other non-article material are removed. Links, lists, tables, math, and common templates are converted to `md`; other templates are retained in a readable form where possible.

Chunks target about {stats['target_words']} words and prefer section boundaries. Each chunk stores byte offsets into its article plus the headings needed to identify its section.

`manifest.json` records the dump date, settings, checksums, and row counts.

## Uses and limitations

Useful applications include language-model pretraining or adaptation, embedding and retrieval corpora, grounded agent knowledge stores, search, summarization, and experiments with alternative chunking strategies. `page_id`, `revision_id`, and `timestamp` support provenance, exact revision links, auditing, and future incremental updates.

This is intentionally a fast, useful approximation rather than a browser-perfect rendering. MediaWiki templates requiring server-side expansion may be simplified, removed, or retained as raw syntax. Images and Wikipedia-only presentation/navigation are generally omitted. Consumers requiring exact current facts should follow the stored revision provenance and compare against Wikimedia.

## License and attribution

Wikipedia text is provided by its contributors under the [Creative Commons Attribution-ShareAlike 4.0 License](https://creativecommons.org/licenses/by-sa/4.0/) and may also be available under the GFDL. This transformed dataset is distributed under CC BY-SA 4.0. Attribute **{name} contributors**, link to the relevant article or revision, and indicate that the text was transformed by Answer.AI's wiki2dataset pipeline. For a row, an exact revision URL is:

```python
url = f"https://{subdomain}.wikipedia.org/w/index.php?oldid={{article['revision_id']}}"
```

See [Wikimedia's reuse guidance](https://foundation.wikimedia.org/wiki/Policy:Terms_of_Use) for full terms.
'''


def _sha256(path):
    with Path(path).open("rb") as f: return hashlib.file_digest(f, "sha256").hexdigest()


def _file_info(path): return dict(path=Path(path).name, sha256=_sha256(path))


def _source_versions(metadata, dump_date):
    sources = {key:json.loads(metadata[key]) for key in ("wikimedia_dump", "enterprise_snapshots") if key in metadata}
    frozen = sources.get("wikimedia_dump", {}).get("date")
    if frozen and dump_date and frozen != dump_date: raise ValueError(f"dump date {dump_date} differs from frozen source {frozen}")
    dump_date = frozen or dump_date
    if not dump_date: raise ValueError("dump date is absent from both metadata and arguments")
    return dump_date,sources


def _git(path):
    commit = subprocess.run(["git", "rev-parse", "HEAD"], cwd=path, text=True, capture_output=True, check=True).stdout.strip()
    dirty = bool(subprocess.run(["git", "status", "--porcelain"], cwd=path, text=True, capture_output=True, check=True).stdout)
    return {"commit": commit, "dirty": dirty}


def _tools():
    root = Path(os.environ.get("WIKI2DATASET_ROOT") or Path(__file__).resolve().parents[2])
    commit = os.environ.get("WIKI2DATASET_COMMIT")
    wiki2dataset = dict(commit=commit, dirty=False) if commit else _git(root)
    dependency = tomllib.loads((root/"Cargo.toml").read_text())["dependencies"]["mdhtml"]
    return dict(wiki2dataset=wiki2dataset, mdhtml={key:dependency[key] for key in ("git", "tag", "version") if key in dependency})


def build_hf_sources(xml, metadata, dest, target_words=700, min_bytes=250, limit=None):
    "Build persistent article and chunk JSONL plus the information needed to package them."
    dest = Path(dest)
    dest.mkdir(parents=True, exist_ok=True)
    articles,chunks = dest/"articles.jsonl",dest/"chunks.jsonl"
    print("Converting and chunking articles", flush=True)
    built = build_dataset(xml, articles, chunks, metadata, min_bytes=min_bytes, limit=limit, target_words=target_words)
    state = dict(xml=str(Path(xml).resolve()), metadata=str(Path(metadata).resolve()),
        parameters=dict(min_bytes=min_bytes, target_words=target_words, limit=limit), result=built)
    (dest/"build.json").write_text(json.dumps(state, indent=2)+"\n")
    return built | {"path":str(dest)}


def package_hf_dataset(src, dest, wiki="simplewiki", dump_date=None):
    "Package a completed persistent JSONL build for Hugging Face."
    src,dest = Path(src),Path(dest)
    state = json.loads((src/"build.json").read_text())
    xml,metadata = Path(state["xml"]),Path(state["metadata"])
    articles,chunks = src/"articles.jsonl",src/"chunks.jsonl"
    built,parameters = state["result"],state["parameters"]
    stats = built
    with sqlite3.connect(metadata) as db: source = dict(db.execute("SELECT key,value FROM metadata"))
    dump_date,sources = _source_versions(source, dump_date)
    dest.mkdir(parents=True, exist_ok=True)
    print("Writing article Parquet", flush=True)
    article_paths = jsonl2parquet(articles, dest/"data/articles", ARTICLE_SCHEMA)
    print("Writing chunk Parquet", flush=True)
    chunk_paths = jsonl2parquet(chunks, dest/"data/chunks", CHUNK_SCHEMA)
    target_words = parameters["target_words"]
    stats["target_words"] = target_words
    (dest/"materialize.py").write_text(materializer_source())
    (dest/"README.md").write_text(dataset_card(wiki, dump_date, stats))
    outputs = article_paths+chunk_paths
    counts = {key:stats[key] for key in ("articles", "chunks")}
    page_props = dict(path=source["page_props_path"], sha256=source["page_props_sha256"])
    inputs = dict(xml=_file_info(xml), page_props=page_props, metadata=_file_info(metadata))
    tools = _tools()
    print("Writing dataset metadata", flush=True)
    files = {str(path.relative_to(dest)):dict(bytes=path.stat().st_size, sha256=_sha256(path)) for path in outputs}
    manifest = dict(schema_version=4, wiki=wiki, dump_date=dump_date, generated_at=datetime.now(timezone.utc).isoformat(),
        parameters=parameters, counts=counts, remaining_operations=stats["remaining_operations"],
        sources=sources, inputs=inputs, tools=tools, files=files)
    (dest/"manifest.json").write_text(json.dumps(manifest, indent=2)+"\n")
    return stats | {"path": str(dest), "files": len(outputs)}


def build_hf_dataset(xml, metadata, dest, wiki="simplewiki", dump_date=None, target_words=700, min_bytes=250, limit=None, build_dir=None):
    "Build persistent JSONL, then package it as a two-configuration Hugging Face dataset."
    dest = Path(dest)
    build_dir = Path(build_dir) if build_dir is not None else dest.with_name(dest.name+"-build")
    build_hf_sources(xml, metadata, build_dir, target_words, min_bytes, limit)
    return package_hf_dataset(build_dir, dest, wiki, dump_date)
