from pathlib import Path

from .metadata import refresh_metadata
from ._core import build_dataset as _build_dataset, build_stage1 as _build_stage1, build_stage2


class WikiPaths:
    "Conventional input, build, and package paths below one data directory."
    def __init__(self, path="data", wiki="enwiki"):
        self.path,self.wiki = Path(path),wiki
        self.wiki_dir = self.path/wiki
        self.metadata = self.wiki_dir/f"{wiki}-templates.sqlite"
        self.build_dir = self.path/"hf"/f"{wiki}-build"
        self.hf_dir = self.path/"hf"/wiki
        self.articles,self.chunks = self.build_dir/"articles.jsonl",self.build_dir/"chunks.jsonl"

    def _first(self, names, kind):
        for name in names:
            path = self.wiki_dir/name
            if path.exists(): return path
        raise FileNotFoundError(f"expected {kind} in {self.wiki_dir}")

    @property
    def xml(self):
        stem = f"{self.wiki}-latest-pages-articles"
        return self._first((f"{stem}-multistream.xml.bz2", f"{stem}-multistream.xml", f"{stem}.xml.bz2", f"{stem}.xml"), "pages XML")

    @property
    def page_props(self):
        stem = f"{self.wiki}-latest-page_props.sql"
        return self._first((f"{stem}.gz", stem), "page_props SQL")


def _summary(result, chunks=True):
    articles,nchunks,operations = result
    out = dict(articles=articles, remaining_operations=[dict(op=op, name=name, pages=pages) for op,name,pages in operations])
    if chunks: out["chunks"] = nchunks
    return out


def build_articles(xml, dest, metadata, min_bytes=250, limit=None):
    "Convert and clean eligible articles to unchunked `md` JSONL."
    return _summary(_build_stage1(xml, dest, metadata, min_bytes, limit), chunks=False)


def build_chunks(src, dest, target_words=700, batch_size=128):
    "Chunk persisted Stage 1 `md`, parsing each document once."
    return build_stage2(src, dest, target_words, batch_size)


def build_dataset(xml, articles, chunks, metadata, min_bytes=250, limit=None, target_words=700):
    "Build articles and byte-range chunk indexes together from each cleaned Rust Document."
    return _summary(_build_dataset(xml, articles, chunks, metadata, min_bytes, limit, target_words))


def materialize_chunk(article, chunk):
    "Materialize one chunk from its article and UTF-8 byte range."
    body = article["md"].encode()
    start,end = chunk["start_byte"],chunk["start_byte"]+chunk["byte_len"]
    return chunk["prefix_md"] + body[start:end].decode()


def build_all(wiki, xml, articles, chunks, page_props, snapshot_dir=None, metadata=None,
    download=False, min_bytes=250, limit=None, target_words=700):
    "Refresh template metadata, then build both dataset stages."
    db = refresh_metadata(wiki, snapshot_dir, metadata, download, page_props=page_props)
    return build_dataset(xml, articles, chunks, db, min_bytes, limit, target_words)
