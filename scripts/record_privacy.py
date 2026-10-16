"""Local disclosure checks for trace records, without printing matched text.

Pattern screening is a backstop, not a private-information detector. A review
receipt binds the caller's content review to files; it does not grant consent,
authenticate a reviewer, authorize an external service, or establish acceptance.
"""
from __future__ import annotations

import datetime as dt
import hashlib
import json
from pathlib import Path
import re
import subprocess

CONTENT_FILES = ("evidence.json", "summary.md", "coverage.json", "checks.jsonl", "decisions.jsonl")
RECEIPT = "publication.json"
PATTERNS = {
    "private-key": re.compile(r"-----BEGIN (?:[A-Z0-9 ]+ )?PRIVATE KEY-----"),
    "credential-url": re.compile(r"[a-z][a-z0-9+.-]*://[^\s/@:]+:[^\s/@]+@", re.I),
    "provider-token": re.compile(r"\b(?:gh[pousr]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{30,}|sk-(?:proj-|ant-)?[A-Za-z0-9_-]{24,}|AKIA[A-Z0-9]{16})\b"),
    "credential-assignment": re.compile(r'''(?ix)\b(?:api[_-]?key|access[_-]?token|auth[_-]?token|password|client[_-]?secret)\b["']?\s*[:=]\s*["']?([A-Za-z0-9_./+!-]{12,})'''),
    "sensitive-url-query": re.compile(r"[?&](?:access_token|api_key|apikey|token|password|signature|x-amz-signature)=[^\s&#<>\"']+", re.I),
    "bearer-credential": re.compile(r"\bBearer\s+[A-Za-z0-9_.~+/-]{20,}", re.I),
}


def safe_record_files(record):
    record = Path(record)
    if record.is_symlink():
        raise ValueError("record directory must not be a symlink")
    for name in CONTENT_FILES + (RECEIPT,):
        p = record / name
        if p.is_symlink() or (p.exists() and not p.is_file()):
            raise ValueError(f"record member must be a regular file: {name}")


def private_capture_target(record):
    """Refuse tracked or unignored capture inside Git; outside Git is caller-owned."""
    record = Path(record)
    safe_record_files(record)
    parent = record.resolve()
    while not parent.exists():
        parent = parent.parent
    result = subprocess.run(["git", "-C", str(parent), "rev-parse", "--show-toplevel"], capture_output=True, text=True)
    if result.returncode == 0:
        root = Path(result.stdout.strip()).resolve()
        for name in ("evidence.json", RECEIPT):
            target = (record.resolve() / name).relative_to(root).as_posix()
            tracked = subprocess.run(["git", "-C", str(root), "ls-files", "--error-unmatch", "--", target], capture_output=True)
            ignored = subprocess.run(["git", "-C", str(root), "check-ignore", "-q", "--", target], capture_output=True)
            if tracked.returncode == 0 or ignored.returncode != 0:
                raise ValueError("capture inside Git requires an ignored, untracked directory; use workspace/record-staging/")
    elif (result.returncode != 128 or
          any((p / ".git").exists() for p in (parent, *parent.parents))):
        raise ValueError("could not check capture destination's Git state")
    record.mkdir(parents=True, exist_ok=True, mode=0o700)


def text_findings(text):
    # Return categories only. Never include the matched value or its hash.
    return sorted(name for name, pattern in PATTERNS.items() if pattern.search(text))


def strings(value, location="$"):
    if isinstance(value, str):
        yield location, value
    elif isinstance(value, dict):
        # Numeric locations avoid echoing user-controlled keys in diagnostics.
        for i, (key, item) in enumerate(value.items()):
            yield f"{location}.key[{i}]", key
            yield from strings(item, f"{location}.value[{i}]")
    elif isinstance(value, list):
        for i, item in enumerate(value):
            yield from strings(item, f"{location}[{i}]")


def scan_record(record):
    record = Path(record)
    safe_record_files(record)
    findings = []
    for name in CONTENT_FILES + (RECEIPT,):
        path = record / name
        if not path.exists():
            continue
        text = path.read_text(encoding="utf-8")
        values = [("text", text)]
        # JSON escaping must not hide a credential in a string value or key.
        if path.suffix == ".json":
            values += list(strings(json.loads(text)))
        elif path.suffix == ".jsonl":
            for n, line in enumerate(text.splitlines(), 1):
                if line.strip():
                    values += list(strings(json.loads(line), f"line[{n}]"))
        for location, value in values:
            for kind in text_findings(value):
                findings.append({"file": name, "location": location, "category": kind})
    return findings


def require_screened(record):
    findings = scan_record(record)
    if findings:
        locations = sorted({f"{f['file']} ({f['category']})" for f in findings})
        raise ValueError("possible sensitive content; inspect locally and redact: " + ", ".join(locations))


def file_hashes(record):
    safe_record_files(record)
    return {name: hashlib.sha256((Path(record) / name).read_text(encoding="utf-8").encode("utf-8")).hexdigest()
            for name in CONTENT_FILES if (Path(record) / name).exists()}


def publication_problems(record, required=False):
    path = Path(record) / RECEIPT
    if not path.exists():
        return ["no publication review; run publication-review after inspecting all record files"] if required else []
    try:
        safe_record_files(record)
        receipt = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(receipt, dict) or receipt.get("version") != 1:
            return ["invalid publication-review receipt"]
        if any(not isinstance(receipt.get(k), str) or not receipt[k].strip() for k in ("by", "scope", "at")):
            return ["publication review needs reviewer, scope and time"]
        if receipt.get("files") != file_hashes(record):
            return ["record files changed since publication review; inspect changes and renew the receipt"]
    except (OSError, ValueError, TypeError):
        return ["unreadable publication-review receipt or record member"]
    return []


def cmd_privacy(api, args):
    findings = scan_record(Path(args.record))
    print(json.dumps({"findings": findings, "limit": "Local pattern screening only. Read all fields for confidential information, personal data and third-party permissions; a clean scan is not clearance."}, indent=2))
    return int(bool(findings))


def write_receipt(api, record, by, scope):
    """Bind a disclosure review to the record's current files, or raise ValueError.

    The core of `publication-review`, lifted so that `keep` writes the same receipt.
    """
    record = Path(record)
    safe_record_files(record)
    result = api.evaluate_record(record, check_publication=False)
    if not result["ok"]:
        raise ValueError("record is not ready: " + "; ".join(result["problems"]))
    require_screened(record)
    if any(not isinstance(v, str) or not v.strip() for v in (by, scope)):
        raise ValueError("identify the actual reviewer and intended sharing scope")
    receipt = {"version": 1, "by": by, "scope": scope,
               "at": dt.datetime.now(dt.timezone.utc).isoformat(), "files": file_hashes(record),
               "limit": "Records the caller's disclosure review, not independent acceptance, source permission or authorization to send data elsewhere."}
    if text_findings(json.dumps(receipt)):
        raise ValueError("possible sensitive content in publication-review metadata")
    (record / RECEIPT).write_text(json.dumps(receipt, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    return receipt


def cmd_publication(api, args):
    write_receipt(api, args.record, args.by, args.scope)
    print("Recorded disclosure review of the current files. Later file changes invalidate it.")
    return 0


def add_commands(sub, api, invoke):
    p = sub.add_parser("privacy-check", help="locally screen record text and metadata without printing matched values")
    p.add_argument("record")
    p.set_defaults(func=lambda args: invoke(cmd_privacy, args))
    p = sub.add_parser("publication-review", help="record your disclosure review of the current record files")
    p.add_argument("record")
    p.add_argument("--by", required=True, help="actual reviewing person or agent")
    p.add_argument("--scope", required=True, help="intended audience and sharing context inspected")
    p.set_defaults(func=lambda args: invoke(cmd_publication, args))
