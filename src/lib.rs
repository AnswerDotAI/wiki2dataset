use mdhtml::Document;
use std::mem;
mod cleanup;
#[cfg(feature = "python")]
mod python;
mod roles;
mod stages;

pub use cleanup::{TemplateAction, TemplateInfo, TemplateLookup, clean};
pub use roles::TemplateRoles;
pub use stages::{Article, BuildSummary, Chunk, OperationCount, cache_page_props, dataset, disambiguation_ids, stage1, stage2};

#[derive(Clone, Debug, PartialEq)]
pub struct Converted { pub md: String, pub warnings: Vec<String> }

pub(crate) struct ConvertedDocument { pub document: Document, pub warnings: Vec<String> }

pub(crate) fn convert_document(source: &str, templates: &dyn TemplateLookup) -> Option<ConvertedDocument> {
    let mut document = mdhtml::parse_wikitext(source);
    let warnings = mem::take(&mut document.diagnostics).into_iter().map(|diagnostic| diagnostic.to_string()).collect();
    clean(&mut document, templates).then_some(ConvertedDocument { document, warnings })
}

#[cfg(feature = "python")]
pub(crate) fn convert_article(source: &str, templates: &dyn TemplateLookup, target_words: usize) -> Option<(Converted, Vec<mdhtml::MdChunkRange>)> {
    let converted = convert_document(source, templates)?;
    let (md, chunks) = mdhtml::document_chunk_ranges_structural(&converted.document, target_words);
    let result = Converted { md, warnings: converted.warnings };
    Some((result, chunks))
}

pub(crate) fn title_prefix(title: &str) -> String {
    let document = Document {
        blocks: vec![mdhtml::Block::Heading { level: 1, attrs: mdhtml::Attr::default(), children: vec![mdhtml::Inline::Text(title.into())], number: None }],
        ..Document::default() };
    mdhtml::render_md(&document)
}

/// Convert and fully clean one article; short/non-content articles return None.
pub fn convert_wikitext(source: &str, templates: &dyn TemplateLookup) -> Option<Converted> {
    let converted = convert_document(source, templates)?;
    Some(Converted { md: mdhtml::render_md(&converted.document), warnings: converted.warnings })
}
