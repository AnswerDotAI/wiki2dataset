import json, sqlite3
from pathlib import Path

import pytest

from wiki2dataset import WikiConverter, WikiPaths, build_dataset, materialize_chunk


XML = """<mediawiki xmlns="http://www.mediawiki.org/xml/export-0.11/"><page><title>Example</title><ns>0</ns><id>1</id>
<revision><id>2</id><timestamp>2026-01-01T00:00:00Z</timestamp><text>{text}</text></revision></page></mediawiki>"""
DATA = Path(__file__).parent/"data"


def _roles(path, rows=()):
    db = sqlite3.connect(path)
    db.executescript("""CREATE TABLE redirect (namespace INTEGER, title TEXT, to_namespace INTEGER, to_title TEXT);
        CREATE TABLE template_role (title TEXT, role TEXT);
        CREATE TABLE disambiguation (page_id INTEGER PRIMARY KEY);""")
    db.executemany("INSERT INTO template_role VALUES (?,?)", rows)
    db.commit()
    db.close()


def test_full_article_and_chunk_stages(tmp_path):
    xml,roles = tmp_path/"pages.xml",tmp_path/"roles.sqlite"
    xml.write_text(XML.format(text=("Article prose with enough content to survive cleanup. "*20)))
    _roles(roles)
    articles,chunks = tmp_path/"articles.jsonl",tmp_path/"chunks.jsonl"
    assert build_dataset(xml, articles, chunks, roles) == dict(articles=1, chunks=1, remaining_operations=[])
    article,chunk = json.loads(articles.read_text()),json.loads(chunks.read_text())
    assert article["article_id"] == chunk["article_id"] == 0 and article["page_id"] == 1 and article["warnings"] == []
    assert chunk["chunk_id"] == 0 and chunk["start_kind"] == "h1" and chunk["word_count"] > 100
    assert materialize_chunk(article, chunk).startswith("# Example") and "md" not in chunk


def test_wiki_paths_discovers_inputs_and_conventional_outputs(tmp_path):
    root,wiki = tmp_path/"data",tmp_path/"data/enwiki"
    wiki.mkdir(parents=True)
    xml,props = wiki/"enwiki-latest-pages-articles-multistream.xml.bz2",wiki/"enwiki-latest-page_props.sql.gz"
    xml.touch()
    props.touch()
    paths = WikiPaths(root, "enwiki")
    assert paths.xml == xml and paths.page_props == props
    assert paths.metadata == wiki/"enwiki-templates.sqlite"
    assert paths.articles == root/"hf/enwiki-build/articles.jsonl" and paths.hf_dir == root/"hf/enwiki"
    (wiki/"enwiki-latest-pages-articles-multistream.xml").touch()
    assert paths.xml == xml


@pytest.mark.timeout(1, method="thread")
def test_b_article_conversion_is_bounded(tmp_path):
    roles = tmp_path/"roles.sqlite"
    _roles(roles)
    md,warnings = WikiConverter(roles).wiki2md((DATA/"B.wiki").read_text())
    assert md is not None and "**B** is the 2nd" in md
    assert warnings == []


def test_operations_are_audited_before_serialization(tmp_path):
    xml,roles = tmp_path/"pages.xml",tmp_path/"roles.sqlite"
    source = ("Article prose with enough content to survive cleanup. "*20)+r" {{NASA|value}} {{Unknown|value}} {{Unknown|again}} {{#expr:1+1}} {{#tag:ref|Hidden footnote}} {{#tag:math|\mu}} {{#tag:timeline|Timeline data}} {{#tag:timeline|More data}}"
    xml.write_text(XML.format(text=source))
    _roles(roles, [("NASA", "ok")])
    articles,chunks = tmp_path/"articles.jsonl",tmp_path/"chunks.jsonl"
    result = build_dataset(xml, articles, chunks, roles)
    assert result["remaining_operations"] == [
        dict(op="mediawiki:function", name="tag:timeline", pages=1),
        dict(op="mediawiki:transclude", name="unknown", pages=1)]
    md = json.loads(articles.read_text())["md"]
    assert "Hidden footnote" not in md and r"\(\mu\)" in md
