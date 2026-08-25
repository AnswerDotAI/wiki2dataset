import json, os
from pathlib import Path

from fastcore.script import call_parse

from wiki2dataset import WikiPaths, build_all, build_articles, build_chunks, build_dataset


@call_parse
def main(
    command:str, # articles, chunks, dataset, all, hf-build, hf-package, or hf
    path:Path=Path("data"), # Root data directory
    xml:Path=None, # MediaWiki pages-articles XML
    articles:Path=None, # Article JSONL
    chunks:Path=None, # Chunk JSONL
    page_props:Path=None, # page_props SQL dump; needed only by all
    metadata:Path=None, # Wiki metadata SQLite
    wiki:str="enwiki", # Wikimedia database name
    snapshot_dir:Path=None, # Template snapshot directory; defaults to data/{wiki}
    download:bool=False, # Refresh namespace snapshots
    min_bytes:int=250, # Minimum source bytes
    limit:int=None, # Maximum retained articles
    target_words:int=700, # Target chunk words
    batch_size:int=128, # Stage 2 parsing batch size
    dest:Path=None, # Packaged Hugging Face dataset directory
    build_dir:Path=None, # Persistent Hugging Face JSONL build directory
    dump_date:str=None, # Wikimedia dump date, YYYYMMDD
    threads:int=None, # Rayon worker threads
):
    "Build Wikipedia article and chunk datasets."
    if threads is not None: os.environ["RAYON_NUM_THREADS"] = str(threads)
    paths = WikiPaths(path, wiki)
    metadata = metadata or paths.metadata
    snapshot_dir = snapshot_dir or paths.wiki_dir
    dest = dest or paths.hf_dir
    build_dir = build_dir or paths.build_dir
    articles = articles or build_dir/"articles.jsonl"
    chunks = chunks or build_dir/"chunks.jsonl"
    if command in ("articles", "dataset", "all", "hf-build", "hf"): xml = xml or paths.xml
    if command == "all": page_props = page_props or paths.page_props
    if command == "articles": result = build_articles(xml, articles, metadata, min_bytes, limit)
    elif command == "chunks": result = build_chunks(articles, chunks, target_words, batch_size)
    elif command == "dataset": result = build_dataset(xml, articles, chunks, metadata, min_bytes, limit, target_words)
    elif command == "all": result = build_all(wiki, xml, articles, chunks, page_props, snapshot_dir, metadata, download, min_bytes, limit, target_words)
    elif command in ("hf-build", "hf-package", "hf"):
        from wiki2dataset.hf import build_hf_dataset, build_hf_sources, package_hf_dataset
        if command == "hf-build": result = build_hf_sources(xml, metadata, build_dir, target_words, min_bytes, limit)
        elif command == "hf-package": result = package_hf_dataset(build_dir, dest, wiki, dump_date)
        else: result = build_hf_dataset(xml, metadata, dest, wiki, dump_date, target_words, min_bytes, limit, build_dir)
    else: raise ValueError(f"unknown command: {command}")
    print(json.dumps(result, indent=2, default=str) if isinstance(result, dict) else result)
