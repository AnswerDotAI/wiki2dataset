import re, sqlite3


PREFIXES = {10: "template:", 14: "category:"}
ROLE_RULES = dict(hatnote=dict(categories=("Hatnote_templates", "Hatnote_templates_for_names", "Disambiguation_and_redirection_hatnote_templates",
    "Inline_hatnote_templates", "Hatnote_templates_for_lists", "Wikipedia_page-section_templates")),
    navigation=dict(categories=("Navigational_boxes",), page_suffixes=("_nav",), leading_wrapper=True),
    sidebar=dict(categories=("Sidebar_templates",), pages=("Sidebar",), wrapper_targets=("#invoke:sidebar",), leading_wrapper=True),
    infobox=dict(categories=("Infobox_templates",), pages=("Infobox",), page_prefixes=("Infobox_", "Infobox/"), leading_wrapper=True),
    citation=dict(categories=("Citation_templates", "Footnote_templates"), ancestor_suffixes=("citation_templates",)),
    maintenance=dict(categories=("Templates_needing_substitution_checking", "Protection_templates", "Templates_with_no_visible_output"),
        category_suffixes=("maintenance_templates",), ancestor_suffixes=("maintenance_templates",)),
    sister_project=dict(categories=("Sister_project_templates",)),
    external_link=dict(category_suffixes=("external_link_templates",), ancestor_suffixes=("external_link_templates",)),
    authority=dict(categories=("Wikipedia_authority_control",)),
    unwrap=dict(categories=("Semantic_markup_templates", "Sorting_templates"),
        category_suffixes=("color_templates", "formatting_templates", "format_templates"),
        ancestor_suffixes=("color_templates", "formatting_templates", "format_templates")),
    unwrap1=dict(category_suffixes=("abbreviation_templates",), ancestor_suffixes=("abbreviation_templates",)),
    # `ok` means an unresolved template is an intentional passthrough: its name and arguments remain in the output,
    # but it is omitted from the audit. It never means the template is unimportant or may be discarded.
    ok=dict(categories=("Minor_planets_templates", "Coordinates_templates", "Association_football_templates", "Flag_template_system",
        "Flag_templates", "Correct_title_templates", "Population_templates", "Catalog_lookup_templates", "Language_tag_handling_templates",
        "Conversion_templates", "Time,_date_and_calendar_templates", "Audio_templates", "IPA_templates",
        "Multilingual_support_templates_by_language_family", "Direction-related_icon_templates", "Inline_spacing_templates",
        "Wikipedia_multilingual_support_templates", "International_Phonetic_Alphabet_templates", "Chart,_diagram_and_graph_templates",
        "Quotation_templates", "Weather_box", "Collapse_templates"),
        pages=("Chem",), page_prefixes=("IPA-", "IPA_"), page_suffixes=("_header", "population"),
        category_suffixes=("table_cell_templates", "link_templates", "function_templates", "table_wikitext", "chart_templates",
            "flag_templates", "multilingual_support_templates", "image_insertion_templates"),
        ancestor_suffixes=("table_cell_templates", "link_templates", "function_templates", "table_wikitext", "chart_templates",
            "flag_templates", "multilingual_support_templates", "image_insertion_templates"),
        body_prefixes=("|-", "|+", "{|", "[[file:")))
DEFAULT_ROLES = {role:rule.get("categories", ()) for role,rule in ROLE_RULES.items() if role != "ok"}


def wiki_title(name, namespace):
    "Normalize the common MediaWiki title forms used in dump metadata."
    name = name.strip()
    prefix = PREFIXES.get(namespace)
    if prefix and name.casefold().startswith(prefix): name = name.split(":", 1)[1]
    name = re.sub(r"[ _]+", "_", name.strip())
    return name[:1].upper()+name[1:]


def transcluded_body(wikitext):
    "Return the part of template source that is transcluded into articles."
    text = re.sub(r"<!--.*?-->", "", wikitext or "", flags=re.S)
    only = re.findall(r"<onlyinclude\b[^>]*>(.*?)</onlyinclude\s*>", text, flags=re.I | re.S)
    if only: text = "".join(only)
    text = re.sub(r"<noinclude\b[^>]*>.*?</noinclude\s*>", "", text, flags=re.I | re.S)
    text = re.sub(r"<noinclude\b[^>]*/\s*>", "", text, flags=re.I)
    return re.sub(r"</?(?:includeonly|onlyinclude)\b[^>]*>", "", text, flags=re.I).strip()


def _outside_calls(text):
    out,stack,i = [],[],0
    while i < len(text):
        if text.startswith("{{{", i):
            stack.append(3)
            i += 3
        elif text.startswith("{{", i):
            stack.append(2)
            i += 2
        elif stack and text.startswith("}"*stack[-1], i): i += stack.pop()
        else:
            if not stack: out.append(text[i])
            i += 1
    return "".join(out)


def content_profile(wikitext, wrapper=None):
    "Summarize implementation content without attempting template expansion."
    body = transcluded_body(wikitext)
    parameters = {}
    for name in re.findall(r"\{\{\{\s*([^|{}]+)", body):
        name = name.strip()
        parameters[name] = parameters.get(name, 0)+1
    outside = re.sub(r"<[^>]*>", " ", _outside_calls(body))
    outside = " ".join(outside.split())
    shape = "delegate" if wrapper else "parameter_driven" if parameters and not outside else "mixed"
    return dict(body=body, parameters=parameters, outside_text=outside, shape=shape)


class TemplateDB:
    "Inspect redirect-aware template source, categories, and semantic roles."
    def __init__(self, path, roles=None):
        self.db = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        custom = roles is not None
        roles = DEFAULT_ROLES if roles is None else roles
        self.role_categories = {role: frozenset(wiki_title(x, 14) for x in ([cats] if isinstance(cats, str) else cats))
            for role,cats in roles.items()}
        self.redirects = {(namespace,title): (to_namespace,to_title)
            for namespace,title,to_namespace,to_title in self.db.execute("SELECT * FROM redirect")}
        self._resolved,self._categories,self._wikitext,self._scopes = {},{},{},{}
        self.role_map,self.ok_titles = None,frozenset()
        self.ok_prefixes = () if custom else tuple(x.replace(" ", "_").casefold() for x in ROLE_RULES["ok"].get("page_prefixes", ()))
        self.ok_suffixes = () if custom else tuple(x.replace(" ", "_").casefold() for x in ROLE_RULES["ok"].get("page_suffixes", ()))
        has_roles = self.db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='template_role'").fetchone()
        if not custom and has_roles:
            self.role_bits = {role:1<<i for i,role in enumerate(self.role_categories)}
            self.role_map = {}
            ok = set()
            for title,role in self.db.execute("SELECT title,role FROM template_role"):
                if role == "ok": ok.add(title)
                elif role in self.role_bits: self.role_map[title] = self.role_map.get(title, 0) | self.role_bits[role]
            self.ok_titles = frozenset(ok)
            self.role_sets = {0:frozenset()}

    def close(self): self.db.close()

    def resolve(self, name):
        namespace,title,seen = 10,wiki_title(name, 10),set()
        if (cached := self._resolved.get((namespace,title))) is not None: return cached
        source = namespace,title
        while (namespace,title) not in seen:
            seen.add((namespace,title))
            row = self.redirects.get((namespace,title))
            if row is None: break
            namespace,title = row
        self._resolved[source] = namespace,title
        return namespace,title

    def categories(self, name):
        namespace,title = self.resolve(name)
        if (cached := self._categories.get((namespace,title))) is not None: return cached
        result = frozenset(x[0] for x in self.db.execute("SELECT category_title FROM categorylink WHERE namespace=? AND title=?", (namespace,title)))
        self._categories[namespace,title] = result
        return result

    def wikitext(self, name):
        "Return the canonical template's source, following redirects."
        namespace,title = self.resolve(name)
        if namespace != 10: return None
        if title not in self._wikitext:
            row = self.db.execute("SELECT wikitext FROM page WHERE namespace=10 AND title=?", (title,)).fetchone()
            self._wikitext[title] = row[0] if row else None
        return self._wikitext[title]

    def roles(self, name):
        if self.role_map is not None:
            namespace,title = self.resolve(name)
            bits = self.role_map.get(title, 0) if namespace == 10 else 0
            if bits not in self.role_sets: self.role_sets[bits] = frozenset(role for role,bit in self.role_bits.items() if bits & bit)
            return self.role_sets[bits]
        categories = self.categories(name)
        return frozenset(role for role,roots in self.role_categories.items() if categories & roots)

    def ok(self, name):
        "Whether an unresolved template is expected and should be omitted from audits."
        namespace,title = self.resolve(name)
        folded = title.casefold()
        return namespace == 10 and (title in self.ok_titles or folded.startswith(self.ok_prefixes) or folded.endswith(self.ok_suffixes))

    def template(self, name):
        "Return the metadata used to understand and classify a template."
        namespace,title = self.resolve(name)
        if namespace != 10: return None
        page = self.db.execute("SELECT wikitext FROM template WHERE title=?", (title,)).fetchone()
        if page is None: return None
        categories = self.categories(name)
        placeholders = ",".join("?"*len(categories))
        parents = frozenset(x[0] for x in self.db.execute(
            f"SELECT DISTINCT parent_title FROM category_parent WHERE title IN ({placeholders})", tuple(categories))) if categories else frozenset()
        wrapper = self.db.execute("SELECT target_title,exclusive FROM template_wrapper WHERE title=?", (title,)).fetchone()
        wrapper = None if wrapper is None else dict(target=wrapper[0], exclusive=bool(wrapper[1]))
        return dict(title=title, wikitext=page[0], content=content_profile(page[0], wrapper), categories=categories, ancestor_categories=parents,
            roles=self.roles(name), ok=self.ok(name), wrapper=wrapper)

    def inspect(self, names, source_chars=500):
        "Return source, classification, and possible general rules with their blast radius."
        result = {}
        for name in names:
            info = self.template(name)
            if info is None:
                matches = self.db.execute("SELECT title FROM template WHERE title=? COLLATE NOCASE LIMIT 2", (wiki_title(name, 10),)).fetchall()
                if len(matches) == 1: info = self.template(matches[0][0])
            if info is not None and source_chars is not None and len(info["wikitext"]) > source_chars:
                body = info["content"]["body"]
                info = info | {"wikitext":info["wikitext"][:source_chars]+"…",
                    "content":info["content"] | {"body":body[:source_chars]+"…" if len(body) > source_chars else body}}
            if info is not None: info = info | {"rule_candidates":self.rule_candidates(info["title"])}
            result[name] = info
        return result

    def _candidate(self, rule, value, title, select, params, examples):
        key = select,params,examples
        if key not in self._scopes:
            rows = self.db.execute(f"""WITH matches(title) AS ({select})
                SELECT title,count(*) OVER() FROM matches ORDER BY title LIMIT ?""", (*params,examples)).fetchall()
            self._scopes[key] = (rows[0][1] if rows else 0, [x[0] for x in rows])
        count,names = self._scopes[key]
        sample = [title]+[name for name in names if name != title]
        return dict(rule=rule, value=value, templates=count, examples=sample[:examples])

    def rule_candidates(self, name, examples=8):
        "Return plausible name/category rules, including scope size and examples."
        info = self.template(name)
        if info is None: return []
        title = info["title"]
        result = [self._candidate("page_prefix", title, title,
            "SELECT title FROM template WHERE substr(lower(title),1,length(?))=lower(?)", (title,title), examples)]
        if "_" in title:
            suffix = title.rsplit("_", 1)[1]
            result.append(self._candidate("page_suffix", suffix, title,
                "SELECT title FROM template WHERE substr(lower(title),-length(?))=lower(?)", (suffix,suffix), examples))
        if wrapper := info["wrapper"]:
            result.append(self._candidate("wrapper_target", wrapper["target"], title,
                "SELECT title FROM template_wrapper WHERE lower(target_title)=lower(?)", (wrapper["target"],), examples))
        for relation,key in (("category", "categories"), ("ancestor_category", "ancestor_categories")):
            for category in info[key]:
                select = """SELECT title FROM template_category WHERE category_title=?
                    UNION SELECT t.title FROM template_category t JOIN category_parent p
                    ON p.title=t.category_title WHERE p.parent_title=?"""
                result.append(self._candidate(relation, category, title, select, (category,category), examples))
        return sorted(result, key=lambda x: (x["templates"], x["rule"], x["value"]))

    def category_templates(self, category):
        "Return templates directly or one category level beneath `category`."
        category = wiki_title(category, 14)
        return tuple(x[0] for x in self.db.execute("""SELECT title FROM template_category WHERE category_title=?
            UNION SELECT t.title FROM template_category t JOIN category_parent p ON p.title=t.category_title WHERE p.parent_title=?
            ORDER BY title""", (category,category)))
