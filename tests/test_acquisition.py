import json

import pytest

from wiki2dataset.hf import _source_versions
from wiki2dataset import metadata as wm
from wiki2dataset.metadata import _dump_plan


def test_dump_plan_freezes_completed_dated_files():
    date,wiki = "20260801","enwiki"
    pages,props = f"{wiki}-{date}-pages-articles-multistream.xml.bz2",f"{wiki}-{date}-page_props.sql.gz"
    status = dict(version=date, jobs=dict(articles=dict(status="done", files={pages:dict(size=25_000)}),
        tables=dict(status="done", files={props:dict(size=500)}), unfinished=dict(status="waiting", files={f"{wiki}-{date}-other":dict(size=1)})))
    checksums = f"pageshash  {pages}\npropshash  {props}\n"
    plan = _dump_plan(wiki, status, checksums)
    assert plan["date"] == date
    assert plan["files"]["pages"] == dict(name=pages, local=pages.replace(date, "latest"), bytes=25_000, sha1="pageshash",
        url=f"https://dumps.wikimedia.org/{wiki}/{date}/{pages}")
    assert plan["files"]["page_props"]["sha1"] == "propshash"
    with pytest.raises(ValueError, match="page_props"): _dump_plan(wiki, status, f"pageshash  {pages}\n")


def test_wikimedia_plan_uses_latest_completed_dated_dump(monkeypatch):
    wiki = "enwiki"
    listing = '<a href="20260820/">new</a><a href="20260801/">complete</a>'
    def get(url, json_result=False):
        assert "/latest/" not in url
        if url.endswith(f"/{wiki}/"): return listing
        date = "20260820" if "20260820" in url else "20260801"
        pages,props = f"{wiki}-{date}-pages-articles-multistream.xml.bz2",f"{wiki}-{date}-page_props.sql.gz"
        if url.endswith("dumpstatus.json"):
            jobs = {"articles":dict(status="done", files={pages:dict(size=10)})}
            if date == "20260801": jobs["tables"] = dict(status="done", files={props:dict(size=5)})
            return dict(version=date, jobs=jobs)
        return f"pages  {pages}\nprops  {props}\n"
    monkeypatch.setattr(wm, "_get", get)
    assert wm.wikimedia_plan(wiki)["date"] == "20260801"


def test_hf_provenance_uses_frozen_source_versions():
    dump = dict(date="20260801", files={"pages": {"sha1":"abc"}})
    enterprise = {"10": {"version":"v10"}, "14": {"version":"v14"}}
    metadata = {"wikimedia_dump":json.dumps(dump), "enterprise_snapshots":json.dumps(enterprise)}
    assert _source_versions(metadata, None) == ("20260801", dict(wikimedia_dump=dump, enterprise_snapshots=enterprise))
    with pytest.raises(ValueError, match="dump date"): _source_versions(metadata, "20260701")
