import base64, hashlib, io, json, os, re, shlex, sqlite3, subprocess, tarfile, tempfile, time
from datetime import datetime, timezone
from pathlib import Path
from urllib.error import HTTPError
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener, urlopen

from fastcore.parallel import parallel

from ._core import build_page_props as _build_page_props
from .templates import ROLE_RULES, transcluded_body, wiki_title


ROOT = Path(__file__).resolve().parents[2]
DATA = ROOT/"data"
NAMESPACES = (10,14)
TOKEN_ENV = "WIKIMEDIA_ENTERPRISE_ACCESS_TOKEN"
REFRESH_ENV = "WIKIMEDIA_ENTERPRISE_REFRESH_TOKEN"
EMAIL_ENV = "WIKIMEDIA_EMAIL"
PASS_ENV = "WIKIMEDIA_PASS"
SECRETS = Path.home()/".secrets"
AUTH_URL = "https://auth.enterprise.wikimedia.com/v1"
API_URL = "https://api.enterprise.wikimedia.com/v2"
DUMPS_URL = "https://dumps.wikimedia.org"
USER_AGENT = "wiki2dataset/0.1 (https://github.com/AnswerDotAI/wiki2dataset)"
PREFIXES = {"template": 10, "category": 14}


def snapshot_id(wiki, namespace): return f"{wiki}_namespace_{namespace}"


def wiki_dir(wiki, snapshot_dir=None): return Path(snapshot_dir) if snapshot_dir else DATA/wiki


def manifest_path(wiki, namespace, snapshot_dir=None): return wiki_dir(wiki, snapshot_dir)/f"{wiki}-namespace-{namespace}.json"


def dump_manifest_path(wiki, snapshot_dir=None): return wiki_dir(wiki, snapshot_dir)/f"{wiki}-dump.json"


def acquisition_path(wiki, snapshot_dir=None): return wiki_dir(wiki, snapshot_dir)/f"{wiki}-acquisition.json"


def _post_json(path, payload):
    data = json.dumps(payload).encode()
    request = Request(f"{AUTH_URL}/{path}", data=data, headers={"Content-Type": "application/json"})
    with urlopen(request) as response: return json.load(response)


def _snapshot_info(identifier, token):
    request = Request(f"{API_URL}/snapshots/{identifier}", data=b"", headers={"Authorization": f"Bearer {token}"})
    with urlopen(request) as response: return json.load(response)


def _get(url, json_result=False):
    with urlopen(Request(url, headers={"User-Agent":USER_AGENT})) as response:
        return json.load(response) if json_result else response.read().decode()


def login(email=None, password=None):
    "Get a new access/refresh token pair from Wikimedia Enterprise."
    email,password = email or os.environ.get(EMAIL_ENV),password or os.environ.get(PASS_ENV)
    if not email or not password: raise RuntimeError(f"set {EMAIL_ENV} and {PASS_ENV}")
    return _post_json("login", {"username": email.lower(), "password": password})


def refresh_access(username=None, refresh_token=None):
    "Get a new access token from a Wikimedia Enterprise refresh token."
    username,refresh_token = username or os.environ.get(EMAIL_ENV),refresh_token or os.environ.get(REFRESH_ENV)
    if not username or not refresh_token: raise RuntimeError(f"set {EMAIL_ENV} and {REFRESH_ENV}")
    return _post_json("token-refresh", {"username":username.lower(), "refresh_token":refresh_token})


def _jwt_payload(token):
    try:
        payload = token.split(".")[1]
        payload += "="*(-len(payload)%4)
        return json.loads(base64.urlsafe_b64decode(payload))
    except (AttributeError, IndexError, ValueError, json.JSONDecodeError): return {}


def _jwt_valid(token, leeway=60): return _jwt_payload(token).get("exp", 0) > time.time()+leeway


def save_tokens(tokens, path=SECRETS):
    "Atomically update Wikimedia Enterprise tokens in a shell secrets file."
    path = Path(path)
    values = {TOKEN_ENV: tokens.get("access_token"), REFRESH_ENV: tokens.get("refresh_token")}
    values = {key:value for key,value in values.items() if value}
    old = path.read_text() if path.exists() else ""
    pattern = re.compile(rf"^\s*(?:export\s+)?({'|'.join(map(re.escape, values))})=.*$\n?", re.M)
    text = pattern.sub("", old)
    if text and not text.endswith("\n"): text += "\n"
    text += "".join(f"export {key}={shlex.quote(value)}\n" for key,value in values.items())
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile("w", dir=path.parent, prefix=f".{path.name}.", delete=False) as f:
        tmp = Path(f.name)
        f.write(text)
    tmp.chmod(0o600)
    os.replace(tmp, path)
    return path


def read_tokens(path=SECRETS):
    "Read Wikimedia credentials written as shell exports without executing the file."
    path = Path(path)
    if not path.exists(): return {}
    names = (EMAIL_ENV, PASS_ENV, TOKEN_ENV, REFRESH_ENV)
    pattern = re.compile(rf"^\s*(?:export\s+)?({'|'.join(map(re.escape, names))})=(.*)$")
    result = {}
    for line in path.read_text().splitlines():
        match = pattern.match(line)
        if match:
            values = shlex.split(match[2], comments=True)
            if values: result[match[1]] = values[0]
    return result


def access_token(token=None, secrets=SECRETS):
    "Return a valid access token, refreshing or logging in and saving as needed."
    stored = read_tokens(secrets)
    candidates = (token, os.environ.get(TOKEN_ENV), stored.get(TOKEN_ENV))
    for candidate in candidates:
        if candidate and _jwt_valid(candidate): return candidate
    email = os.environ.get(EMAIL_ENV) or stored.get(EMAIL_ENV)
    refresh = os.environ.get(REFRESH_ENV) or stored.get(REFRESH_ENV)
    password = os.environ.get(PASS_ENV) or stored.get(PASS_ENV)
    username = next((payload["username"] for candidate in candidates if (payload := _jwt_payload(candidate)).get("username")), email)
    try: tokens = refresh_access(username, refresh) if refresh else login(email, password)
    except HTTPError as error:
        if error.code != 401 or not password: raise
        tokens = login(email, password)
    save_tokens(tokens, secrets)
    return tokens["access_token"]


class _DownloadRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        redirected = super().redirect_request(req, fp, code, msg, headers, newurl)
        if redirected and urlsplit(req.full_url).netloc != urlsplit(newurl).netloc: redirected.remove_header("Authorization")
        return redirected


def _digest(path, algorithm):
    with Path(path).open("rb") as f: return hashlib.file_digest(f, algorithm).hexdigest()


def _download(url, dest, token=None, attempts=3, digest=None, algorithm="sha1"):
    dest = Path(dest)
    if dest.exists() and (digest is None or _digest(dest, algorithm) == digest): return dest
    dest.parent.mkdir(parents=True, exist_ok=True)
    part = dest.with_suffix(dest.suffix+".part")
    for attempt in range(attempts):
        offset = part.stat().st_size if part.exists() else 0
        headers = {"User-Agent":USER_AGENT}
        if token: headers["Authorization"] = f"Bearer {token}"
        if offset: headers["Range"] = f"bytes={offset}-"
        try:
            with build_opener(_DownloadRedirect).open(Request(url, headers=headers)) as src:
                append = offset and getattr(src, "status", None) == 206
                with part.open("ab" if append else "wb") as out:
                    while chunk := src.read(2**20): out.write(chunk)
            if digest is not None and _digest(part, algorithm) != digest:
                part.unlink()
                raise ValueError(f"{algorithm} mismatch for {url}")
            os.replace(part, dest)
            return dest
        except Exception:
            if attempt+1 == attempts: raise
            time.sleep(2**attempt)


def _aria_download(url, dest, digest, label, connections=3):
    "Download one large file in parallel, then verify and atomically finalize it."
    dest = Path(dest)
    if dest.exists():
        print(f"Verifying {label} ({dest.stat().st_size/1e9:.1f} GB)...", flush=True)
        if _digest(dest, "sha1") == digest:
            print(f"Verified {label}", flush=True)
            return dest
    dest.parent.mkdir(parents=True, exist_ok=True)
    part = dest.with_suffix(dest.suffix+".part")
    print(f"Downloading {label} with {connections} connections", flush=True)
    command = ["aria2c", "--no-conf=true", "--continue=true", f"--max-connection-per-server={connections}", f"--split={connections}",
        "--min-split-size=16M", "--file-allocation=none", "--allow-overwrite=true", "--auto-file-renaming=false",
        "--summary-interval=30", "--console-log-level=warn", "--download-result=hide", "--enable-color=false", f"--user-agent={USER_AGENT}",
        f"--dir={part.parent}", f"--out={part.name}", url]
    process = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    reported,messages = 0,[]
    for line in process.stdout:
        line = line.strip()
        if line.startswith("[#"):
            now = time.monotonic()
            if now-reported >= 30:
                print(f"{label}: {line}", flush=True)
                reported = now
        elif line: messages.append(line)
    if process.wait():
        if messages: print("\n".join(messages[-20:]), flush=True)
        raise subprocess.CalledProcessError(process.returncode, command)
    print(f"Verifying {label} ({part.stat().st_size/1e9:.1f} GB)...", flush=True)
    if _digest(part, "sha1") != digest:
        part.unlink()
        raise ValueError(f"sha1 mismatch for {url}")
    os.replace(part, dest)
    print(f"Downloaded and verified {label}", flush=True)
    return dest


def _dump_plan(wiki, status, checksums):
    "Freeze the completed pages and page_props files from one Wikimedia dump."
    files = {name:info for job in status["jobs"].values() if job.get("status") == "done"
        for name,info in job.get("files", {}).items()}
    dates = {match.group(1) for name in files if (match := re.match(rf"{re.escape(wiki)}-(\d{{8}})-", name))}
    if len(dates) != 1: raise ValueError(f"completed dump has ambiguous dates: {sorted(dates)}")
    date = dates.pop()
    sums = {}
    for line in checksums.splitlines():
        if len(parts := line.split()) >= 2: sums[parts[1]] = parts[0]
    candidates = dict(pages=(f"{wiki}-{date}-pages-articles-multistream.xml.bz2", f"{wiki}-{date}-pages-articles.xml.bz2"),
        page_props=(f"{wiki}-{date}-page_props.sql.gz",))
    selected = {}
    for kind,names in candidates.items():
        name = next((name for name in names if name in files), None)
        if name is None: raise ValueError(f"completed dump has no {kind} file")
        if name not in sums: raise ValueError(f"published checksum has no {kind} file")
        selected[kind] = dict(name=name, local=name.replace(date, "latest"), bytes=int(files[name]["size"]), sha1=sums[name],
            url=f"{DUMPS_URL}/{wiki}/{date}/{name}")
    return dict(wiki=wiki, date=date, files=selected)


def wikimedia_plan(wiki="enwiki"):
    "Resolve the latest completed Wikimedia dump to immutable dated files."
    root_url = f"{DUMPS_URL}/{wiki}/"
    dates = sorted(set(re.findall(r'href=["\'](\d{8})/["\']', _get(root_url))), reverse=True)
    for date in dates:
        status_url = f"{root_url}{date}/dumpstatus.json"
        checksum_url = f"{root_url}{date}/{wiki}-{date}-sha1sums.txt"
        try: plan = _dump_plan(wiki, _get(status_url, True), _get(checksum_url))
        except ValueError: continue
        except HTTPError as error:
            if error.code == 404: continue
            raise
        return plan | {"dumpstatus_url":status_url, "checksum_url":checksum_url}
    raise RuntimeError(f"no completed Wikimedia dump found for {wiki}")


def download_wikimedia(wiki="enwiki", snapshot_dir=None, plan=None):
    "Download the frozen article and page_props dump files with published SHA-1 verification."
    root = wiki_dir(wiki, snapshot_dir)
    plan = plan or wikimedia_plan(wiki)
    if plan["wiki"] != wiki: raise ValueError(f"plan is for {plan['wiki']}, not {wiki}")
    paths = {kind:_aria_download(item["url"], root/item["local"], item["sha1"], f"{wiki} {kind}")
        for kind,item in plan["files"].items()}
    _write_manifest(plan, dump_manifest_path(wiki, root))
    return paths


def _download_chunk(job):
    identifier,root,token,chunk = job
    dest = root/f"{chunk}.tar.gz"
    if dest.exists(): return dest
    return _download(f"{API_URL}/snapshots/{identifier}/chunks/{chunk}/download", dest, token)


def _write_manifest(info, path):
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile("w", dir=path.parent, prefix=f".{path.name}.", delete=False) as f:
        tmp = Path(f.name)
        json.dump(info, f, indent=2)
        f.write("\n")
    os.replace(tmp, path)


def _snapshot_plan(wiki, namespace, snapshot_dir, token, info=None):
    identifier = snapshot_id(wiki, namespace)
    info = info or _snapshot_info(identifier, token)
    chunks,version = info["chunks"],info["version"]
    root = Path(snapshot_dir)/f"{wiki}-namespace-{namespace}-{version}"
    root.mkdir(parents=True, exist_ok=True)
    size = info.get("size") or {}
    print(f"Downloading {identifier}: {len(chunks)} chunks, {size.get('value', '?')} {size.get('unit_text', '')}", flush=True)
    return namespace,info,[(identifier,root,token,chunk) for chunk in chunks]


def enterprise_plan(wiki="enwiki", token=None):
    "Freeze the current Template and Category namespace snapshot identifiers."
    token = access_token(token)
    return {str(namespace):_snapshot_info(snapshot_id(wiki, namespace), token) for namespace in NAMESPACES}


def acquisition_plan(wiki="enwiki", token=None):
    "Freeze all Wikimedia sources used by one dataset build."
    token = access_token(token)
    return dict(wiki=wiki, planned_at=datetime.now(timezone.utc).isoformat(), dump=wikimedia_plan(wiki),
        enterprise=enterprise_plan(wiki, token))


def download_snapshots(wiki="enwiki", snapshot_dir=None, token=None, workers=16, snapshots=None):
    "Download Template and Category namespace snapshots in parallel chunks."
    snapshot_dir = wiki_dir(wiki, snapshot_dir)
    token = access_token(token)
    snapshots = snapshots or enterprise_plan(wiki, token)
    plans = [_snapshot_plan(wiki, namespace, snapshot_dir, token, snapshots[str(namespace)]) for namespace in NAMESPACES]
    paths = parallel(_download_chunk, [job for _,_,jobs in plans for job in jobs], n_workers=workers, threadpool=True)
    changed = [namespace for namespace,info,_ in plans
        if _snapshot_info(snapshot_id(wiki, namespace), token)["version"] != info["version"]]
    if changed: raise RuntimeError(f"Enterprise snapshot changed during download: namespaces {changed}")
    for namespace,info,_ in plans: _write_manifest(info, manifest_path(wiki, namespace, snapshot_dir))
    return list(paths)


def acquire_sources(wiki="enwiki", snapshot_dir=None, token=None, workers=16, plan=None):
    "Freeze and download every external source needed for one dataset build."
    root = wiki_dir(wiki, snapshot_dir)
    token = access_token(token)
    plan = plan or acquisition_plan(wiki, token)
    if plan["wiki"] != wiki: raise ValueError(f"plan is for {plan['wiki']}, not {wiki}")
    _write_manifest(plan, acquisition_path(wiki, root))
    dumps = download_wikimedia(wiki, root, plan["dump"])
    snapshots = download_snapshots(wiki, root, token, workers, plan["enterprise"])
    plan["completed_at"] = datetime.now(timezone.utc).isoformat()
    _write_manifest(plan, acquisition_path(wiki, root))
    return dict(dump=dumps, snapshots=snapshots, manifest=acquisition_path(wiki, root))


def snapshot_sources(wiki, namespace, snapshot_dir=None):
    snapshot_dir = wiki_dir(wiki, snapshot_dir)
    path = manifest_path(wiki, namespace, snapshot_dir)
    if not path.exists(): raise FileNotFoundError(path)
    info = json.loads(path.read_text())
    root = Path(snapshot_dir)/f"{wiki}-namespace-{namespace}-{info['version']}"
    paths = [root/f"{chunk}.tar.gz" for chunk in info["chunks"]]
    missing = [str(x) for x in paths if not x.exists()]
    if missing: raise FileNotFoundError(", ".join(missing))
    return paths


def source_manifests(wiki, snapshot_dir=None):
    "Read the frozen Wikimedia and Enterprise source descriptions available locally."
    root = wiki_dir(wiki, snapshot_dir)
    result = {}
    dump = dump_manifest_path(wiki, root)
    if dump.exists(): result["wikimedia_dump"] = json.loads(dump.read_text())
    snapshots = {str(namespace):json.loads(manifest_path(wiki, namespace, root).read_text()) for namespace in NAMESPACES}
    result["enterprise_snapshots"] = {namespace:{key:info.get(key) for key in ("identifier", "version", "date_modified")}
        for namespace,info in snapshots.items()}
    return result


def records(path):
    "Yield records from an Enterprise NDJSON tarball."
    with tarfile.open(path, "r:gz") as archive:
        for member in archive:
            if not member.isfile(): continue
            src = archive.extractfile(member)
            if src is None: continue
            with src, io.TextIOWrapper(src, encoding="utf-8") as text:
                for line in text:
                    if line.strip(): yield json.loads(line)


def _ref(name, default_namespace):
    prefix,sep,title = name.partition(":")
    namespace = PREFIXES.get(prefix.casefold()) if sep else None
    if namespace is None: namespace,title = default_namespace,name
    return namespace,wiki_title(title, namespace)


def _version(record):
    version = record.get("version") or {}
    return int(version.get("identifier") or 0)


def _wrapper(wikitext):
    "Return the leading target and whether it is the sole top-level template call."
    text = transcluded_body(wikitext)
    calls,i = [],0
    while i < len(text):
        while i < len(text) and text[i].isspace(): i += 1
        if i == len(text): break
        match = re.match(r"\{\{\s*([^|{}\n]+)", text[i:])
        if not match: return None
        calls.append(match.group(1).strip())
        stack = []
        while i < len(text):
            if text.startswith("{{{", i):
                stack.append(3)
                i += 3
            elif text.startswith("{{", i):
                stack.append(2)
                i += 2
            elif stack and text.startswith("}"*stack[-1], i): i += stack.pop()
            else: i += 1
            if not stack: break
    return (calls[0], len(calls) == 1) if calls else None


def _stage_schema(db):
    db.executescript("""
PRAGMA foreign_keys=ON;
PRAGMA journal_mode=OFF;
PRAGMA synchronous=OFF;
PRAGMA temp_store=FILE;
CREATE TABLE metadata (key TEXT PRIMARY KEY, value TEXT NOT NULL) WITHOUT ROWID;
CREATE TEMP TABLE raw_page (namespace INTEGER, title TEXT, version INTEGER, wikitext TEXT);
CREATE TEMP TABLE raw_wrapper (title TEXT, version INTEGER, target_title TEXT, exclusive INTEGER);
CREATE TEMP TABLE raw_role (title TEXT, version INTEGER, role TEXT);
CREATE TEMP TABLE raw_redirect (
    target_namespace INTEGER, target_title TEXT, target_version INTEGER,
    namespace INTEGER, title TEXT, to_namespace INTEGER, to_title TEXT
);
CREATE TEMP TABLE raw_categorylink (namespace INTEGER, title TEXT, version INTEGER, category_title TEXT);
CREATE TEMP TABLE raw_license (identifier TEXT, name TEXT, url TEXT);
""")


def _materialize_role_rules(db):
    "Materialize declarative page/category role rules."
    def insert(role, select, params=()):
        db.execute(f"INSERT OR IGNORE INTO template_role SELECT DISTINCT title,? FROM ({select})", (role,*params))
    for role,rule in ROLE_RULES.items():
        pages = tuple(wiki_title(x, 10) for x in rule.get("pages", ()))
        if pages:
            placeholders = ",".join("?"*len(pages))
            insert(role, f"SELECT title FROM template WHERE title IN ({placeholders})", pages)
        for prefix in rule.get("page_prefixes", ()):
            insert(role, "SELECT title FROM template WHERE lower(title) GLOB ?", (wiki_title(prefix, 10).casefold()+"*",))
        for suffix in rule.get("page_suffixes", ()):
            insert(role, "SELECT title FROM template WHERE lower(title) GLOB ?", ("*"+suffix.casefold(),))
        for target in rule.get("wrapper_targets", ()):
            insert(role, "SELECT title FROM template_wrapper WHERE lower(target_title)=?", (target.casefold(),))
    category_titles = tuple(x[0] for x in db.execute("SELECT DISTINCT category_title FROM categorylink"))
    direct,ancestor = set(),set()
    for role,rule in ROLE_RULES.items():
        direct.update((wiki_title(x, 14),role) for x in rule.get("categories", ()))
        ancestor.update((wiki_title(x, 14),role) for x in rule.get("ancestor_categories", rule.get("categories", ())))
        direct_suffixes = tuple(x.casefold() for x in rule.get("category_suffixes", ()))
        ancestor_suffixes = tuple(x.casefold() for x in rule.get("ancestor_suffixes", ()))
        for title in category_titles:
            folded = title.casefold()
            if direct_suffixes and folded.endswith(direct_suffixes): direct.add((title,role))
            if ancestor_suffixes and folded.endswith(ancestor_suffixes): ancestor.add((title,role))
    db.execute("CREATE TEMP TABLE role_direct_category(category_title TEXT,role TEXT,PRIMARY KEY(category_title,role)) WITHOUT ROWID")
    db.execute("CREATE TEMP TABLE role_ancestor_category(category_title TEXT,role TEXT,PRIMARY KEY(category_title,role)) WITHOUT ROWID")
    db.executemany("INSERT INTO role_direct_category VALUES (?,?)", direct)
    db.executemany("INSERT INTO role_ancestor_category VALUES (?,?)", ancestor)
    db.execute("""INSERT OR IGNORE INTO template_role SELECT c.title,r.role FROM categorylink c
        JOIN role_direct_category r ON r.category_title=c.category_title WHERE c.namespace=10""")
    db.execute("""INSERT OR IGNORE INTO template_role SELECT c.title,r.role FROM categorylink c JOIN categorylink p
        ON p.namespace=14 AND p.title=c.category_title JOIN role_ancestor_category r ON r.category_title=p.category_title
        WHERE c.namespace=10""")


def _body_roles(wikitext):
    source = transcluded_body(wikitext)
    return tuple(role for role,rule in ROLE_RULES.items() if source.casefold().startswith(tuple(rule.get("body_prefixes", ()))))


def _materialize_wrapper_graph(db):
    "Resolve template redirects once so role refreshes can traverse wrappers in SQL."
    redirects = {(namespace,title): (to_namespace,to_title) for namespace,title,to_namespace,to_title in db.execute("SELECT * FROM redirect")}
    def resolve(title):
        current,seen = (10,title),set()
        while current not in seen and current in redirects:
            seen.add(current)
            current = redirects[current]
        return current
    rows = []
    for title,target,exclusive in db.execute("SELECT title,target_title,exclusive FROM template_wrapper"):
        namespace,target = resolve(target)
        if namespace == 10: rows.append((title,target,exclusive))
    db.executemany("INSERT INTO template_role_edge VALUES (?,?,?)", rows)


def _propagate_wrapper_roles(db):
    "Propagate non-OK roles through the cached resolved wrapper graph."
    before = db.total_changes
    for role,rule in ROLE_RULES.items():
        if role == "ok": continue
        db.execute("""WITH RECURSIVE inherited(title) AS (
            SELECT title FROM template_role WHERE role=?
            UNION
            SELECT e.title FROM template_role_edge e JOIN inherited i ON e.target_title=i.title
            WHERE e.exclusive OR ?)
            INSERT OR IGNORE INTO template_role SELECT title,? FROM inherited""", (role, bool(rule.get("leading_wrapper")), role))
    return db.total_changes-before


def _finalize(db):
    db.executescript("""
CREATE TABLE page (
    namespace INTEGER NOT NULL CHECK(namespace IN (10,14)),
    title TEXT NOT NULL,
    version INTEGER NOT NULL,
    wikitext TEXT,
    PRIMARY KEY(namespace,title)
) WITHOUT ROWID;
CREATE TABLE redirect (
    namespace INTEGER NOT NULL CHECK(namespace IN (10,14)),
    title TEXT NOT NULL,
    to_namespace INTEGER NOT NULL CHECK(to_namespace IN (10,14)),
    to_title TEXT NOT NULL,
    PRIMARY KEY(namespace,title),
    FOREIGN KEY(to_namespace,to_title) REFERENCES page(namespace,title) DEFERRABLE INITIALLY DEFERRED
) WITHOUT ROWID;
CREATE TABLE categorylink (
    namespace INTEGER NOT NULL CHECK(namespace IN (10,14)),
    title TEXT NOT NULL,
    category_title TEXT NOT NULL,
    PRIMARY KEY(namespace,title,category_title),
    FOREIGN KEY(namespace,title) REFERENCES page(namespace,title) ON DELETE CASCADE
) WITHOUT ROWID;
CREATE TABLE license (
    identifier TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    url TEXT NOT NULL
) WITHOUT ROWID;
CREATE TABLE template_role (
    title TEXT NOT NULL,
    role TEXT NOT NULL,
    PRIMARY KEY(title,role)
) WITHOUT ROWID;
CREATE TABLE template_body_role (
    title TEXT NOT NULL,
    role TEXT NOT NULL,
    PRIMARY KEY(title,role)
) WITHOUT ROWID;
CREATE TABLE template_wrapper (
    title TEXT PRIMARY KEY,
    target_title TEXT NOT NULL,
    exclusive INTEGER NOT NULL CHECK(exclusive IN (0,1))
) WITHOUT ROWID;
CREATE TABLE template_role_edge (
    title TEXT PRIMARY KEY,
    target_title TEXT NOT NULL,
    exclusive INTEGER NOT NULL CHECK(exclusive IN (0,1))
) WITHOUT ROWID;
INSERT INTO page
    SELECT namespace,title,max(version),wikitext FROM raw_page GROUP BY namespace,title ORDER BY namespace,title;
INSERT OR IGNORE INTO redirect
    SELECT r.namespace,r.title,r.to_namespace,r.to_title
    FROM raw_redirect r JOIN page p
      ON p.namespace=r.target_namespace AND p.title=r.target_title AND p.version=r.target_version
    ORDER BY r.target_version DESC,r.to_namespace,r.to_title;
INSERT INTO categorylink
    SELECT DISTINCT r.namespace,r.title,r.category_title
    FROM raw_categorylink r JOIN page p
      ON p.namespace=r.namespace AND p.title=r.title AND p.version=r.version
    ORDER BY r.namespace,r.title,r.category_title;
INSERT INTO license SELECT identifier,min(name),min(url) FROM raw_license GROUP BY identifier;
INSERT OR IGNORE INTO template_wrapper
    SELECT r.title,r.target_title,r.exclusive FROM raw_wrapper r JOIN page p
      ON p.namespace=10 AND p.title=r.title AND p.version=r.version;
CREATE INDEX categorylink_category ON categorylink(category_title);
CREATE INDEX template_role_edge_target ON template_role_edge(target_title);
CREATE VIEW template AS SELECT title,version,wikitext FROM page WHERE namespace=10;
CREATE VIEW category AS SELECT title,version FROM page WHERE namespace=14;
CREATE VIEW template_redirect AS
    SELECT title,to_title FROM redirect WHERE namespace=10 AND to_namespace=10;
CREATE VIEW template_category AS
    SELECT title,category_title FROM categorylink WHERE namespace=10;
CREATE VIEW category_parent AS
    SELECT title,category_title AS parent_title FROM categorylink WHERE namespace=14;
DROP TABLE raw_page;
DROP TABLE raw_redirect;
DROP TABLE raw_categorylink;
DROP TABLE raw_license;
""")
    _materialize_wrapper_graph(db)
    print("Materializing declarative template roles", flush=True)
    _materialize_role_rules(db)
    db.execute("""INSERT OR IGNORE INTO template_body_role SELECT r.title,r.role FROM raw_role r JOIN page p
        ON p.namespace=10 AND p.title=r.title AND p.version=r.version""")
    db.execute("INSERT OR IGNORE INTO template_role SELECT * FROM template_body_role")
    print("Propagating wrapper roles", flush=True)
    additions = _propagate_wrapper_roles(db)
    db.execute("CREATE INDEX template_role_role ON template_role(role)")
    print(f"Materialized {additions:,} inherited wrapper roles", flush=True)
    db.executescript("DROP TABLE raw_wrapper; DROP TABLE raw_role;")


def _add_record(db, record):
    namespace = int(record["namespace"]["identifier"])
    if namespace not in NAMESPACES: return False
    namespace,title = _ref(record["name"], namespace)
    version = _version(record)
    body = record.get("article_body") or {}
    wikitext = body.get("wikitext") if namespace == 10 else None
    db.execute("INSERT INTO raw_page VALUES (?,?,?,?)", (namespace,title,version,wikitext))
    if namespace == 10:
        if wrapper := _wrapper(wikitext):
            target,exclusive = wrapper
            target_ns,target_title = _ref(target, 10)
            if target_ns == 10: db.execute("INSERT INTO raw_wrapper VALUES (?,?,?,?)", (title,version,target_title,exclusive))
        for role in _body_roles(wikitext): db.execute("INSERT INTO raw_role VALUES (?,?,?)", (title,version,role))
    categories = {_ref(x["name"], 14)[1] for x in record.get("categories") or []}
    db.executemany("INSERT INTO raw_categorylink VALUES (?,?,?,?)", ((namespace,title,version,x) for x in categories))
    redirects = {_ref(x["name"], namespace) for x in record.get("redirects") or []}
    db.executemany("INSERT INTO raw_redirect VALUES (?,?,?,?,?,?,?)",
        ((namespace,title,version,from_ns,from_title,namespace,title) for from_ns,from_title in redirects))
    for item in record.get("license") or []:
        db.execute("INSERT INTO raw_license VALUES (?,?,?)", (item["identifier"],item["name"],item["url"]))
    return True


def cache_page_props(source, metadata):
    "Store the disambiguation page IDs and source provenance in wiki metadata."
    source,metadata = Path(source),Path(metadata)
    with source.open("rb") as f: digest = hashlib.file_digest(f, "sha256").hexdigest()
    count = _build_page_props(source, metadata)
    with sqlite3.connect(metadata) as db:
        db.execute("CREATE TABLE IF NOT EXISTS metadata (key TEXT PRIMARY KEY, value TEXT NOT NULL) WITHOUT ROWID")
        db.executemany("INSERT OR REPLACE INTO metadata VALUES (?,?)", (
            ("page_props_path", source.name), ("page_props_sha256", digest), ("disambiguation_count", str(count))))
    return count


def build_metadata(sources, dest, wiki="enwiki", limit=None, page_props=None, source_info=None):
    "Build the redirect and category graph directly from namespace snapshots."
    dest,sources = Path(dest),tuple(map(Path, sources))
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = Path(tempfile.NamedTemporaryFile(prefix=f".{dest.name}.", dir=dest.parent, delete=False).name)
    db = None
    try:
        db = sqlite3.connect(tmp)
        _stage_schema(db)
        db.execute("INSERT INTO metadata VALUES ('wiki',?)", (wiki,))
        db.executemany("INSERT INTO metadata VALUES (?,?)", ((key,json.dumps(value, sort_keys=True)) for key,value in (source_info or {}).items()))
        seen = {}
        for source in sources:
            print(f"Reading {source.name}", flush=True)
            for record in records(source):
                namespace = int(record["namespace"]["identifier"])
                if limit is not None and seen.get(namespace, 0) >= limit: break
                if _add_record(db, record): seen[namespace] = seen.get(namespace, 0)+1
        print("Materializing final tables and indexes", flush=True)
        _finalize(db)
        assert not db.execute("PRAGMA foreign_key_check").fetchall()
        assert db.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        counts = {table: db.execute(f"SELECT count(*) FROM {table}").fetchone()[0]
            for table in ("page", "redirect", "categorylink", "template_role", "template_wrapper")}
        db.commit()
        db.close()
        db = None
        if page_props is not None: counts["disambiguation"] = cache_page_props(page_props, tmp)
        os.replace(tmp, dest)
    except BaseException:
        if db is not None: db.close()
        tmp.unlink(missing_ok=True)
        raise
    print(f"Built {dest}: " + ", ".join(f"{key}={value:,}" for key,value in counts.items()), flush=True)
    return dest


def refresh_metadata(wiki="enwiki", snapshot_dir=None, dest=None, download=False, token=None, workers=16, limit=None, page_props=None):
    "Optionally download namespace snapshots, then rebuild the SQLite role graph."
    snapshot_dir = wiki_dir(wiki, snapshot_dir)
    if download: sources = download_snapshots(wiki, snapshot_dir, token, workers)
    else: sources = [path for namespace in NAMESPACES for path in snapshot_sources(wiki, namespace, snapshot_dir)]
    return build_metadata(sources, dest or Path(snapshot_dir)/f"{wiki}-templates.sqlite", wiki, limit, page_props, source_manifests(wiki, snapshot_dir))


def refresh_roles(metadata):
    "Rematerialize cleanup roles from an existing wiki metadata database."
    with sqlite3.connect(metadata) as db:
        cached = db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='template_body_role'").fetchone()
        if not cached:
            db.execute("CREATE TABLE template_body_role(title TEXT NOT NULL,role TEXT NOT NULL,PRIMARY KEY(title,role)) WITHOUT ROWID")
            body_roles = tuple(role for role,rule in ROLE_RULES.items() if rule.get("body_prefixes"))
            placeholders = ",".join("?"*len(body_roles))
            rows = ((title,role) for title,wikitext in db.execute(f"""SELECT r.title,t.wikitext FROM template_role r
                JOIN template t ON t.title=r.title WHERE r.role IN ({placeholders})""", body_roles) for role in _body_roles(wikitext))
            db.executemany("INSERT OR IGNORE INTO template_body_role VALUES (?,?)", rows)
        cached = db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='template_role_edge'").fetchone()
        if not cached:
            db.execute("""CREATE TABLE template_role_edge (
                title TEXT PRIMARY KEY,target_title TEXT NOT NULL,exclusive INTEGER NOT NULL CHECK(exclusive IN (0,1))) WITHOUT ROWID""")
            db.execute("CREATE INDEX template_role_edge_target ON template_role_edge(target_title)")
            _materialize_wrapper_graph(db)
        db.execute("DROP INDEX IF EXISTS template_role_role")
        db.execute("DELETE FROM template_role")
        _materialize_role_rules(db)
        db.execute("INSERT OR IGNORE INTO template_role SELECT * FROM template_body_role")
        inherited = _propagate_wrapper_roles(db)
        db.execute("CREATE INDEX template_role_role ON template_role(role)")
        roles = dict(db.execute("SELECT role,count(*) FROM template_role GROUP BY role"))
    return dict(roles=roles, inherited=inherited)
