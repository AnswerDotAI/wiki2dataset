import base64, io, json, sqlite3, tarfile, time

from wiki2dataset.templates import TemplateDB
from wiki2dataset import metadata as wiki_db
from wiki2dataset.metadata import build_metadata, refresh_roles


LICENSE = [dict(identifier="CC-BY-SA-4.0", name="Creative Commons", url="https://creativecommons.org/licenses/by-sa/4.0/")]


def _record(name, namespace, version, categories=(), redirects=(), wikitext=None):
    return dict(name=name, namespace={"identifier": namespace}, version={"identifier": version},
        categories=[{"name": x} for x in categories], redirects=[{"name": x} for x in redirects], license=LICENSE,
        article_body={"wikitext": wikitext} if wikitext is not None else None)


def _snapshot(path, records):
    payload = "".join(json.dumps(x)+"\n" for x in records).encode()
    info = tarfile.TarInfo("snapshot.ndjson")
    info.size = len(payload)
    with tarfile.open(path, "w:gz") as archive: archive.addfile(info, io.BytesIO(payload))


def test_snapshot_graph_redirects_latest_records_and_roles(tmp_path):
    templates,categories,props,dest = tmp_path/"templates.tar.gz",tmp_path/"categories.tar.gz",tmp_path/"page_props.sql",tmp_path/"templates.sqlite"
    _snapshot(templates, [
        _record("Template:Canonical name", 10, 1, ["Category:Wrong templates"], ["Template:Old alias"], "old"),
        _record("Template:Canonical name", 10, 2, ["Category:Specific templates"], ["Template:Alias"], "current"),
        _record("Template:Direct hatnote", 10, 1, ["Category:Hatnote templates"]),
        _record("Template:Name hatnote", 10, 1, ["Category:Hatnote templates for names"]),
        _record("Template:Weather data", 10, 1, ["Category:Weather box"]),
        _record("Template:Collapsed content", 10, 1, ["Category:Collapse templates"]),
        _record("Template:Sidebar", 10, 1),
        _record("Template:Lua sidebar", 10, 1, wikitext="{{#invoke:sidebar|sidebar}}"),
        _record("Template:Wrapped sidebar", 10, 1, wikitext="<noinclude>docs</noinclude><includeonly>{{Sidebar|body={{Nested}}}}</includeonly>"),
        _record("Template:Wrapped twice", 10, 1, wikitext="{{Wrapped sidebar}}"),
        _record("Template:Sidebar with extras", 10, 1, wikitext="{{Sidebar|body={{Nested}}}} {{Other}}"),
        _record("Template:Cite", 10, 1, ["Category:Citation templates"]),
        _record("Template:Citation with extras", 10, 1, wikitext="{{Cite}} {{Other}}"),
        _record("Template:Misleading sidebar category", 10, 1, ["Category:Sidebars with styles"]),
        _record("Template:Mixed sidebar", 10, 1, wikitext="article content {{Sidebar}}"),
        _record("Template:Verify", 10, 1, ["Category:Specific maintenance templates"]),
        _record("Template:Author link", 10, 1, ["Category:Author external link templates"]),
        _record("Template:Styled", 10, 1, ["Category:Specific presentation"]),
        _record("Template:Linked", 10, 1, ["Category:Internal link templates"]),
        _record("Template:Functional", 10, 1, ["Category:Image function templates"]),
        _record("Template:Tooltip", 10, 1, ["Category:Abbreviation templates"]),
        _record("Template:Partial table", 10, 1, ["Category:Templates with partial table wikitext"]),
        _record("Template:Chart", 10, 1, ["Category:Bar chart templates"]),
        _record("Template:Chinese", 10, 1, ["Category:Chinese multilingual support templates"]),
        _record("Template:Olympic flag", 10, 1, ["Category:Olympics flag templates"]),
        _record("Template:Checkmark", 10, 1, ["Category:Checkmark insertion templates"]),
        _record("Template:Footnote", 10, 1, ["Category:Footnote3 templates"]),
        _record("Template:Science ref", 10, 1, ["Category:Science journal citations"]),
        _record("Template:Invisible", 10, 1, ["Category:Templates with no visible output"]),
        _record("Template:Cell format", 10, 1, ["Category:Periodic table format templates"]),
        _record("Template:Generic header", 10, 1),
        _record("Template:Chem", 10, 1),
        _record("Template:Table row", 10, 1, wikitext="<noinclude>docs</noinclude>\n|-\n| {{{1}}}"),
        _record("Template:Image carrier", 10, 1, wikitext="<includeonly>[[File:{{{1}}}]]</includeonly>"),
        _record("Template:Cell", 10, 1, ["Category:Unified table cell templates"]),
        _record("Template:Older target", 10, 10, redirects=["Template:Shared alias"]),
        _record("Template:Newer target", 10, 20, redirects=["Template:Shared alias"])])
    # chkstyle: ignore-node
    _snapshot(categories, [_record("Category:Specific templates", 14, 1, ["Category:Hatnote templates", "Category:Chronology templates"]),
        _record("Category:Chronology templates", 14, 1, ["Category:Sidebar templates"]),
        _record("Category:Sidebar templates", 14, 1),
        _record("Category:Sidebars with styles", 14, 1),
        _record("Category:Unified table cell templates", 14, 1, ["Category:Table cell templates"]),
        _record("Category:Table cell templates", 14, 1),
        _record("Category:Specific maintenance templates", 14, 1, ["Category:Citation maintenance templates"]),
        _record("Category:Citation maintenance templates", 14, 1),
        _record("Category:Author external link templates", 14, 1),
        _record("Category:Specific presentation", 14, 1, ["Category:Text color templates"]),
        _record("Category:Text color templates", 14, 1),
        _record("Category:Internal link templates", 14, 1),
        _record("Category:Image function templates", 14, 1),
        _record("Category:Abbreviation templates", 14, 1),
        _record("Category:Templates with partial table wikitext", 14, 1),
        _record("Category:Bar chart templates", 14, 1),
        _record("Category:Chinese multilingual support templates", 14, 1),
        _record("Category:Olympics flag templates", 14, 1),
        _record("Category:Checkmark insertion templates", 14, 1, ["Category:Wikipedia-specific image insertion templates"]),
        _record("Category:Wikipedia-specific image insertion templates", 14, 1),
        _record("Category:Footnote3 templates", 14, 1, ["Category:Footnote templates"]),
        _record("Category:Footnote templates", 14, 1),
        _record("Category:Science journal citations", 14, 1, ["Category:Science citation templates"]),
        _record("Category:Science citation templates", 14, 1),
        _record("Category:Templates with no visible output", 14, 1),
        _record("Category:Periodic table format templates", 14, 1),
        _record("Category:Hatnote templates", 14, 1)])
    props.write_text("INSERT INTO `page_props` VALUES (3,'disambiguation','',NULL),(9,'other','',NULL);")
    assert build_metadata((templates,categories), dest, page_props=props) == dest
    roles = TemplateDB(dest)
    assert roles.resolve(" template:alias ") == (10, "Canonical_name")
    assert roles.resolve("old alias") == (10, "Old_alias")
    assert roles.resolve("shared alias") == (10, "Newer_target")
    assert roles.categories("alias") == {"Specific_templates"}
    assert roles.wikitext("alias") == "current"
    assert roles.wikitext("missing") is None
    assert roles.template("missing") is None
    assert roles.roles("alias") == {"hatnote"}
    assert roles.roles("direct hatnote") == {"hatnote"}
    assert roles.roles("name hatnote") == {"hatnote"}
    assert roles.roles("lua sidebar") == {"sidebar"}
    assert roles.roles("wrapped sidebar") == {"sidebar"}
    assert roles.roles("wrapped twice") == {"sidebar"}
    assert roles.roles("sidebar with extras") == {"sidebar"}
    assert roles.roles("citation with extras") == set()
    assert roles.roles("misleading sidebar category") == set()
    assert roles.roles("mixed sidebar") == set()
    assert roles.roles("verify") == {"maintenance"} and roles.roles("author link") == {"external_link"}
    assert roles.roles("styled") == {"unwrap"}
    assert roles.roles("footnote") == {"citation"} and roles.roles("science ref") == {"citation"}
    assert roles.roles("invisible") == {"maintenance"}
    assert roles.roles("cell format") == {"unwrap"}
    assert roles.roles("tooltip") == {"unwrap1"}
    assert roles.ok("generic header") and roles.ok("table row") and roles.ok("image carrier") and roles.ok("cell") and roles.ok("chem")
    assert all(roles.ok(x) for x in ("ipa-de", "UN Population", "linked", "functional", "partial table", "chart"))
    assert all(roles.ok(x) for x in ("chinese", "olympic flag", "checkmark"))
    assert roles.ok("weather data") and roles.ok("collapsed content")
    assert roles.roles("missing") == set()
    report = roles.inspect(["alias"], source_chars=None)["alias"]
    assert report["content"] == dict(body="current", parameters={}, outside_text="current", shape="mixed")
    assert next(x for x in report["rule_candidates"] if x["value"] == "Specific_templates") == dict(
        rule="category", value="Specific_templates", templates=1, examples=["Canonical_name"])
    assert next(x for x in report["rule_candidates"] if x["value"] == "Hatnote_templates") == dict(
        rule="ancestor_category", value="Hatnote_templates", templates=2, examples=["Canonical_name", "Direct_hatnote"])
    roles.close()
    db = sqlite3.connect(dest)
    assert db.execute("SELECT identifier FROM license").fetchall() == [("CC-BY-SA-4.0",)]
    assert db.execute("SELECT wikitext FROM template WHERE title='Canonical_name'").fetchone() == ("current",)
    assert db.execute("SELECT to_title FROM template_redirect WHERE title='Alias'").fetchone() == ("Canonical_name",)
    assert db.execute("SELECT category_title FROM template_category WHERE title='Canonical_name'").fetchall() == [("Specific_templates",)]
    assert db.execute("SELECT parent_title FROM category_parent WHERE title='Chronology_templates'").fetchall() == [("Sidebar_templates",)]
    assert db.execute("SELECT target_title FROM template_wrapper WHERE title='Wrapped_sidebar'").fetchone() == ("Sidebar",)
    assert db.execute("SELECT exclusive FROM template_wrapper WHERE title='Wrapped_sidebar'").fetchone() == (1,)
    assert db.execute("SELECT exclusive FROM template_wrapper WHERE title='Sidebar_with_extras'").fetchone() == (0,)
    assert db.execute("SELECT * FROM disambiguation").fetchall() == [(3,)]
    provenance = dict(db.execute("SELECT key,value FROM metadata WHERE key LIKE 'page_props_%' OR key='disambiguation_count'"))
    assert provenance["page_props_path"] == props.name and provenance["disambiguation_count"] == "1" and len(provenance["page_props_sha256"]) == 64
    db.execute("DELETE FROM template_role WHERE title IN ('Name_hatnote','Weather_data','Collapsed_content')")
    db.commit()
    db.close()
    assert refresh_roles(dest)["roles"]["hatnote"] >= 2
    refreshed = TemplateDB(dest)
    assert refreshed.roles("name hatnote") == {"hatnote"} and refreshed.ok("weather data") and refreshed.ok("collapsed content")
    refreshed.close()


def _jwt(exp, username=None):
    payload = base64.urlsafe_b64encode(json.dumps({"exp":exp, "username":username}).encode()).decode().rstrip("=")
    return f"x.{payload}.x"


def test_access_token_refreshes_expired_token_and_saves_it(tmp_path, monkeypatch):
    secrets,new = tmp_path/"secrets",_jwt(time.time()+3600)
    old = _jwt(0, "canonical-user")
    secrets.write_text(f"export OTHER=kept\nexport WIKIMEDIA_ENTERPRISE_ACCESS_TOKEN={old}\n")
    monkeypatch.setenv("WIKIMEDIA_EMAIL", "ME@EXAMPLE.COM")
    monkeypatch.setenv("WIKIMEDIA_ENTERPRISE_ACCESS_TOKEN", old)
    monkeypatch.setenv("WIKIMEDIA_ENTERPRISE_REFRESH_TOKEN", "refresh")
    def post(path, payload):
        assert payload["username"] == "canonical-user"
        return {"access_token":new}
    monkeypatch.setattr(wiki_db, "_post_json", post)
    assert wiki_db.access_token(secrets=secrets) == new
    assert secrets.read_text() == f"export OTHER=kept\nexport WIKIMEDIA_ENTERPRISE_ACCESS_TOKEN={new}\n"
    assert secrets.stat().st_mode & 0o777 == 0o600


def test_access_token_logs_in_after_rejected_refresh(tmp_path, monkeypatch):
    secrets,new = tmp_path/"secrets",_jwt(time.time()+3600)
    secrets.write_text("export WIKIMEDIA_EMAIL=me@example.com\nexport WIKIMEDIA_PASS=secret\nexport WIKIMEDIA_ENTERPRISE_REFRESH_TOKEN=old\n")
    monkeypatch.setenv("WIKIMEDIA_EMAIL", "me@example.com")
    monkeypatch.setenv("WIKIMEDIA_PASS", "secret")
    monkeypatch.setenv("WIKIMEDIA_ENTERPRISE_ACCESS_TOKEN", _jwt(0))
    monkeypatch.setenv("WIKIMEDIA_ENTERPRISE_REFRESH_TOKEN", "old")
    def rejected(*args): raise wiki_db.HTTPError("url", 401, "", {}, None)
    monkeypatch.setattr(wiki_db, "refresh_access", rejected)
    monkeypatch.setattr(wiki_db, "login", lambda email,password: {"access_token":new, "refresh_token":"new-refresh"})
    assert wiki_db.access_token(secrets=secrets) == new
    assert "WIKIMEDIA_ENTERPRISE_REFRESH_TOKEN=new-refresh" in secrets.read_text()


def test_download_redirect_drops_bearer_at_signed_host():
    request = wiki_db.Request("https://api.enterprise.wikimedia.com/download", headers={"Authorization": "Bearer secret"})
    redirected = wiki_db._DownloadRedirect().redirect_request(request, None, 302, "Found", {}, "https://signed.example/file")
    assert redirected.get_header("Authorization") is None
