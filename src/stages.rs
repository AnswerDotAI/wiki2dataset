use crate::{TemplateRoles, convert_document, title_prefix};
use anyhow::{Context, Result, ensure};
use fbz::{DecodeOptions, Reader as FbzReader};
use mdhtml::{Block, Document, Inline, Operation, TableEntry};
use quick_xml::events::Event;
use quick_xml::{Reader, escape};
use rayon::prelude::*;
use rusqlite::Connection;
use serde::{Deserialize, Serialize};
use std::collections::{HashMap, HashSet};
use std::env;
use std::fs::{File, create_dir_all};
use std::io::{BufRead, BufReader, BufWriter, Read, Write};
use std::mem;
use std::path::Path;
use std::time::{Duration, Instant};
use tempfile::NamedTempFile;

#[derive(Clone, Debug, Default)]
struct Page { page_id: u64, revision_id: Option<u64>, title: String, timestamp: Option<String>, namespace: i64, redirect: bool, wikitext: String }

#[derive(Clone, Debug, Serialize, Deserialize)]
pub struct Article {
    pub article_id: u64, pub page_id: u64, pub revision_id: Option<u64>, pub title: String, pub timestamp: Option<String>, pub md: String,
    pub warnings: Vec<String>
}

#[derive(Clone, Debug, Serialize, Deserialize)]
pub struct Chunk {
    pub article_id: u64, pub chunk_id: usize, pub start_byte: usize, pub byte_len: usize, pub prefix_md: String, pub start_kind: String, pub word_count: usize
}

#[derive(Clone, Debug, Serialize, Deserialize, PartialEq, Eq)]
pub struct OperationCount { pub op: String, pub name: String, pub pages: usize }

#[derive(Clone, Debug, Serialize, Deserialize, PartialEq, Eq)]
pub struct BuildSummary { pub articles: usize, pub chunks: usize, pub remaining_operations: Vec<OperationCount> }

// `ok` operations are intentional, readable passthroughs rather than missing cleanup.
const OK_OPERATIONS: [(&str, &str); 1] = [("mediawiki:function", "expr")];

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
enum Field { None, Title, Namespace, PageId, RevisionId, Timestamp, Wikitext }

struct Pages { reader: Reader<Box<dyn BufRead>>, buffer: Vec<u8>, page: Option<Page>, field: Field, text: String, revision: bool }

impl Pages {
    fn open(path: &Path) -> Result<Self> {
        let input: Box<dyn BufRead> = Box::new(BufReader::new(open_input(path)?));
        let mut reader = Reader::from_reader(input);
        reader.config_mut().trim_text(false);
        Ok(Self { reader, buffer: Vec::new(), page: None, field: Field::None, text: String::new(), revision: false })
    }

    fn start(&mut self, name: &[u8], empty: bool) {
        self.field = match name {
            b"page" => { self.page = Some(Page::default()); Field::None } b"revision" => { self.revision = true; Field::None }
            b"redirect" => { if let Some(page) = &mut self.page { page.redirect = true } Field::None } b"title" if !self.revision => Field::Title,
            b"ns" if !self.revision => Field::Namespace, b"id" if self.revision => Field::RevisionId, b"id" => Field::PageId,
            b"timestamp" if self.revision => Field::Timestamp, b"text" if self.revision => Field::Wikitext, _ => Field::None
        };
        self.text.clear();
        if empty { self.finish_field() }
    }

    fn finish_field(&mut self) {
        let Some(page) = &mut self.page else { self.field = Field::None; return; };
        match self.field {
            Field::Title => page.title = mem::take(&mut self.text), Field::Namespace => page.namespace = self.text.parse().unwrap_or(-1),
            Field::PageId => page.page_id = self.text.parse().unwrap_or(0), Field::RevisionId => page.revision_id = self.text.parse().ok(),
            Field::Timestamp => page.timestamp = Some(mem::take(&mut self.text)), Field::Wikitext => page.wikitext = mem::take(&mut self.text),
            Field::None => {}
        }
        self.field = Field::None;
    }
}

fn decode_options() -> DecodeOptions {
    let threads = env::var("FBZ_NUM_THREADS").ok().and_then(|value| value.parse().ok()).unwrap_or(4);
    DecodeOptions { threads, ..Default::default() }
}

fn open_input(path: &Path) -> Result<Box<dyn Read>> {
    if path.extension().is_some_and(|extension| extension == "bz2" || extension == "gz") { Ok(Box::new(FbzReader::open(path, decode_options())?)) }
    else { Ok(Box::new(File::open(path).with_context(|| format!("opening {}", path.display()))?)) }
}

impl Iterator for Pages {
    type Item = Result<Page>;

    fn next(&mut self) -> Option<Self::Item> {
        loop {
            match self.reader.read_event_into(&mut self.buffer) {
                Ok(Event::Start(event)) => {
                    let name = event.local_name().as_ref().to_vec();
                    self.start(&name, false);
                }
                Ok(Event::Empty(event)) => {
                    let name = event.local_name().as_ref().to_vec();
                    self.start(&name, true);
                }
                Ok(Event::Text(event)) if self.field != Field::None => match event.xml_content() {
                    Ok(text) => match escape::unescape(&text) { Ok(text) => self.text.push_str(&text), Err(error) => return Some(Err(error.into())) },
                    Err(error) => return Some(Err(error.into())),
                },
                Ok(Event::CData(event)) if self.field != Field::None => match event.xml_content() {
                    Ok(text) => self.text.push_str(&text), Err(error) => return Some(Err(error.into()))
                },
                Ok(Event::GeneralRef(event)) if self.field != Field::None => match event.decode() {
                    Ok(reference) => {
                        let value = if let Some(hex) = reference.strip_prefix("#x").or_else(|| reference.strip_prefix("#X")) {
                            u32::from_str_radix(hex, 16).ok().and_then(char::from_u32).map(|x| x.to_string())
                        } else if let Some(decimal) = reference.strip_prefix('#') {
                            decimal.parse().ok().and_then(char::from_u32).map(|x| x.to_string())
                        } else { escape::resolve_xml_entity(&reference).map(str::to_string) };
                        match value {
                            Some(value) => self.text.push_str(&value), None => { return Some(Err(anyhow::anyhow!("unknown XML entity &{reference};"))); }
                        }
                    }
                    Err(error) => return Some(Err(error.into())),
                },
                Ok(Event::End(event)) => {
                    let name = event.local_name();
                    if matches!(name.as_ref(), b"title" | b"ns" | b"id" | b"timestamp" | b"text") {
                        self.finish_field();
                    } else if name.as_ref() == b"revision" { self.revision = false; }
                    else if name.as_ref() == b"page" { self.buffer.clear(); return self.page.take().map(Ok); }
                }
                Ok(Event::Eof) => return None,
                Err(error) => return Some(Err(error.into())),
                _ => {}
            }
            self.buffer.clear();
        }
    }
}

fn find(haystack: &[u8], needle: &[u8], from: usize) -> Option<usize> {
    haystack[from..].windows(needle.len()).position(|window| window == needle).map(|at| at + from)
}

fn scan_disambiguation_ids(path: impl AsRef<Path>) -> Result<HashSet<u64>> {
    const NEEDLE: &[u8] = b",'disambiguation',";
    let mut input = open_input(path.as_ref())?;
    let mut found = HashSet::new();
    let mut carry = Vec::new();
    let mut chunk = vec![0; 1 << 20];
    loop {
        let read = input.read(&mut chunk)?;
        if read == 0 { break; }
        let mut data = mem::take(&mut carry);
        data.extend_from_slice(&chunk[..read]);
        let mut at = 0;
        while let Some(hit) = find(&data, NEEDLE, at) {
            let mut start = hit;
            while start > 0 && data[start - 1].is_ascii_digit() { start -= 1; }
            if start > 0 && data[start - 1] == b'(' { if let Ok(id) = std::str::from_utf8(&data[start..hit]).unwrap_or("").parse() { found.insert(id); } }
            at = hit + NEEDLE.len();
        }
        carry = data[data.len().saturating_sub(128)..].to_vec();
    }
    Ok(found)
}

pub fn cache_page_props(source: impl AsRef<Path>, metadata: impl AsRef<Path>) -> Result<usize> {
    let ids = scan_disambiguation_ids(source)?;
    let mut db = Connection::open(metadata.as_ref()).with_context(|| format!("opening metadata {}", metadata.as_ref().display()))?;
    let transaction = db.transaction()?;
    transaction.execute_batch("CREATE TABLE IF NOT EXISTS disambiguation(page_id INTEGER PRIMARY KEY); DELETE FROM disambiguation;")?;
    {
        let mut insert = transaction.prepare("INSERT INTO disambiguation VALUES (?)")?;
        for id in &ids { insert.execute([*id as i64])?; }
    }
    transaction.commit()?;
    Ok(ids.len())
}

pub fn disambiguation_ids(metadata: impl AsRef<Path>) -> Result<HashSet<u64>> {
    let db = Connection::open_with_flags(metadata.as_ref(), rusqlite::OpenFlags::SQLITE_OPEN_READ_ONLY)
        .with_context(|| format!("opening metadata {}", metadata.as_ref().display()))?;
    let mut query = db.prepare("SELECT page_id FROM disambiguation")?;
    Ok(query.query_map([], |row| row.get::<_, i64>(0))?.collect::<rusqlite::Result<Vec<_>>>()?.into_iter().map(|id| id as u64).collect())
}

fn atomic(path: &Path) -> Result<NamedTempFile> {
    let parent = path.parent().unwrap_or_else(|| Path::new("."));
    create_dir_all(parent)?;
    NamedTempFile::new_in(parent).map_err(Into::into)
}

fn process_pages(
    xml: &Path, metadata: &Path, min_bytes: usize, limit: Option<usize>, batch_size: usize, mut process: impl FnMut(Vec<Page>, usize) -> Result<usize>
) -> Result<usize> {
    ensure!(batch_size > 0, "batch_size must be positive");
    let disambiguation = disambiguation_ids(metadata)?;
    let mut count = 0;
    let mut batch = Vec::with_capacity(batch_size);
    let started = Instant::now();
    let mut reported = Instant::now();
    for page in Pages::open(xml)? {
        let page = page?;
        if page.namespace != 0 || page.redirect || disambiguation.contains(&page.page_id) || page.wikitext.len() < min_bytes { continue; }
        batch.push(page);
        if batch.len() == batch_size {
            count += process(mem::take(&mut batch), limit.map_or(usize::MAX, |limit| limit.saturating_sub(count)))?;
            if reported.elapsed() >= Duration::from_secs(30) {
                eprintln!("Processed {count} articles ({:.0}/s)", count as f64 / started.elapsed().as_secs_f64());
                reported = Instant::now();
            }
            if limit == Some(count) {
                eprintln!("Processed {count} articles in {:.1}s ({:.0}/s)", started.elapsed().as_secs_f64(), count as f64 / started.elapsed().as_secs_f64());
                return Ok(count);
            }
        }
    }
    if !batch.is_empty() && limit != Some(count) { count += process(batch, limit.map_or(usize::MAX, |limit| limit.saturating_sub(count)))?; }
    eprintln!("Processed {count} articles in {:.1}s ({:.0}/s)", started.elapsed().as_secs_f64(), count as f64 / started.elapsed().as_secs_f64());
    Ok(count)
}

fn audit_inlines(items: &[Inline], templates: &TemplateRoles, out: &mut Vec<(String, String)>) {
    for item in items {
        match item {
            Inline::Emph { children, .. } | Inline::Strong { children, .. } | Inline::Strike { children, .. } | Inline::Highlight { children, .. }
            | Inline::Link { children, .. } | Inline::Superscript { children, .. } | Inline::Subscript { children, .. } | Inline::Element { children, .. }
            | Inline::Span { children, .. } => audit_inlines(children, templates, out),
            Inline::Operation(operation) => audit_operation(operation, templates, out), _ => {}
        }
    }
}

fn audit_operation(operation: &Operation, templates: &TemplateRoles, out: &mut Vec<(String, String)>) {
    if let Some(key) = operation_key(operation, templates) { out.push(key) }
    for arg in &operation.args { audit_blocks(&arg.content, templates, out) }
}

fn literal(items: &[Inline]) -> Option<String> {
    let mut out = String::new();
    for item in items {
        match item { Inline::Text(text) => out.push_str(text), Inline::SoftBreak | Inline::HardBreak => out.push(' '), _ => return None }
    }
    Some(out.trim().to_lowercase())
}

fn operation_key(operation: &Operation, templates: &TemplateRoles) -> Option<(String, String)> {
    let op = format!("{}:{}", operation.syntax, operation.action).to_lowercase();
    let mut name = operation.name.trim().replace('_', " ").to_lowercase();
    if op == "mediawiki:function" && name == "tag"
        && let Some(tag) = operation.args.first().and_then(|arg| crate::cleanup::inline_content(&arg.content)).and_then(literal) {
            name = format!("tag:{tag}");
        }
    if OK_OPERATIONS.contains(&(op.as_str(), name.as_str())) { return None; }
    if op == "mediawiki:transclude" { name = templates.audit_template(&operation.name)?; }
    Some((op, name))
}

fn audit_blocks(blocks: &[Block], templates: &TemplateRoles, out: &mut Vec<(String, String)>) {
    for block in blocks {
        match block {
            Block::Paragraph { children, .. } | Block::Heading { children, .. } => audit_inlines(children, templates, out),
            Block::BlockQuote { children, .. } | Block::Div { children, .. } => audit_blocks(children, templates, out),
            Block::List { items, .. } => { for item in items { audit_blocks(&item.blocks, templates, out) } }
            Block::DefinitionList { items, .. } => {
                for item in items {
                    for term in &item.terms { audit_inlines(&term.inlines, templates, out) }
                    for definition in &item.definitions { audit_blocks(definition, templates, out) }
                }
            }
            Block::Table { head, rows, foot, caption, .. } => {
                audit_inlines(caption, templates, out);
                for entry in head.iter().chain(rows).chain(foot) {
                    match entry {
                        TableEntry::Row(row) => { for cell in &row.cells { audit_blocks(&cell.content, templates, out) } }
                        TableEntry::Operation(operation) => audit_operation(operation, templates, out),
                    }
                }
            }
            Block::Figure { caption, body, .. } => {
                audit_inlines(caption, templates, out);
                audit_blocks(body, templates, out);
            }
            _ => {}
        }
    }
}

fn audit(document: &Document, templates: &TemplateRoles) -> Vec<(String, String)> {
    let mut out = Vec::new();
    audit_blocks(&document.blocks, templates, &mut out);
    out.sort_unstable();
    out.dedup();
    out
}

fn summary(articles: usize, chunks: usize, page_counts: HashMap<(String, String), usize>) -> BuildSummary {
    let mut remaining_operations: Vec<_> = page_counts.into_iter().map(|((op, name), pages)| OperationCount { op, name, pages }).collect();
    remaining_operations.sort_by(|a, b| b.pages.cmp(&a.pages).then_with(|| a.op.cmp(&b.op)).then_with(|| a.name.cmp(&b.name)));
    remaining_operations.truncate(30);
    BuildSummary { articles, chunks, remaining_operations }
}

struct ConvertedPage { article: Article, operations: Vec<(String, String)> }

fn convert_page(page: Page, templates: &TemplateRoles) -> Option<ConvertedPage> {
    let converted = convert_document(&page.wikitext, templates)?;
    let operations = audit(&converted.document, templates);
    Some(ConvertedPage { article: Article { article_id: 0, page_id: page.page_id, revision_id: page.revision_id, title: page.title, timestamp: page.timestamp,
            md: mdhtml::render_md(&converted.document), warnings: converted.warnings }, operations })
}

fn convert_pages(pages: Vec<Page>, templates: &TemplateRoles) -> Vec<ConvertedPage> {
    pages.into_par_iter().filter_map(|page| convert_page(page, templates)).collect()
}

fn write_articles(out: &mut impl Write, articles: &[Article]) -> Result<()> {
    for article in articles {
        serde_json::to_writer(&mut *out, article)?;
        out.write_all(b"\n")?;
    }
    Ok(())
}

pub fn stage1(xml: impl AsRef<Path>, dest: impl AsRef<Path>, metadata: impl AsRef<Path>, min_bytes: usize, limit: Option<usize>) -> Result<BuildSummary> {
    let templates = TemplateRoles::open(&metadata)?;
    let mut tmp = atomic(dest.as_ref())?;
    let mut page_counts = HashMap::new();
    let count = {
        let mut out = BufWriter::new(tmp.as_file_mut());
        let mut next_article_id = 0;
        let count = process_pages(xml.as_ref(), metadata.as_ref(), min_bytes, limit, 256, |pages, remaining| {
            let mut converted = convert_pages(pages, &templates);
            converted.truncate(remaining);
            for item in &mut converted {
                item.article.article_id = next_article_id;
                next_article_id += 1;
                for key in item.operations.drain(..) { *page_counts.entry(key).or_insert(0) += 1; }
            }
            let articles: Vec<_> = converted.into_iter().map(|item| item.article).collect();
            write_articles(&mut out, &articles)?;
            Ok(articles.len())
        })?;
        out.flush()?;
        count
    };
    tmp.persist(dest.as_ref()).map_err(|error| error.error)?;
    Ok(summary(count, 0, page_counts))
}

fn chunks(article: &Article, target_words: usize) -> Vec<Chunk> {
    let document = mdhtml::parse(&article.md, &mdhtml::Options::default());
    let (source, chunks) = mdhtml::document_chunk_ranges_structural(&document, target_words);
    assert_eq!(source, article.md, "Stage 1 md must be canonical");
    let title = title_prefix(&article.title);
    chunks.into_iter().enumerate().map(|(chunk_id, chunk)| {
        let prefix_md = format!("{title}{}", chunk.prefix);
        let word_count = prefix_md.split_whitespace().count() + source[chunk.range.clone()].split_whitespace().count();
        Chunk { article_id: article.article_id, chunk_id, start_byte: chunk.range.start, byte_len: chunk.range.len(), prefix_md,
            start_kind: if chunk_id == 0 { "h1".into() } else { chunk.start.as_str() }, word_count }
    }).collect()
}

struct Prepared { article: Article, chunks: Vec<Chunk>, operations: Vec<(String, String)> }

fn prepare(page: Page, templates: &TemplateRoles, target_words: usize) -> Option<Prepared> {
    let converted = convert_document(&page.wikitext, templates)?;
    let operations = audit(&converted.document, templates);
    let (source, chunks) = mdhtml::document_chunk_ranges_structural(&converted.document, target_words);
    let title = title_prefix(&page.title);
    let article = Article { article_id: 0, page_id: page.page_id, revision_id: page.revision_id, title: page.title, timestamp: page.timestamp, md: source,
        warnings: converted.warnings };
    let chunks = chunks.into_iter().enumerate().map(|(chunk_id, chunk)| {
        let prefix_md = format!("{title}{}", chunk.prefix);
        let word_count = prefix_md.split_whitespace().count() + article.md[chunk.range.clone()].split_whitespace().count();
        Chunk { article_id: 0, chunk_id, start_byte: chunk.range.start, byte_len: chunk.range.len(), prefix_md,
            start_kind: if chunk_id == 0 { "h1".into() } else { chunk.start.as_str() }, word_count }
    }).collect();
    Some(Prepared { article, chunks, operations })
}

pub fn stage2(src: impl AsRef<Path>, dest: impl AsRef<Path>, target_words: usize, batch_size: usize) -> Result<usize> {
    let input = BufReader::new(File::open(src.as_ref())?);
    let mut tmp = atomic(dest.as_ref())?;
    let mut count = 0;
    {
        let mut out = BufWriter::new(tmp.as_file_mut());
        let mut batch = Vec::with_capacity(batch_size);
        for line in input.lines() {
            batch.push(serde_json::from_str::<Article>(&line?)?);
            if batch.len() == batch_size { count += write_chunks(&mut out, mem::take(&mut batch), target_words)?; }
        }
        if !batch.is_empty() { count += write_chunks(&mut out, batch, target_words)?; }
        out.flush()?;
    }
    tmp.persist(dest.as_ref()).map_err(|error| error.error)?;
    Ok(count)
}

fn write_chunks(out: &mut impl Write, articles: Vec<Article>, target_words: usize) -> Result<usize> {
    let chunks: Vec<_> = articles.par_iter().flat_map_iter(|article| chunks(article, target_words)).collect();
    for chunk in &chunks {
        serde_json::to_writer(&mut *out, chunk)?;
        out.write_all(b"\n")?;
    }
    Ok(chunks.len())
}

#[allow(clippy::too_many_arguments)]
pub fn dataset(
    xml: impl AsRef<Path>, articles: impl AsRef<Path>, chunks: impl AsRef<Path>, metadata: impl AsRef<Path>, min_bytes: usize, limit: Option<usize>,
    target_words: usize) -> Result<BuildSummary> {
    ensure!(target_words > 0, "target_words must be positive");
    let templates = TemplateRoles::open(&metadata)?;
    let mut article_tmp = atomic(articles.as_ref())?;
    let mut chunk_tmp = atomic(chunks.as_ref())?;
    let mut chunk_count = 0;
    let mut page_counts = HashMap::new();
    let mut next_article_id = 0;
    let article_count = {
        let mut article_out = BufWriter::new(article_tmp.as_file_mut());
        let mut chunk_out = BufWriter::new(chunk_tmp.as_file_mut());
        let count = process_pages(xml.as_ref(), metadata.as_ref(), min_bytes, limit, 256, |pages, remaining| {
            let mut prepared: Vec<_> = pages.into_par_iter().filter_map(|page| prepare(page, &templates, target_words)).collect();
            prepared.truncate(remaining);
            for item in &mut prepared {
                item.article.article_id = next_article_id;
                for chunk in &mut item.chunks { chunk.article_id = next_article_id; }
                next_article_id += 1;
                for key in item.operations.drain(..) { *page_counts.entry(key).or_insert(0) += 1; }
                serde_json::to_writer(&mut article_out, &item.article)?;
                article_out.write_all(b"\n")?;
                for chunk in &item.chunks {
                    serde_json::to_writer(&mut chunk_out, chunk)?;
                    chunk_out.write_all(b"\n")?;
                    chunk_count += 1;
                }
            }
            Ok(prepared.len())
        })?;
        article_out.flush()?;
        chunk_out.flush()?;
        count
    };
    article_tmp.persist(articles.as_ref()).map_err(|error| error.error)?;
    chunk_tmp.persist(chunks.as_ref()).map_err(|error| error.error)?;
    Ok(summary(article_count, chunk_count, page_counts))
}
