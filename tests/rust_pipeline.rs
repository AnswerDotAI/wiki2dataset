use flate2::{Compression, write::GzEncoder};
use rusqlite::Connection;
use std::fs;
use std::io::Write;
use tempfile::tempdir;
use wiki2dataset::{Article, BuildSummary, Chunk, cache_page_props, dataset, disambiguation_ids, stage1};

const XML: &str = r#"<mediawiki xmlns="http://www.mediawiki.org/xml/export-0.11/">
<page><title>Keep</title><ns>0</ns><id>1</id><revision><id>11</id><timestamp>2026-01-01T00:00:00Z</timestamp><text>'''Kept''' [[page]] '''Kept''' [[page]] '''Kept''' [[page]] '''Kept''' [[page]] '''Kept''' [[page]] '''Kept''' [[page]] '''Kept''' [[page]] '''Kept''' [[page]] '''Kept''' [[page]] '''Kept''' [[page]]</text></revision></page>
<page><title>Redirect</title><ns>0</ns><id>2</id><redirect title="Keep"/><revision><id>12</id><timestamp>2026-01-01T00:00:00Z</timestamp><text>long redirect text that should never be retained in the output article collection</text></revision></page>
<page><title>Disambiguation</title><ns>0</ns><id>3</id><revision><id>13</id><timestamp>2026-01-01T00:00:00Z</timestamp><text>long disambiguation text that should never be retained in the output article collection</text></revision></page>
</mediawiki>"#;

fn metadata(path: &std::path::Path) {
    let db = Connection::open(path).unwrap();
    db.execute_batch("CREATE TABLE redirect(namespace INTEGER,title TEXT,to_namespace INTEGER,to_title TEXT); CREATE TABLE template_role(title TEXT,role TEXT); CREATE TABLE disambiguation(page_id INTEGER PRIMARY KEY);").unwrap();
    db.execute("INSERT INTO disambiguation VALUES (3)", []).unwrap();
}

#[test]
fn full_dataset_stages() {
    let dir = tempdir().unwrap();
    let xml = dir.path().join("pages.xml");
    let roles = dir.path().join("roles.sqlite");
    let articles = dir.path().join("articles.jsonl");
    let chunks = dir.path().join("chunks.jsonl");
    fs::write(&xml, XML).unwrap();
    metadata(&roles);
    assert_eq!(dataset(&xml, &articles, &chunks, &roles, 50, Some(1), 700).unwrap(), BuildSummary { articles: 1, chunks: 1, remaining_operations: Vec::new() });
    let article: Article = serde_json::from_str(fs::read_to_string(articles).unwrap().trim()).unwrap();
    let chunk: Chunk = serde_json::from_str(fs::read_to_string(chunks).unwrap().trim()).unwrap();
    assert_eq!((article.article_id, article.page_id, article.revision_id, article.title.as_str()), (0, 1, Some(11), "Keep"));
    assert!(article.md.contains("**Kept**") && !article.md.contains("[page]"));
    assert_eq!((chunk.article_id, chunk.chunk_id, chunk.start_kind.as_str()), (0, 0, "h1"));
    let body = &article.md.as_bytes()[chunk.start_byte..chunk.start_byte + chunk.byte_len];
    assert!(format!("{}{}", chunk.prefix_md, std::str::from_utf8(body).unwrap()).starts_with("# Keep"));
}

#[test]
fn filtering_precedes_limit() {
    let dir = tempdir().unwrap();
    let xml = dir.path().join("pages.xml");
    let roles = dir.path().join("roles.sqlite");
    let articles = dir.path().join("articles.jsonl");
    fs::write(&xml, XML).unwrap();
    metadata(&roles);
    assert_eq!(stage1(xml, articles, roles, 50, Some(1)).unwrap().articles, 1);
}

#[test]
fn compressed_page_props_are_cached_in_metadata() {
    let dir = tempdir().unwrap();
    let props = dir.path().join("page_props.sql.gz");
    let metadata = dir.path().join("metadata.sqlite");
    let mut data = vec![b'x'; (1 << 20) - 5];
    data.extend_from_slice(b"(3,'disambiguation','',NULL),(99,'disambiguation','',NULL)");
    let mut gzip = GzEncoder::new(Vec::new(), Compression::fast());
    gzip.write_all(&data).unwrap();
    fs::write(&props, gzip.finish().unwrap()).unwrap();
    Connection::open(&metadata).unwrap();
    assert_eq!(cache_page_props(props, &metadata).unwrap(), 2);
    assert_eq!(disambiguation_ids(metadata).unwrap(), [3, 99].into_iter().collect());
}
