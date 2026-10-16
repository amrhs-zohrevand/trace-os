#!/usr/bin/env python3
"""Shared helpers for the Zotero knowledge-base scripts.

One place for what those scripts share: the project root, the `.env` reader,
the Zotero Web API and local API calls, the Crossref and OpenAlex lookups, the
embedding call and the chat call. Each script keeps its own logic and reads its
configuration from here, so every key is read once and in one way.

Configuration comes from the project `.env`, which is gitignored; `.env.example`
is the tracked template.

    ZOTERO_USER_ID      numeric user id, shown at zotero.org/settings/keys
    ZOTERO_API_KEY      Web API key with library read and write access
    ZOTERO_LOCAL_API    optional; default http://127.0.0.1:23119/api/users/0
    CROSSREF_MAILTO     optional; an address for the Crossref and OpenAlex polite pools
    DEEPINFRA_API_KEY   optional; embeddings, model Qwen/Qwen3-Embedding-8B
    OPENROUTER_API_KEY  optional; embeddings when DeepInfra is absent, and chat synthesis
    OPENROUTER_MODEL    optional; the chat model, default qwen/qwen3.7-max
    OPENROUTER_REASONING optional; on keeps a reasoning model's reasoning, default off
    EMBED_PROVIDER      optional; deepinfra, openrouter or custom
    EMBED_URL           with EMBED_PROVIDER=custom: an OpenAI-compatible embeddings endpoint
    EMBED_API_KEY       with EMBED_PROVIDER=custom: its key
    EMBED_MODEL         optional; overrides the provider's default model

Nothing in this module writes to the library. A script that writes says so in
its help text and gates the write on --write.

Paths the scripts share:

    workspace/shared/zotero/    regenerable state: plans, the vector index (ignored by Git)
    workspace/sandbox/zotero/   reports written for a person to read
    workspace/shared/papers/    the untracked literature library, when the project keeps one;
                                its clean-corpus-manifest.csv says which clean text may be used
    paper/bib/                  the project's BibTeX, when the project keeps one
"""
from __future__ import annotations

import csv
import datetime as dt
import json
import re
import sys
import time
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SHARED = ROOT / "workspace" / "shared" / "zotero"
SANDBOX = ROOT / "workspace" / "sandbox" / "zotero"
PAPERS = ROOT / "workspace" / "shared" / "papers"
BIB_DIR = ROOT / "paper" / "bib"
CLEAN_MANIFEST = PAPERS / "clean-corpus-manifest.csv"
CLEAN_ELIGIBLE = ("GOOD", "PARTIAL")

DEFAULT_LOCAL_API = "http://127.0.0.1:23119/api/users/0"
DEFAULT_CHAT_MODEL = "qwen/qwen3.7-max"
OPENROUTER_CHAT_URL = "https://openrouter.ai/api/v1/chat/completions"
EMBED_PROVIDERS = {
    "deepinfra": ("https://api.deepinfra.com/v1/openai/embeddings", "Qwen/Qwen3-Embedding-8B", "DEEPINFRA_API_KEY"),
    "openrouter": ("https://openrouter.ai/api/v1/embeddings", "qwen/qwen3-embedding-8b", "OPENROUTER_API_KEY"),
}

# Cyrillic-to-Latin homoglyphs. A key pasted from some sources carries these,
# and they break HTTP header encoding. Repaired on load, and reported.
_HOMOGLYPHS = str.maketrans({
    "А": "A", "а": "a", "В": "B", "Е": "E", "е": "e", "К": "K", "М": "M", "Н": "H",
    "О": "O", "о": "o", "Р": "P", "р": "p", "С": "C", "с": "c", "Т": "T", "Х": "X",
    "х": "x", "У": "Y", "у": "y", "І": "I", "Ѕ": "S", "ѕ": "s",
})
_ENV_LINE = re.compile(r"""^\s*(?:export\s+)?([A-Z][A-Z0-9_]*)\s*=\s*(?:"([^"]*)"|'([^']*)'|([^#\s]*))""")


def parse_env(text: str) -> dict:
    """Read KEY=value lines. Quotes are stripped, `export` is tolerated, comments are skipped."""
    out = {}
    for line in text.splitlines():
        m = _ENV_LINE.match(line)
        if not m:
            continue
        value = next((g for g in m.groups()[1:] if g is not None), "")
        out[m.group(1)] = value.strip().translate(_HOMOGLYPHS)
    return out


def load_env(path: Path | None = None) -> dict:
    """The project `.env` as a dict, or an empty dict when there is none."""
    path = path or ROOT / ".env"
    if not path.is_file():
        return {}
    return parse_env(path.read_text(encoding="utf-8", errors="ignore"))


_ENV_CACHE: dict | None = None


def env(key: str, default: str | None = None) -> str | None:
    global _ENV_CACHE
    if _ENV_CACHE is None:
        _ENV_CACHE = load_env()
    value = _ENV_CACHE.get(key)
    return value if value not in (None, "") else default


def require(*keys: str) -> dict:
    """Return the named keys, or exit with the list of what `.env` lacks."""
    values = {k: env(k) for k in keys}
    missing = [k for k, v in values.items() if not v]
    if missing:
        raise SystemExit(
            "missing in .env: " + ", ".join(missing)
            + ". Copy .env.example to .env and fill in the values; nothing was read from the library."
        )
    return values


def this_year() -> int:
    return dt.date.today().year


def cutoff_for(years: int | None) -> str | None:
    """The dateAdded lower bound for a window of N years, or None for the whole library."""
    return None if years is None else f"{this_year() - years}-01-01"


def ensure_dir(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    return path


def rel(path: Path) -> str:
    try:
        return str(path.relative_to(ROOT))
    except ValueError:
        return str(path)


# ---- Zotero Web API --------------------------------------------------------

def zotero_base() -> str:
    return f"https://api.zotero.org/users/{require('ZOTERO_USER_ID')['ZOTERO_USER_ID']}"


def zotero_headers(extra: dict | None = None) -> dict:
    h = {"Zotero-API-Key": require("ZOTERO_API_KEY")["ZOTERO_API_KEY"], "Zotero-API-Version": "3"}
    h.update(extra or {})
    return h


def zreq(method: str, path: str, data: bytes | None = None, headers: dict | None = None,
         raw: bool = False, tries: int = 3, timeout: int = 60):
    """One Web API call. Returns (response, body), the body parsed as JSON unless raw.

    A network error or a 5xx is retried with back-off. A 4xx is raised at once:
    a version conflict or a malformed item is not transient, and retrying a
    write after it could duplicate the write.
    """
    url = path if path.startswith("http") else zotero_base() + path
    last: Exception | None = None
    for i in range(tries):
        try:
            request = urllib.request.Request(url, data=data, headers=zotero_headers(headers), method=method)
            with urllib.request.urlopen(request, timeout=timeout) as resp:
                body = resp.read()
                if raw:
                    return resp, body
                return resp, (json.loads(body) if body else None)
        except urllib.error.HTTPError as e:
            if e.code < 500:
                raise
            last = e
        except Exception as e:  # timeouts, connection resets
            last = e
        time.sleep(2 * (i + 1))
    assert last is not None
    raise last


def cloud_items(years: int | None = 5, item_type: str = "-attachment", collection: str | None = None) -> list:
    """Top-level cloud items, newest first, within the window. Each has data, version and meta."""
    cutoff = cutoff_for(years)
    prefix = f"/collections/{collection}/items/top" if collection else "/items"
    out, start = [], 0
    while True:
        query = urllib.parse.urlencode({"limit": 100, "start": start, "itemType": item_type,
                                        "format": "json", "sort": "dateAdded", "direction": "desc"})
        _, page = zreq("GET", f"{prefix}?{query}")
        if not page:
            break
        out += page
        if len(page) < 100 or (cutoff and (page[-1]["data"].get("dateAdded") or "") < cutoff):
            break
        start += 100
    return [it for it in out
            if it["data"].get("itemType") not in ("attachment", "note")
            and (not cutoff or (it["data"].get("dateAdded") or "") >= cutoff)]


def children(item_key: str) -> list:
    _, kids = zreq("GET", f"/items/{item_key}/children?limit=100")
    return kids or []


def all_collections() -> dict:
    """Every collection in the library, keyed by collection key."""
    out, start = {}, 0
    while True:
        _, page = zreq("GET", f"/collections?limit=100&start={start}")
        if not page:
            break
        for c in page:
            out[c["data"]["key"]] = c["data"]
        if len(page) < 100:
            break
        start += len(page)
    return out


def subtree(collections: dict, root: str) -> list:
    """The keys of every collection below root, depth first."""
    kids: dict = {}
    for key, data in collections.items():
        kids.setdefault(data.get("parentCollection"), []).append(key)
    out: list = []

    def walk(k):
        for c in kids.get(k, []):
            out.append(c)
            walk(c)

    walk(root)
    return out


# ---- Zotero local API (read-only) -----------------------------------------

def local_items(item_type: str = "-attachment") -> list:
    """Every item from the running Zotero client's local API. Raises OSError when it is not running."""
    base = env("ZOTERO_LOCAL_API", DEFAULT_LOCAL_API)
    items, start = [], 0
    while True:
        query = urllib.parse.urlencode({"limit": 100, "start": start, "itemType": item_type,
                                        "format": "json", "sort": "dateAdded", "direction": "desc"})
        with urllib.request.urlopen(urllib.request.Request(f"{base}/items?{query}"), timeout=40) as r:
            page = json.load(r)
        if not page:
            break
        items += page
        if len(page) < 100:
            break
        start += 100
    return items


def local_api_answers(timeout: int = 3) -> bool:
    """True when the running Zotero client's local API answers a one-item read."""
    base = env("ZOTERO_LOCAL_API", DEFAULT_LOCAL_API)
    try:
        with urllib.request.urlopen(urllib.request.Request(f"{base}/items?limit=1&format=json"), timeout=timeout) as r:
            return r.status == 200
    except (urllib.error.URLError, OSError, ValueError):
        return False


def items_for_reading(years: int | None = None, collection: str | None = None) -> tuple[list, str]:
    """Items for matching or indexing, and where they came from: "local" or "cloud".

    The running client answers in seconds where the Web API pages a large
    library in minutes, so reads that need no write guard try it first. The
    local `version` is not the cloud version, so nothing returned here is used
    in an If-Unmodified-Since-Version header; a write re-reads the cloud.
    """
    cutoff = cutoff_for(years)
    if not collection:
        try:
            raw = local_items()
        except OSError:
            raw = None
        if raw is not None:
            items = [it for it in raw if it["data"].get("itemType") not in ("attachment", "note")
                     and (not cutoff or (it["data"].get("dateAdded") or "") >= cutoff)]
            return items, "local"
    if collection:
        seen = {}
        for ck in subtree(all_collections(), collection) + [collection]:
            for it in cloud_items(None, collection=ck):
                seen.setdefault(it["data"]["key"], it)
        return list(seen.values()), "cloud"
    return cloud_items(years), "cloud"


# ---- Crossref and OpenAlex -------------------------------------------------

def mailto_param() -> str:
    address = env("CROSSREF_MAILTO")
    return f"&mailto={urllib.parse.quote(address)}" if address else ""


def get_json(url: str, headers: dict | None = None, tries: int = 3, timeout: int = 40):
    for i in range(tries):
        try:
            with urllib.request.urlopen(urllib.request.Request(url, headers=headers or {}), timeout=timeout) as r:
                return json.load(r)
        except urllib.error.HTTPError as e:
            if e.code == 404:
                return None
            if i == tries - 1:
                return None
        except Exception:
            if i == tries - 1:
                return None
        time.sleep(1.5 * (i + 1))
    return None


def strip_jats(s: str | None) -> str:
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", "", s or "")).strip()


def crossref_work(doi: str) -> dict | None:
    d = get_json(f"https://api.crossref.org/works/{urllib.parse.quote(doi)}?x=1{mailto_param()}")
    return (d or {}).get("message")


def crossref_abstract(doi: str) -> str | None:
    w = crossref_work(doi)
    a = (w or {}).get("abstract")
    return strip_jats(a) if a else None


def openalex_abstract(doi: str) -> str | None:
    d = get_json(f"https://api.openalex.org/works/doi:{urllib.parse.quote(doi)}?x=1{mailto_param()}")
    inv = (d or {}).get("abstract_inverted_index")
    if not inv:
        return None
    pos = {}
    for word, idxs in inv.items():
        for i in idxs:
            pos[i] = word
    return " ".join(pos[i] for i in sorted(pos))[:4000] or None


def match_text(s: str | None) -> str:
    """A title or a name for an exact comparison: markup and braces dropped, case folded,
    and every letter or digit kept, in any script.

    norm_title keeps a-z and 0-9 only, which is right for the cite-key index
    but reduces a title in Cyrillic, Greek or Chinese to nothing; this is used
    where two titles must be the same, not merely share their Latin letters.
    """
    folded = unicodedata.normalize("NFKC", re.sub(r"[{}\\]", "", strip_jats(s))).casefold()
    return "".join(ch for ch in folded if ch.isalnum())


def crossref_match(title: str | None, author: str | None) -> dict | None:
    """{"DOI", "title"} of the one Crossref work whose whole title is the title given, or None.

    The whole normalised titles must be equal (a Crossref subtitle may complete
    the title); a prefix or a title inside another is no match. When the first
    author's family name is given, a hit that lists authors must list it too.
    A title of fewer than three words (such as "Introduction") needs that
    author to agree, since the title alone does not name one work. An empty
    normalised title matches nothing, and hits with the same title but
    different DOIs are ambiguous, so neither is returned.
    """
    want = match_text(title)
    if not want:
        return None
    first = re.split(r"\s+and\s+|,", re.sub(r"[{}\\]", "", author or ""))[0].strip() if author else ""
    who = match_text(first)
    short = len(re.findall(r"\w+", strip_jats(title))) < 3
    if short and not who:
        return None
    q = urllib.parse.urlencode({"query.bibliographic": f"{title} {first}", "rows": 3})
    d = get_json(f"https://api.crossref.org/works?{q}{mailto_param()}")
    found = {}
    for it in (d or {}).get("message", {}).get("items", []):
        main = " ".join(it.get("title") or [])
        whole = " ".join([main] + list(it.get("subtitle") or []))
        if want not in (match_text(main), match_text(whole)) or not it.get("DOI"):
            continue
        families = [match_text(a.get("family") or a.get("name")) for a in it.get("author") or []]
        if who and (families or short) and who not in families:
            continue
        found.setdefault(norm_doi(it["DOI"]), {"DOI": it["DOI"], "title": strip_jats(main)})
    return next(iter(found.values())) if len(found) == 1 else None


def crossref_doi(title: str | None, author: str | None) -> str | None:
    """The DOI of crossref_match, or None."""
    match = crossref_match(title, author)
    return match["DOI"] if match else None


# ---- Embeddings and chat ---------------------------------------------------

def embed_provider() -> tuple[str, str, str, str] | None:
    """(name, url, model, key) for the embedding provider the `.env` selects, or None.

    Explicit EMBED_PROVIDER wins. Otherwise DeepInfra when its key is present,
    else OpenRouter, which serves the same Qwen3-Embedding-8B space, so an index
    built with one is queried with the other without recalibration. The index
    records which provider and model built it, and `zotero_ask.py` reports a mismatch.
    """
    name = (env("EMBED_PROVIDER") or "").lower()
    if name == "custom":
        url, key = env("EMBED_URL"), env("EMBED_API_KEY")
        model = env("EMBED_MODEL")
        if url and key and model:
            return "custom", url, model, key
        return None
    if not name:
        for candidate in ("deepinfra", "openrouter"):
            if env(EMBED_PROVIDERS[candidate][2]):
                name = candidate
                break
    if name not in EMBED_PROVIDERS:
        return None
    url, default_model, key_name = EMBED_PROVIDERS[name]
    key = env(key_name)
    if not key:
        return None
    return name, url, env("EMBED_MODEL", default_model), key


def embed(texts: list[str], tries: int = 4) -> list[list[float]]:
    """Embed a batch of texts. Exits with a message when no provider is configured."""
    provider = embed_provider()
    if provider is None:
        raise SystemExit("no embedding provider in .env: set DEEPINFRA_API_KEY or OPENROUTER_API_KEY, "
                         "or EMBED_PROVIDER=custom with EMBED_URL, EMBED_API_KEY and EMBED_MODEL.")
    name, url, model, key = provider
    body = json.dumps({"model": model, "input": texts}).encode()
    headers = {"Authorization": f"Bearer {key}", "Content-Type": "application/json"}
    if name == "openrouter":
        headers.update({"HTTP-Referer": "https://localhost/zotero-knowledge-base", "X-Title": "zotero-knowledge-base"})
    last: Exception | None = None
    for i in range(tries):
        try:
            with urllib.request.urlopen(urllib.request.Request(url, data=body, headers=headers), timeout=120) as r:
                data = json.load(r)["data"]
                data.sort(key=lambda d: d.get("index", 0))
                return [d["embedding"] for d in data]
        except urllib.error.HTTPError as e:
            if e.code < 500:
                raise
            last = e
        except Exception as e:
            last = e
        time.sleep(2 * (i + 1))
    assert last is not None
    raise last


def chat_model() -> str:
    return env("OPENROUTER_MODEL", DEFAULT_CHAT_MODEL) or DEFAULT_CHAT_MODEL


def llm(messages: list[dict], max_tokens: int = 1600) -> str:
    """One chat completion through OpenRouter. Exits with a message when the key is absent.

    Reasoning is switched off unless OPENROUTER_REASONING=on: these calls are
    grounded summaries, and a reasoning model given a small budget spends all
    of it thinking and returns no content at all (finish_reason "length",
    content null), which is reported here by name rather than printed as None.
    """
    key = require("OPENROUTER_API_KEY")["OPENROUTER_API_KEY"]
    payload = {"model": chat_model(), "messages": messages, "max_tokens": max_tokens}
    if (env("OPENROUTER_REASONING") or "off").lower() not in ("on", "true", "1"):
        payload["reasoning"] = {"enabled": False}
    request = urllib.request.Request(OPENROUTER_CHAT_URL, data=json.dumps(payload).encode(), headers={
        "Authorization": f"Bearer {key}", "Content-Type": "application/json",
        "HTTP-Referer": "https://localhost/zotero-knowledge-base", "X-Title": "zotero-knowledge-base"})
    with urllib.request.urlopen(request, timeout=180) as r:
        d = json.load(r)
    choice = (d.get("choices") or [{}])[0]
    content = (choice.get("message") or {}).get("content")
    if not content:
        raise SystemExit(f"{chat_model()} returned no content (finish_reason {choice.get('finish_reason')!r}); "
                         f"raise max_tokens, or set OPENROUTER_REASONING=off if the model spent the budget on reasoning")
    return content


# ---- Normalization and the project's BibTeX --------------------------------

def norm_title(t: str | None, n: int = 60) -> str:
    return re.sub(r"[^a-z0-9]", "", re.sub(r"[{}\\']", "", t or "").lower())[:n]


def norm_doi(d: str | None) -> str:
    return (d or "").strip().lower().replace("https://doi.org/", "").replace("http://doi.org/", "")



def bib_field(body: str, name: str) -> str:
    """The value of one BibTeX field, with nested braces balanced and then removed.

    `title = {A {Study} of Things}` is the common case a first-closing-brace
    regex truncates to "A Study"; protected capitals are why titles nest.

    The name is matched only where a field starts, at the body's start or
    after a comma outside every value, so `note = {see title = X}` has no
    title (AH-202).
    """
    field_start = re.compile(rf"\s*{re.escape(name)}\s*=\s*", re.I)
    start = 0
    while True:
        m = field_start.match(body, start)
        if m:
            break
        comma = _bib_entry_end(body, start, ",", entry=False)
        if comma < 0:
            return ""
        start = comma + 1
    i = m.end()
    if i >= len(body):
        return ""
    if body[i] in '{"':
        # LESSON (AH-209): `\{` counted as an opening brace, so the value ran
        # into the next field. A backslash escapes the character after it, as
        # in the entry scanner, and a quoted value ends only outside braces.
        closer, depth, j = "}" if body[i] == "{" else '"', 0, i + 1
        while j < len(body):
            ch = body[j]
            if ch == "\\":
                j += 2
                continue
            if ch == closer and depth == 0:
                break
            if ch == "{":
                depth += 1
            elif ch == "}" and depth:
                depth -= 1
            j += 1
        raw = body[i + 1:j]
    else:
        raw = re.split(r"[,\n]", body[i:], 1)[0]
    # The grouping braces go; an escaped brace is text and stays as written.
    raw = re.sub(r"\\.|[{}]", lambda t: t.group(0) if t.group(0)[0] == "\\" else "", raw, flags=re.S)
    return re.sub(r"\s+", " ", raw).strip()

_BIB_OPEN = re.compile(r"@\s*(\w+)\s*([{(])")
_BIB_DIRECTIVES = {"string", "comment", "preamble"}


def _bib_entry_end(text: str, start: int, closer: str, entry: bool = True) -> int:
    """The index of the delimiter that closes an entry whose body starts at start, or -1.

    Braces are counted, a backslash escapes the character after it, and a
    quoted value hides a closing parenthesis. An `@` at the entry's own level,
    outside any value, means a new entry began before this one closed, so
    this one is reported as unclosed rather than read into the next; a
    directive (entry=False) is skipped by its braces alone, since a
    `@comment` may hold an `@`.
    """
    depth, quoted, i = 0, False, start
    while i < len(text):
        ch = text[i]
        if ch == "\\":
            i += 2
            continue
        if ch == "{":
            depth += 1
        elif ch == "}":
            if depth == 0:
                if closer == "}":
                    return i
            else:
                depth -= 1
        elif depth == 0 and ch == '"':
            quoted = not quoted
        elif depth == 0 and not quoted and ch == closer:
            return i
        elif entry and depth == 0 and not quoted and ch == "@":
            return -1
        i += 1
    return -1


def _bib_parse(text: str, name: str) -> dict:
    """The entries of one .bib file's text; see bib_entries."""
    out, pos = {}, 0
    while True:
        m = _BIB_OPEN.search(text, pos)
        if not m:
            return out
        kind = m.group(1).lower()
        end = _bib_entry_end(text, m.end(), "}" if m.group(2) == "{" else ")", kind not in _BIB_DIRECTIVES)
        if end < 0:
            if kind not in _BIB_DIRECTIVES:
                key = re.split(r"[,\s]", text[m.end():].lstrip(), 1)[0]
                line = text.count("\n", 0, m.start()) + 1
                print(f"{name}:{line}: entry {key!r} does not close before the next entry or the end of the file; "
                      f"skipped", file=sys.stderr)
            pos = m.end()
            continue
        pos = end + 1
        if kind in _BIB_DIRECTIVES:
            continue
        key, _, body = text[m.end():end].partition(",")
        key = key.strip()
        if not key:
            continue

        def field(field_name):
            return bib_field(body, field_name)

        out[key] = {"type": kind, "doi": field("doi"), "title": field("title"),
                    "author": field("author"), "journal": field("journal"), "year": field("year"),
                    "volume": field("volume"), "number": field("number"), "pages": field("pages"),
                    "institution": field("institution"), "booktitle": field("booktitle")}


def bib_entries(bib_dir: Path | None = None) -> dict:
    """{cite_key: {doi, title, author, journal, year, ...}} from every .bib under paper/bib/, or from the one file or directory given.

    Empty when the project keeps no bibliography there; callers report that
    rather than treating it as a library with nothing in it.

    Each file is read on its own, and an entry ends at the delimiter that
    balances its opening one, not at the first line holding only a `}`: a
    one-line entry, the form doi.org returns, keeps its own fields and leaves
    the next entry whole. `@string`, `@comment` and `@preamble` are not
    entries. An entry that never closes is named on stderr and skipped, so
    its key never takes another entry's DOI.
    """
    bib_dir = bib_dir or BIB_DIR
    if bib_dir.is_file():
        files = [bib_dir]
    elif bib_dir.is_dir():
        files = sorted(bib_dir.glob("*.bib"))
    else:
        return {}
    out = {}
    for f in files:
        out.update(_bib_parse(f.read_text(encoding="utf-8", errors="ignore"), f.name))
    return out


def bib_index(bib_dir: Path | None = None) -> tuple[dict, dict]:
    """(doi -> cite key, normalized title -> cite key) over the project's BibTeX."""
    doi2k, t2k = {}, {}
    for key, e in bib_entries(bib_dir).items():
        if e["doi"]:
            doi2k[norm_doi(e["doi"])] = key
        nt = norm_title(e["title"], 50)
        if nt:
            t2k[nt] = key
    return doi2k, t2k


def clean_statuses(manifest: Path | None = None) -> dict | None:
    """{cite_key: status} from clean_corpus.py's manifest, or None when there is no manifest.

    clean_corpus.py classifies each document GOOD, PARTIAL, JUNK or MISSING.
    Only a key whose status is in CLEAN_ELIGIBLE has clean text that the
    attach and index steps may treat as the paper's text; a clean file with no
    manifest row, or with any other status, is not that.
    """
    manifest = manifest or CLEAN_MANIFEST
    if not manifest.is_file():
        return None
    with manifest.open(newline="", encoding="utf-8", errors="replace") as fh:
        return {row["key"]: (row.get("status") or "").strip().upper()
                for row in csv.DictReader(fh) if row.get("key")}


def clean_eligible(statuses: dict | None) -> set:
    """The keys whose manifest status allows their clean text to be used."""
    return {k for k, s in (statuses or {}).items() if s in CLEAN_ELIGIBLE}


def cited_keys(tex: Path | None = None) -> list:
    """Cite keys used in paper/main.tex and paper/sections/*.tex, or [] when there is no paper."""
    tex = tex or ROOT / "paper" / "main.tex"
    files = [tex] + (sorted((tex.parent / "sections").glob("*.tex")) if (tex.parent / "sections").is_dir() else [])
    keys = set()
    for f in files:
        if not f.is_file():
            continue
        for m in re.finditer(r"\\cite[a-zA-Z]*\*?(?:\[[^\]]*\])*\{([^}]+)\}", f.read_text(encoding="utf-8", errors="ignore")):
            keys.update(k.strip() for k in m.group(1).split(","))
    return sorted(keys)
