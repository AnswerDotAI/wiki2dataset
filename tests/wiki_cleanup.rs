use mdhtml::{parse_wikitext, render};
use wiki2dataset::{TemplateAction, TemplateInfo, TemplateLookup, clean};

const LONG: &str = "This paragraph contains enough ordinary article text to pass the historical sixty character body filter.";

fn cleaned(source: &str, lookup: &dyn TemplateLookup) -> String {
    let mut document = parse_wikitext(source);
    assert!(clean(&mut document, lookup));
    render(&document)
}

#[test]
fn content_and_section_rules() {
    let source = format!(
        "{{{{sfn|lead}}}}\n{LONG} [[Page|''linked'']] [[File:x.png]] {{{{sfn|note}}}} {{{{unknown|kept}}}}\n\n== References ==\nremove\n\n== History ==\nkeep"
    );
    let html = cleaned(&source, &());
    assert!(html.starts_with(&format!("<p>{LONG}")), "{html}");
    assert!(html.contains("<em>linked</em>") && !html.contains("<a "));
    assert!(!html.contains("<img") && !html.contains("data-name=\"sfn\""));
    assert!(
        html.contains("data-name=\"unknown\"") && !html.contains("remove") && html.contains("keep")
    );
}

#[test]
fn value_and_presentational_templates() {
    let source = format!(
        "{LONG} {{{{val|7|e=-29|u=g/cm3}}}} {{{{val|4.7|0.20|u=μs}}}} {{{{val|1.65|+0.4|-0.1|ul=μs}}}} {{{{frac|26|1|2}}}} {{{{chem2|H3O+|link=hydronium}}}} {{{{lang|fr|raison}}}} {{{{nowrap|0.032 ''Pa''}}}} {{{{left|French}}}}{{{{left}}}} {{{{center|French}}}}{{{{center}}}} {{{{right|French}}}}{{{{right}}}} {{{{no}}}} {{{{yes|Confirmed}}}} {{{{abbr|NASA|National Aeronautics and Space Administration}}}}"
    );
    let html = cleaned(&source, &Lookup);
    assert!(html.contains("7 \\times 10^{-29}</span> g/cm3"), "{html}");
    assert!(
        html.contains("4.7 ± 0.20 μs") && html.contains("1.65^{+0.4}_{-0.1}</span> μs"),
        "{html}"
    );
    assert!(
        html.contains("26 1/2") && html.contains("H3O+") && html.contains("raison"),
        "{html}"
    );
    assert!(
        html.contains("0.032 <em>Pa</em>") && !html.contains("data-name=\"nowrap\""),
        "{html}"
    );
    assert!(
        html.contains("French French French No Confirmed NASA"),
        "{html}"
    );
    assert!(
        !["left", "center", "right", "abbr"]
            .iter()
            .any(|name| html.contains(&format!("data-name=\"{name}\""))),
        "{html}"
    );
}

#[test]
fn unsupported_table_content_remains_cleanable() {
    let source = format!(
        "{LONG}\n\n{{| style=float:right\n! {{{{no}}}}\n| {{{{abbr|NASA|National Aeronautics and Space Administration}}}}\n|}}"
    );
    let html = cleaned(&source, &Lookup);
    assert!(html.contains("No") && html.contains("NASA"), "{html}");
    assert!(
        !html.contains("data-format=\"wikitext\"")
            && !html.contains("data-name=\"abbr\"")
            && !html.contains("data-name=\"no\""),
        "{html}"
    );
}

struct Lookup;

impl TemplateLookup for Lookup {
    fn template(&self, name: &str) -> TemplateInfo {
        match name {
            "ndash" => TemplateInfo {
                name: "En_dash".into(),
                action: TemplateAction::Keep,
            },
            "bull" => TemplateInfo {
                name: "•".into(),
                action: TemplateAction::Keep,
            },
            "main" => TemplateInfo {
                name: "Main".into(),
                action: TemplateAction::Drop,
            },
            "styled" => TemplateInfo {
                name: name.into(),
                action: TemplateAction::Unwrap,
            },
            "abbr" | "tooltip" => TemplateInfo {
                name: name.into(),
                action: TemplateAction::UnwrapFirst,
            },
            "val" | "ipac-en" => TemplateInfo {
                name: name.into(),
                action: TemplateAction::Drop,
            },
            _ => TemplateInfo {
                name: name.into(),
                action: TemplateAction::Keep,
            },
        }
    }
}

#[test]
fn metadata_roles_and_anchors() {
    let source = format!(
        "{LONG} {{{{main|Physics}}}}{{{{flag|Algeria}}}}{{{{Infobox person|name=Noise}}}}{{{{anchor|one place|two}}}}{{{{ndash}}}}{{{{bull}}}} {{{{val|2.7255|u=K}}}} {{{{IPAc-en|æ|l}}}} {{{{styled|red|Warning|align=center}}}} {{{{tooltip|NASA|National Aeronautics and Space Administration}}}}"
    );
    let html = cleaned(&source, &Lookup);
    assert!(
        !html.contains("data-name=\"main\"")
            && !html.contains("Infobox person")
            && html.contains("data-name=\"flag\"")
    );
    assert!(
        html.contains("<span id=\"one_place\"></span><span id=\"two\"></span>–\u{a0}• "),
        "{html}"
    );
    assert!(html.contains("2.7255 K"), "{html}");
    assert!(html.contains("IPAC: æl"), "{html}");
    assert!(
        html.contains("red|Warning|center") && !html.contains("data-name=\"styled\""),
        "{html}"
    );
    assert!(
        html.contains("NASA")
            && !html.contains("National Aeronautics")
            && !html.contains("data-name=\"tooltip\""),
        "{html}"
    );
}
#[test]
fn simple_value_templates() {
    let html = cleaned(
        &format!("{LONG} {{{{FORMATNUM:64000}}}} {{{{transliteration|ar|ALA|Allāh}}}}"),
        &(),
    );
    assert!(
        html.contains("64000 Allāh")
            && !html.contains("FORMATNUM")
            && !html.contains("transliteration"),
        "{html}"
    );
}

#[test]
fn raw_islands_are_preserved_and_short_pages_are_rejected() {
    let source = format!("{LONG}\n\n<div class=\"layout\">\nraw article content\n</div>");
    let html = cleaned(&source, &());
    assert!(
        html.contains("data-format=\"wikitext\"") && html.contains("raw article content"),
        "{html}"
    );
    let mut short = parse_wikitext("short");
    assert!(!clean(&mut short, &()));
}

#[test]
fn documents_emptied_by_cleanup_are_rejected() {
    struct Drop;
    impl TemplateLookup for Drop {
        fn template(&self, name: &str) -> TemplateInfo {
            TemplateInfo {
                name: name.into(),
                action: TemplateAction::Drop,
            }
        }
    }
    let mut document = parse_wikitext(
        "{{discard|This template argument contains enough prose to pass the initial article length filter before cleanup.}}",
    );
    assert!(!clean(&mut document, &Drop));
}
