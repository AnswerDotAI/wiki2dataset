import json
from pathlib import Path

from fastcore.script import call_parse

from wiki2dataset.metadata import SECRETS, access_token, acquire_sources, build_metadata, cache_page_props, download_snapshots
from wiki2dataset.metadata import refresh_metadata, refresh_roles, snapshot_sources, source_manifests, wiki_dir


@call_parse
def main(
    action:str, # auth, acquire, download, build, roles, page_props, or refresh
    wiki:str="enwiki", # Wikimedia database name
    snapshot_dir:Path=None, # Snapshot and SQLite directory; defaults to data/{wiki}
    metadata:Path=None, # Output wiki metadata SQLite
    page_props:Path=None, # page_props SQL dump
    workers:int=16, # Concurrent download workers
    limit:int=None, # Maximum records per namespace
):
    "Download and build Wikimedia template metadata."
    snapshot_dir = wiki_dir(wiki, snapshot_dir)
    dest = metadata or snapshot_dir/f"{wiki}-templates.sqlite"
    if action == "auth": result = f"Stored tokens in {SECRETS}" if access_token() else None
    elif action == "acquire": result = acquire_sources(wiki, snapshot_dir, workers=workers)
    elif action == "download": result = download_snapshots(wiki, snapshot_dir, workers=workers)
    elif action == "build": result = build_metadata([p for ns in (10,14) for p in snapshot_sources(wiki, ns, snapshot_dir)], dest, wiki, limit,
        page_props, source_manifests(wiki, snapshot_dir))
    elif action == "roles": result = refresh_roles(dest)
    elif action == "page_props": result = cache_page_props(page_props, dest)
    elif action == "refresh": result = refresh_metadata(wiki, snapshot_dir, dest, True, workers=workers, limit=limit, page_props=page_props)
    else: raise ValueError(f"unknown action: {action}")
    print(json.dumps(result, indent=2, default=str) if isinstance(result, (dict,list)) else result)
