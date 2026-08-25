# wiki2dataset

The pipeline exposes metadata, article conversion, and chunking as independent Python stages and matching CLI commands. `dataset` writes both outputs in one pass, chunking each cleaned Rust `Document` before serialization; `all` refreshes template metadata first:

```bash
python -m scripts.wiki2dataset articles --xml pages.xml --articles articles.jsonl --metadata enwiki-templates.sqlite
python -m scripts.wiki2dataset chunks --articles articles.jsonl --chunks chunks.jsonl
python -m scripts.wiki2dataset dataset --xml pages.xml --articles articles.jsonl --chunks chunks.jsonl --metadata enwiki-templates.sqlite
python -m scripts.wiki2dataset all --xml pages.xml --articles articles.jsonl --chunks chunks.jsonl --page-props page_props.sql --wiki enwiki
```

Stage 1 contains one unchunked `md` record per filtered article. Standalone Stage 2 parses that persisted `md` and chunks it in batches of 128. The combined command avoids that reparse. Chunk records are compact indexes rather than duplicated text: `article_id`, `chunk_id`, UTF-8 `start_byte`/`byte_len`, heading `prefix_md`, `start_kind`, and `word_count`. Parser warnings and Wikimedia provenance remain only in article records. Stage 1 and combined builds return their row counts plus remaining semantic operations, ranked by the number of unique articles containing each operation and name.

```python
def materialize(article, chunk):
    body = article["md"].encode()
    start = chunk["start_byte"]
    return chunk["prefix_md"] + body[start:start+chunk["byte_len"]].decode()
```

The wikitext importer lives in `mdhtml` and parses directly into its Rust `Document` model. This package applies the full Wikipedia article cleanup to that `Document`, using semantic `mediawiki:*` operations plus template-role metadata, audits the operations that remain, then serializes it once to `md`. The audit therefore distinguishes operations such as `mediawiki:function` and `mediawiki:transclude` without guessing from rendered text; transclusions explicitly classified as `ok` are omitted. There is no intermediate `md`, MDHTML string, or fast5ever DOM in the article hot path. Expansion-dependent fragments that cannot be represented structurally remain inert `{=wikitext}` raw data; complex tables instead degrade to parsed visible text so their recognized content remains cleanable.

The Python API exposes the combined converter and the two dataset stages separately:

```python
from wiki2dataset import WikiConverter, build_articles, build_chunks, build_dataset

converter = WikiConverter("enwiki-templates.sqlite")
md, warnings = converter.wiki2md(wikitext)
results = converter.wiki2md_batch(wikitexts)  # parallel Rust batch
articles_and_chunks = converter.wiki2dataset_batch([(title, wikitext), ...])
```

Template/category metadata comes directly from Wikimedia Enterprise's Template and Category namespace snapshots. Authenticate once using the credentials in `WIKIMEDIA_EMAIL` and `WIKIMEDIA_PASS`; access and refresh tokens are stored in `~/.secrets` and refreshed automatically:

```bash
python -m scripts.wiki_db auth
python -m scripts.wiki_db acquire --wiki enwiki
python -m scripts.wiki_db refresh --wiki enwiki --page_props page_props.sql
python -m scripts.wiki_db roles --wiki enwiki
```

`acquire` freezes and downloads the latest completed Wikimedia article and `page_props` dumps plus matching Template and Category snapshots. Downloads are resumable and verified against their published checksums. `refresh` downloads the two Enterprise namespaces to `data/{wiki}` by default, then builds `{wiki}-templates.sqlite` there. Pass `--snapshot_dir` to use another directory or `--workers` to change concurrency. `download` only fetches the Enterprise chunks; `build` rebuilds SQLite from the completed manifests, while `roles` quickly rematerializes cleanup policy from an existing database. Duplicate snapshot records are resolved by highest revision identifier. The SQLite file contains canonical pages, inbound redirects, category edges, source licences, build metadata, source versions, and disambiguation page IDs. The compressed `page_props` dump is scanned once while building metadata rather than during every dataset run; `wiki_db page_props` refreshes only that table.

`TemplateDB` provides redirect-aware template source, categories, bounded ancestor categories, semantic roles, and wrapper information. `inspect()` also reports plausible name/category rules with their affected-template count and representative members, so role decisions include their blast radius. Role policy is declarative. It uses direct categories and explicitly configured one-parent ancestry rather than recursively traversing Wikipedia's cyclic organizational graph; templates whose transcluded body wraps a sidebar, infobox, or navigation template inherit that role:

```python
from wiki2dataset import TemplateDB

templates = TemplateDB("enwiki-templates.sqlite")
templates.template("Worldpop")
```

The default categories identify hatnotes, navigation boxes, sidebars, infoboxes, citations, maintenance, sister-project, external-link, and authority-control templates. Passing an explicit role-to-category mapping replaces these defaults.

The Stage 1 filter keeps namespace-0, non-redirect pages of at least 250 UTF-8 bytes and excludes pages carrying the `disambiguation` page property. `limit` is applied after all filters, so `limit=100` means the first 100 retained articles. Records contain dense `article_id`, page and revision ids, title, timestamp, `md`, and conversion warnings. The single cleanup policy removes non-article sections and role-classified templates, resolves known semantic and presentational templates, unwraps ordinary wiki links, and discards articles whose pre-cleanup paragraph text is under 60 characters or which become empty during cleanup.

Hugging Face packaging converts both JSONL relations to Zstandard-compressed Parquet, writes a dataset card and provenance manifest, and shards files at roughly 500 MB:

```python
from wiki2dataset.hf import build_hf_dataset

build_hf_dataset(xml, metadata, dest, wiki="simplewiki", dump_date="20260801")
```

The corresponding `hf-build` and `hf-package` CLI stages keep article and chunk JSONL in a persistent data directory, so packaging can be retried without repeating conversion. `hf` runs both stages; `--threads` limits the Rust worker pool.

With the conventional `data/{wiki}` layout, the CLI discovers the pages XML and metadata and writes to `data/hf/{wiki}-build` and `data/hf/{wiki}`:

```bash
python -m scripts.wiki2dataset hf-build --wiki enwiki --threads 12
python -m scripts.wiki2dataset hf-package --wiki enwiki --dump_date 20260801
```

`--path` changes the data root. Individual path arguments remain available as overrides.

The complete monthly release flow acquires frozen sources, builds both datasets, uploads each through a Hugging Face pull request, verifies its exact file set, and then merges it:

```bash
python -m scripts.monthly --path data --workers 12
```

Remote jobs should keep `--path` on fast local storage and can use `--checkpoint hf://buckets/<namespace>/wiki2dataset` for persistence. Reusable sources and metadata are restored at startup and checkpointed before dataset processing; generated dataset files are published rather than duplicated in the checkpoint bucket.

Set `limit=10_000` for the default-skipped local integration test. Generated datasets belong in an ignored data directory rather than this source repository.
