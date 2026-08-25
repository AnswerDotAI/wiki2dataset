import json, os
from pathlib import Path

from huggingface_hub import HfApi, sync_bucket
from huggingface_hub.utils import disable_progress_bars

from .hf import build_hf_dataset
from .metadata import NAMESPACES, acquire_sources, build_metadata, snapshot_sources, source_manifests
from .pipeline import WikiPaths


DATASET_FILES = ("README.md", "manifest.json", "materialize.py", "data/articles/*.parquet", "data/chunks/*.parquet")


def sync_checkpoint(path, checkpoint, restore=False):
    "Sync reusable source and metadata files to or from a persistent Hugging Face bucket."
    if not checkpoint: return
    source,dest = (checkpoint,str(path)) if restore else (str(path),checkpoint)
    direction = "Restoring" if restore else "Saving"
    print(f"{direction} checkpoint: {source} -> {dest}", flush=True)
    disable_progress_bars()
    result = sync_bucket(source, dest, exclude=["hf/**"])
    print(f"{direction} checkpoint complete", flush=True)
    return result

def upload_hf_dataset(path, repo_id, api=None):
    "Upload a package through a pull request, verify its files, then merge it."
    path,api = Path(path),api or HfApi()
    manifest = json.loads((path/"manifest.json").read_text())
    result = api.upload_folder(repo_id=repo_id, repo_type="dataset", folder_path=path, allow_patterns=DATASET_FILES,
        delete_patterns=("*", "**/*"), create_pr=True, commit_message=f"Update {manifest['wiki']} to {manifest['dump_date']}")
    expected = {str(file.relative_to(path)) for pattern in DATASET_FILES for file in path.glob(pattern)}
    actual = set(api.list_repo_files(repo_id, repo_type="dataset", revision=result.pr_revision))-{".gitattributes"}
    if actual != expected:
        raise RuntimeError(f"uploaded dataset differs: missing={sorted(expected-actual)}, unexpected={sorted(actual-expected)}")
    api.change_discussion_status(repo_id, result.pr_num, "open", repo_type="dataset")
    api.merge_pull_request(repo_id, result.pr_num, repo_type="dataset", comment="Verified package files before merge.")
    return dict(repo=repo_id, commit=result.oid, pull_request=result.pr_url)


def release_wiki(path="data", wiki="enwiki", workers=12, upload=True, checkpoint=None):
    "Acquire current sources, build one dataset, then publish it."
    os.environ["RAYON_NUM_THREADS"] = str(workers)
    os.environ["FBZ_NUM_THREADS"] = str(min(workers, 4))
    paths = WikiPaths(path, wiki)
    print(f"Starting {wiki}", flush=True)
    acquired = acquire_sources(wiki, paths.wiki_dir, workers=workers)
    sync_checkpoint(path, checkpoint)
    sources = [file for namespace in NAMESPACES for file in snapshot_sources(wiki, namespace, paths.wiki_dir)]
    print(f"Building {wiki} metadata", flush=True)
    build_metadata(sources, paths.metadata, wiki, page_props=paths.page_props, source_info=source_manifests(wiki, paths.wiki_dir))
    sync_checkpoint(path, checkpoint)
    print(f"Building {wiki} dataset", flush=True)
    built = build_hf_dataset(paths.xml, paths.metadata, paths.hf_dir, wiki=wiki, build_dir=paths.build_dir)
    if upload: print(f"Publishing {wiki}", flush=True)
    published = upload_hf_dataset(paths.hf_dir, f"answerdotai/{wiki}") if upload else None
    print(f"Completed {wiki}", flush=True)
    return dict(acquired=acquired, built=built, published=published)


def monthly_release(path="data", workers=12, upload=True, wikis=("simplewiki", "enwiki"), checkpoint=None):
    "Build and publish each monthly Wikipedia dataset."
    sync_checkpoint(path, checkpoint, restore=True)
    return {wiki:release_wiki(path, wiki, workers, upload, checkpoint) for wiki in wikis}
