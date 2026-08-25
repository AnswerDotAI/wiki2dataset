import os, resource, time
from pathlib import Path

import pytest, pyarrow.parquet as pq

from wiki2dataset.hf import build_hf_dataset


DATA = Path(__file__).resolve().parents[1]/"data/simplewiki"


@pytest.mark.skipif(os.environ.get("RUN_WIKI_E2E") != "1", reason="set RUN_WIKI_E2E=1 to process the local SimpleWiki dump")
@pytest.mark.timeout(30, method="thread")
def test_simplewiki_10k(tmp_path):
    started = time.perf_counter()
    result = build_hf_dataset(DATA/"simplewiki-latest-pages-articles.xml.bz2", DATA/"simplewiki-templates.sqlite", tmp_path/"hf", dump_date="20260801", limit=10_000)
    elapsed = time.perf_counter()-started
    peak_mb = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss/2**20
    print(f"simplewiki 10k: {elapsed:.2f}s, peak RSS {peak_mb:.0f} MiB, {result['chunks']:,} chunks")
    print(f"remaining operations: {result['remaining_operations'][:10]}")
    assert result["articles"] == 10_000 and result["chunks"] > 10_000
    assert pq.ParquetDataset(tmp_path/"hf/data/articles").read(columns=["article_id"]).num_rows == 10_000
    assert pq.ParquetDataset(tmp_path/"hf/data/chunks").read(columns=["article_id"]).num_rows == result["chunks"]
    assert "training and for agent/RAG systems" in (tmp_path/"hf/README.md").read_text()
