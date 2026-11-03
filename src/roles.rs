use crate::{TemplateAction, TemplateInfo, TemplateLookup};
use anyhow::{Context, Result};
use rusqlite::Connection;
use std::collections::{HashMap, HashSet};
use std::path::Path;

const DROP_ROLES: [&str; 9] = ["hatnote", "navigation", "sidebar", "infobox", "citation", "maintenance", "sister_project", "external_link", "authority"];

pub struct TemplateRoles {
    redirects: HashMap<(i64, String), (i64, String)>, drops: HashSet<String>, unwraps: HashSet<String>, unwrap_firsts: HashSet<String>, ok: HashSet<String>
}

impl TemplateRoles {
    pub fn open(path: impl AsRef<Path>) -> Result<Self> {
        let db = Connection::open_with_flags(path.as_ref(), rusqlite::OpenFlags::SQLITE_OPEN_READ_ONLY)
            .with_context(|| format!("opening wiki metadata {}", path.as_ref().display()))?;
        let redirects = {
            let mut query = db.prepare("SELECT namespace,title,to_namespace,to_title FROM redirect")?;
            query.query_map([], |row| Ok(((row.get(0)?, row.get(1)?), (row.get(2)?, row.get(3)?))))?.collect::<rusqlite::Result<_>>()?
        };
        let drops = {
            let placeholders = std::iter::repeat_n("?", DROP_ROLES.len()).collect::<Vec<_>>().join(",");
            let mut query = db.prepare(&format!("SELECT DISTINCT title FROM template_role WHERE role IN ({placeholders})"))?;
            query.query_map(rusqlite::params_from_iter(DROP_ROLES), |row| row.get(0))?.collect::<rusqlite::Result<_>>()?
        };
        let unwraps = {
            let mut query = db.prepare("SELECT title FROM template_role WHERE role='unwrap'")?;
            query.query_map([], |row| row.get(0))?.collect::<rusqlite::Result<_>>()?
        };
        let unwrap_firsts = {
            let mut query = db.prepare("SELECT title FROM template_role WHERE role='unwrap1'")?;
            query.query_map([], |row| row.get(0))?.collect::<rusqlite::Result<_>>()?
        };
        let ok = {
            let mut query = db.prepare("SELECT title FROM template_role WHERE role='ok'")?;
            query.query_map([], |row| row.get(0))?.collect::<rusqlite::Result<_>>()?
        };
        Ok(Self { redirects, drops, unwraps, unwrap_firsts, ok })
    }

    fn resolve(&self, source: &str) -> (i64, String) {
        let mut current = (10, wiki_title(source, 10));
        let mut seen = HashSet::new();
        while seen.insert(current.clone()) {
            let Some(next) = self.redirects.get(&current) else { break; };
            current = next.clone();
        }
        current
    }

    pub(crate) fn audit_template(&self, source: &str) -> Option<String> {
        let (namespace, title) = self.resolve(source);
        if namespace == 10 && self.ok.contains(&title) { return None; }
        Some(if namespace == 10 { title.replace('_', " ") } else { source.to_string() }.trim().to_lowercase())
    }
}

impl TemplateLookup for TemplateRoles {
    fn template(&self, source: &str) -> TemplateInfo {
        let (namespace, title) = self.resolve(source);
        let action = if namespace != 10 { TemplateAction::Keep }
        else if self.drops.contains(&title) { TemplateAction::Drop }
        else if self.unwraps.contains(&title) { TemplateAction::Unwrap }
        else if self.unwrap_firsts.contains(&title) { TemplateAction::UnwrapFirst }
        else { TemplateAction::Keep };
        TemplateInfo { name: if namespace == 10 { title.replace('_', " ") } else { source.to_string() }, action }
    }
}

pub fn wiki_title(source: &str, namespace: i64) -> String {
    let mut name = source.trim();
    let prefix = match namespace { 10 => Some("template:"), 14 => Some("category:"), _ => None };
    if let Some(prefix) = prefix && name.to_lowercase().starts_with(prefix) { name = name.split_once(':').unwrap().1; }
    let normalized = name.split(|ch| ch == ' ' || ch == '_').filter(|x| !x.is_empty()).collect::<Vec<_>>().join("_");
    let mut chars = normalized.chars();
    chars.next().map_or(String::new(), |first| first.to_uppercase().chain(chars).collect())
}
