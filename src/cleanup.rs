//! Content cleanup for MediaWiki documents.

use mdhtml::{Attr, Block, DefinitionItem, Document, Inline, Operation};
use std::collections::HashMap;

const SECTIONS: [&str; 10] = [
    "references",
    "further reading",
    "external links",
    "see also",
    "sources",
    "citations",
    "bibliography",
    "notes",
    "works cited",
    "related pages",
];
const DROP_TEMPLATES: [&str; 38] = [
    "sfn",
    "sfnm",
    "efn",
    "rp",
    "bots",
    "representative",
    "cn",
    "cite",
    "clear",
    "citation needed",
    "good article",
    "primary inline",
    "cite journal",
    "cite web",
    "cite book",
    "short description",
    "birth date",
    "death date",
    "pp-semi",
    "toc limit",
    "stack end",
    "cs1 config",
    "infobox",
    "infobox grapheme",
    "pp",
    "pp-move",
    "use article",
    "see below",
    "respell",
    "etymology",
    "webarchive",
    "div col",
    "div col end",
    "use mdy dates",
    "use dmy dates",
    "use american english",
    "use british english",
    "monththisyear",
];
const TRANSPARENT: [&str; 14] = [
    "nowrap",
    "nobr",
    "nowraplinks",
    "small",
    "smaller",
    "big",
    "large",
    "larger",
    "midsize",
    "nobold",
    "noitalic",
    "left",
    "center",
    "right",
];

#[derive(Clone, Debug)]
pub struct TemplateInfo {
    pub name: String,
    pub action: TemplateAction,
}

#[derive(Clone, Copy, Debug, Default, Eq, PartialEq)]
pub enum TemplateAction {
    #[default]
    Keep,
    Drop,
    Unwrap,
    UnwrapFirst,
}

pub trait TemplateLookup: Sync {
    fn template(&self, name: &str) -> TemplateInfo;
}

impl TemplateLookup for () {
    fn template(&self, name: &str) -> TemplateInfo {
        TemplateInfo {
            name: name.to_string(),
            action: TemplateAction::Keep,
        }
    }
}

#[derive(Default)]
struct Args {
    positional: Vec<Option<Vec<Inline>>>,
    named: HashMap<String, Vec<Inline>>,
}

fn args(operation: &Operation) -> Option<Args> {
    let mut out = Args::default();
    for arg in &operation.args {
        let name = arg.name.as_deref().unwrap_or("").trim().to_lowercase();
        if name.is_empty() {
            out.positional.push(Some(arg.children.clone()));
        } else if let Ok(position) = name.parse::<usize>() {
            let position = position.checked_sub(1)?;
            if position >= operation.args.len() {
                return None;
            }
            out.positional
                .resize_with(out.positional.len().max(position + 1), || None);
            if out.positional[position]
                .replace(arg.children.clone())
                .is_some()
            {
                return None;
            }
        } else if out.named.insert(name, arg.children.clone()).is_some() {
            return None;
        }
    }
    Some(out)
}

fn plain(items: &[Inline]) -> String {
    let mut out = String::new();
    for item in items {
        match item {
            Inline::Text(text) | Inline::Html(text) => out.push_str(text),
            Inline::SoftBreak | Inline::HardBreak => out.push(' '),
            Inline::Emph { children, .. }
            | Inline::Strong { children, .. }
            | Inline::Strike { children, .. }
            | Inline::Highlight { children, .. }
            | Inline::Link { children, .. }
            | Inline::Note { children }
            | Inline::Span { children, .. } => out.push_str(&plain(children)),
            Inline::Superscript { text, .. }
            | Inline::Subscript { text, .. }
            | Inline::Code { text, .. }
            | Inline::Raw { text, .. }
            | Inline::Math { tex: text, .. } => out.push_str(text),
            Inline::Image { alt, .. } => out.push_str(&plain(alt)),
            Inline::Autolink { text, .. } => out.push_str(text),
            Inline::Operation(operation) => {
                for arg in &operation.args {
                    out.push_str(&plain(&arg.children))
                }
            }
            Inline::TemplateToken { body, .. } => out.push_str(body),
            Inline::FootnoteRef { .. } => {}
        }
    }
    out
}

fn values(args: &Args) -> Option<Vec<String>> {
    args.positional
        .iter()
        .map(|item| item.as_deref().map(|item| plain(item).trim().to_string()))
        .collect()
}

fn named(args: &Args, name: &str) -> Option<String> {
    args.named
        .get(name)
        .map(|item| plain(item).trim().to_string())
}

fn only_named(args: &Args, allowed: &[&str]) -> bool {
    args.named
        .keys()
        .all(|name| allowed.contains(&name.as_str()))
}

fn number(value: &str) -> bool {
    let value = value.strip_prefix(['+', '-', '−']).unwrap_or(value);
    let mut dot = false;
    let mut digit = false;
    for ch in value.chars() {
        match ch {
            '0'..='9' => digit = true,
            ',' => {}
            '.' if !dot => dot = true,
            _ => return false,
        }
    }
    digit
}

fn text(value: impl Into<String>) -> Vec<Inline> {
    vec![Inline::Text(value.into())]
}

fn math(tex: impl Into<String>) -> Vec<Inline> {
    vec![Inline::Math {
        attrs: Attr::default(),
        display: false,
        tex: tex.into(),
    }]
}

fn unit(args: &Args) -> Option<String> {
    if args.named.contains_key("u") && args.named.contains_key("ul")
        || args.named.contains_key("up") && args.named.contains_key("upl")
    {
        return None;
    }
    let unit = named(args, "u")
        .or_else(|| named(args, "ul"))
        .unwrap_or_default();
    let per = named(args, "up")
        .or_else(|| named(args, "upl"))
        .unwrap_or_default();
    Some(match (unit.is_empty(), per.is_empty()) {
        (false, false) => format!("{unit}/{per}"),
        (false, true) => unit,
        (true, false) => format!("/{per}"),
        (true, true) => String::new(),
    })
}

fn val(args: &Args) -> Option<Vec<Inline>> {
    if !only_named(args, &["u", "ul", "up", "upl", "e", "fmt"]) {
        return None;
    }
    let values = values(args)?;
    if values.is_empty() || values.iter().any(String::is_empty) {
        return None;
    }
    let unit = unit(args)?;
    let mut result = if let Some(exponent) = named(args, "e") {
        (values.len() == 1 && number(&values[0]) && number(&exponent))
            .then(|| math(format!("{} \\times 10^{{{exponent}}}", values[0])))?
    } else if values.len() == 1 {
        text(&values[0])
    } else if values.len() == 2 && values.iter().all(|x| number(x)) {
        text(format!("{} ± {}", values[0], values[1]))
    } else if values.len() == 2 && values[1].starts_with('(') && values[1].ends_with(')') {
        text(values.concat())
    } else if values.len() == 3 && matches!(values[1].as_str(), "×" | "/" | "to") {
        text(if values[1] == "to" {
            format!("{}–{}", values[0], values[2])
        } else {
            values.join(" ")
        })
    } else if values.len() == 3
        && values[1].starts_with('+')
        && values[2].starts_with('-')
        && values.iter().all(|x| number(x))
    {
        math(format!("{}^{{{}}}_{{{}}}", values[0], values[1], values[2]))
    } else {
        return None;
    };
    if !unit.is_empty() {
        result.push(Inline::Text(format!(" {unit}")))
    }
    Some(result)
}

fn convert(args: &Args) -> Option<Vec<Inline>> {
    if !only_named(
        args,
        &[
            "abbr", "adj", "disp", "flip", "lk", "order", "round", "sigfig", "sp", "spelling",
        ],
    ) {
        return None;
    }
    let values = values(args)?;
    if values.len() >= 4
        && matches!(values[1].to_lowercase().as_str(), "-" | "–" | "to")
        && number(&values[0])
        && number(&values[2])
        && !values[3].is_empty()
    {
        Some(text(format!("{}–{} {}", values[0], values[2], values[3])))
    } else if values.len() >= 2 && number(&values[0]) && !values[1].is_empty() {
        Some(text(format!("{} {}", values[0], values[1])))
    } else {
        None
    }
}

fn frac(args: &Args) -> Option<Vec<Inline>> {
    if !args.named.is_empty() || !(1..=3).contains(&args.positional.len()) {
        return None;
    }
    let values = values(args)?;
    if values.iter().any(String::is_empty) {
        return None;
    }
    Some(text(match values.as_slice() {
        [denom] => format!("1/{denom}"),
        [num, denom] => format!("{num}/{denom}"),
        [whole, num, denom] => format!("{whole} {num}/{denom}"),
        _ => unreachable!(),
    }))
}

fn anchors(args: &Args) -> Option<Vec<Inline>> {
    if !args.named.is_empty() || args.positional.is_empty() {
        return None;
    }
    let values = values(args)?;
    if values.iter().any(String::is_empty) {
        return None;
    }
    Some(
        values
            .into_iter()
            .map(|value| Inline::Span {
                attrs: Attr {
                    id: Some(value.split_whitespace().collect::<Vec<_>>().join("_")),
                    ..Attr::default()
                },
                children: Vec::new(),
            })
            .collect(),
    )
}

fn handler(name: &str, args: &Args) -> Option<Vec<Inline>> {
    let values = values(args)?;
    let first = || values.first().cloned().unwrap_or_default();
    let last = || values.last().cloned().unwrap_or_default();
    let value = match name {
        "coord" => values.join(" "),
        "lang" | "langx" | "transliteration" => last(),
        "nbsp" => "\u{a0}".to_string(),
        "circa" => format!("c. {}", first()),
        "tlit" => values
            .get(1)
            .filter(|x| !x.is_empty())
            .cloned()
            .unwrap_or_else(first),
        "ill" | "angbr" | "cx" => first(),
        "gph" => format!("|{}|", first()),
        "ipaslink" => format!("IPAS: {}", first()),
        "ipac-en" => format!("IPAC: {}", values.join("")),
        _ => return None,
    };
    Some(text(value))
}

fn tag(args: &Args) -> Option<Vec<Inline>> {
    let values = values(args)?;
    let name = values.first()?.to_lowercase();
    match name.trim() {
        "ref" | "references" => Some(Vec::new()),
        "math" if values.len() == 2 && only_named(args, &["display"]) => Some(vec![Inline::Math {
            attrs: Attr::default(),
            display: named(args, "display").is_some_and(|x| x.eq_ignore_ascii_case("block")),
            tex: values[1].clone(),
        }]),
        "span" if args.positional.len() == 2 => {
            Some(args.positional[1].clone().unwrap_or_default())
        }
        _ => None,
    }
}

fn operation(operation: Operation, lookup: &dyn TemplateLookup) -> Vec<Inline> {
    if operation.syntax == "mediawiki"
        && operation.action == "function"
        && operation.name.eq_ignore_ascii_case("tag")
    {
        let Some(args) = args(&operation) else {
            return vec![Inline::Operation(operation)];
        };
        return tag(&args).unwrap_or_else(|| vec![Inline::Operation(operation)]);
    }
    if operation.syntax != "mediawiki" || operation.action != "transclude" {
        return vec![Inline::Operation(operation)];
    }
    let source = operation.name.trim();
    if source
        .get(..10)
        .is_some_and(|prefix| prefix.eq_ignore_ascii_case("formatnum:"))
    {
        return text(source[10..].trim());
    }
    let info = lookup.template(source);
    let name = info.name.replace('_', " ").to_lowercase();
    if ["defaultsort:", "defaultcategorysort:", "displaytitle:"]
        .iter()
        .any(|prefix| name.starts_with(prefix))
        || name == "infobox"
        || name.starts_with("infobox ")
        || name.starts_with("infobox/")
        || DROP_TEMPLATES.contains(&name.as_str())
    {
        return Vec::new();
    }
    let Some(args) = args(&operation) else {
        return vec![Inline::Operation(operation)];
    };
    match name.as_str() {
        "•" => text("\u{a0}• "),
        "ndash" | "en dash" => text("–"),
        "mdash" | "em dash" => text("—"),
        "val" => val(&args).unwrap_or_else(|| vec![Inline::Operation(operation)]),
        "convert" | "cvt" => convert(&args).unwrap_or_else(|| vec![Inline::Operation(operation)]),
        "frac" | "sfrac" => frac(&args).unwrap_or_else(|| vec![Inline::Operation(operation)]),
        "chem2" if only_named(&args, &["link"]) && args.positional.len() == 1 => args.positional[0]
            .clone()
            .unwrap_or_else(|| vec![Inline::Operation(operation)]),
        "yes" | "no" => args
            .positional
            .first()
            .cloned()
            .flatten()
            .unwrap_or_else(|| text(if name == "yes" { "Yes" } else { "No" })),
        "anchor" => anchors(&args).unwrap_or_else(|| vec![Inline::Operation(operation)]),
        _ if TRANSPARENT.contains(&name.as_str())
            && only_named(&args, &[])
            && args.positional.len() <= 1 =>
        {
            args.positional
                .first()
                .cloned()
                .flatten()
                .unwrap_or_default()
        }
        _ if name.starts_with("lang-")
            && name[5..]
                .chars()
                .all(|c| c.is_ascii_alphanumeric() || c == '-')
            && only_named(&args, &["links"])
            && args.positional.len() == 1 =>
        {
            args.positional[0]
                .clone()
                .unwrap_or_else(|| vec![Inline::Operation(operation)])
        }
        "coord" | "lang" | "langx" | "transliteration" | "nbsp" | "circa" | "tlit" | "ill"
        | "angbr" | "cx" | "gph" | "ipaslink" | "ipac-en" => {
            handler(&name, &args).unwrap_or_else(|| vec![Inline::Operation(operation)])
        }
        _ if info.action == TemplateAction::Unwrap => {
            let mut out = Vec::new();
            for arg in &operation.args {
                if !out.is_empty() {
                    out.push(Inline::Text("|".into()))
                }
                out.extend(arg.children.clone());
            }
            out
        }
        _ if info.action == TemplateAction::UnwrapFirst => args
            .positional
            .first()
            .cloned()
            .flatten()
            .unwrap_or_default(),
        _ if info.action == TemplateAction::Drop => Vec::new(),
        _ => vec![Inline::Operation(operation)],
    }
}

fn clean_inlines(items: &mut Vec<Inline>, lookup: &dyn TemplateLookup) {
    let mut out = Vec::with_capacity(items.len());
    for mut item in std::mem::take(items) {
        match &mut item {
            Inline::Emph { children, .. }
            | Inline::Strong { children, .. }
            | Inline::Strike { children, .. }
            | Inline::Highlight { children, .. }
            | Inline::Span { children, .. } => clean_inlines(children, lookup),
            Inline::Link {
                attrs,
                children,
                url,
                ..
            } => {
                clean_inlines(children, lookup);
                if attrs
                    .pairs
                    .iter()
                    .any(|(name, _)| name == "data-mediawiki-category")
                {
                    continue;
                }
                if url.starts_with("./") || url.starts_with('#') {
                    out.append(children);
                    continue;
                }
            }
            Inline::Image { .. } | Inline::Note { .. } | Inline::FootnoteRef { .. } => continue,
            Inline::Raw { format, text } if format == "html" && text.starts_with("<!--") => {
                continue;
            }
            Inline::Operation(operation) => {
                for arg in &mut operation.args {
                    clean_inlines(&mut arg.children, lookup)
                }
                out.extend(self::operation(operation.clone(), lookup));
                continue;
            }
            _ => {}
        }
        if matches!(&item, Inline::Span { attrs, .. } if attrs.pairs.iter().any(|(name, _)| name == "data-mediawiki-behavior"))
        {
            continue;
        }
        out.push(item);
    }
    *items = out;
    while matches!(items.first(), Some(Inline::SoftBreak)) {
        items.remove(0);
    }
    while matches!(items.last(), Some(Inline::SoftBreak)) {
        items.pop();
    }
}

fn clean_blocks(blocks: &mut Vec<Block>, lookup: &dyn TemplateLookup) {
    let mut out = Vec::with_capacity(blocks.len());
    for mut block in std::mem::take(blocks) {
        match &mut block {
            Block::Paragraph { children, .. } | Block::Heading { children, .. } => {
                clean_inlines(children, lookup)
            }
            Block::BlockQuote { children, .. } | Block::Div { children, .. } => {
                clean_blocks(children, lookup)
            }
            Block::List { items, .. } => {
                for item in items {
                    clean_blocks(&mut item.blocks, lookup)
                }
            }
            Block::DefinitionList { items, .. } => {
                for DefinitionItem { terms, definitions } in items {
                    for term in terms {
                        clean_inlines(&mut term.inlines, lookup)
                    }
                    for definition in definitions {
                        clean_inlines(definition, lookup)
                    }
                }
            }
            Block::Table {
                head,
                rows,
                foot,
                caption,
                row_tokens,
                ..
            } => {
                clean_inlines(caption, lookup);
                for row in head.iter_mut().chain(rows).chain(foot) {
                    for cell in &mut row.cells {
                        clean_inlines(&mut cell.content, lookup)
                    }
                }
                for (_, token) in row_tokens {
                    let mut items = vec![token.clone()];
                    clean_inlines(&mut items, lookup);
                    if let Some(item) = items.pop() {
                        *token = item
                    }
                }
            }
            Block::Figure { .. } => continue,
            _ => {}
        }
        let empty = matches!(&block, Block::Paragraph { children, .. } if plain(children).trim().is_empty());
        let noise =
            matches!(&block, Block::Paragraph { children, .. } if is_noise(&plain(children)));
        if !empty && !noise {
            out.push(block)
        }
    }
    *blocks = out;
}

fn is_noise(text: &str) -> bool {
    let lower = text.trim_start().to_lowercase();
    if lower.starts_with("file:") || lower.starts_with("<file:") {
        return true;
    }
    let mut parts = lower.split_whitespace();
    matches!(parts.next(), Some("poly" | "rect" | "circle"))
        && parts.take(2).all(|x| x.chars().all(|c| c.is_ascii_digit()))
}

fn paragraph_chars(blocks: &[Block]) -> usize {
    blocks
        .iter()
        .map(|block| match block {
            Block::Paragraph { children, .. } => plain(children).chars().count(),
            Block::BlockQuote { children, .. } | Block::Div { children, .. } => {
                paragraph_chars(children)
            }
            Block::List { items, .. } => {
                items.iter().map(|item| paragraph_chars(&item.blocks)).sum()
            }
            _ => 0,
        })
        .sum()
}

fn drop_sections(blocks: &mut Vec<Block>) {
    let mut out = Vec::with_capacity(blocks.len());
    let mut skipping = None;
    for block in std::mem::take(blocks) {
        let heading = match &block {
            Block::Heading {
                level, children, ..
            } => Some((*level, plain(children).to_lowercase())),
            _ => None,
        };
        if heading.as_ref().is_some_and(|(level, text)| {
            *level <= 2 && SECTIONS.iter().any(|name| text.contains(name))
        }) {
            skipping = heading.map(|(level, _)| level);
        } else if skipping
            .is_some_and(|stop| heading.as_ref().is_none_or(|(level, _)| *level > stop))
        {
        } else {
            skipping = None;
            out.push(block);
        }
    }
    *blocks = out;
}

/// Apply the complete article-content cleanup. Returns false for short pages.
pub fn clean(document: &mut Document, lookup: &dyn TemplateLookup) -> bool {
    if paragraph_chars(&document.blocks) < 60 {
        return false;
    }
    clean_blocks(&mut document.blocks, lookup);
    drop_sections(&mut document.blocks);
    document.footnotes.clear();
    !document.blocks.is_empty()
}
