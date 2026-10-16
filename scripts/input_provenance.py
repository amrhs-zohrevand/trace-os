"""Evidence-linked annotations of what input passages do and their stated origin.

No model, network, clipboard access or origin detector. The caller supplies the
interpretation; validators bind it to retained text, not to historical truth.
"""
from __future__ import annotations

import datetime as dt
import hashlib
import json
from pathlib import Path

ROLES = {"instruction", "idea", "material", "unknown"}
ORIGINS = {"speaker", "external", "ai", "unknown"}
BASES = {"explicit", "inferred", "unknown"}


def text_hash(text):
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def annotation_problems(evidence):
    problems = []
    ids = {t["id"] for t in evidence["turns"]}
    for turn in evidence["turns"]:
        if "annotation" not in turn:
            continue
        a = turn["annotation"]
        prefix = f"{turn['id']} annotation"
        if not isinstance(a, dict) or a.get("version") != 1:
            problems.append(prefix + ": unsupported annotation")
            continue
        if a.get("text_sha256") != text_hash(turn["text"]):
            problems.append(prefix + ": retained text changed; re-annotate after redaction")
        if not isinstance(a.get("by"), str) or not a["by"].strip():
            problems.append(prefix + ": identify the annotator")
        segments = a.get("segments")
        if not isinstance(segments, list) or not segments:
            problems.append(prefix + ": segments must be a nonempty list")
            continue
        end = 0
        for segment in segments:
            if not isinstance(segment, dict):
                problems.append(prefix + ": segment must be an object")
                continue
            start, stop = segment.get("start"), segment.get("end")
            if (type(start) is not int or type(stop) is not int or
                    not 0 <= start < stop <= len(turn["text"])):
                problems.append(prefix + ": invalid character offsets")
            else:
                if start < end:
                    problems.append(prefix + ": overlapping or unordered segments")
                end = stop
            if segment.get("role") not in ROLES or segment.get("origin") not in ORIGINS:
                problems.append(prefix + ": unknown role or origin")
            basis = segment.get("basis")
            refs = segment.get("basis_refs")
            if basis not in BASES:
                problems.append(prefix + ": unknown attribution basis")
            if (not isinstance(refs, list) or
                    any(not isinstance(r, str) or r not in ids for r in refs)):
                problems.append(prefix + ": basis_refs must name captured turns")
            elif basis in {"explicit", "inferred"} and not refs:
                problems.append(prefix + ": stated or inferred attribution needs basis_refs")
            if segment.get("origin") != "unknown" and basis == "unknown":
                problems.append(prefix + ": an unstated origin must remain unknown")
            if segment.get("origin") == "speaker" and basis != "explicit":
                problems.append(prefix + ": speaker origin requires an explicit self-attribution")
            if not isinstance(segment.get("source", ""), str):
                problems.append(prefix + ": source must be a short reference or label")
            allowed = {"start", "end", "role", "origin", "basis", "basis_refs", "source"}
            if set(segment) - allowed:
                problems.append(prefix + ": unsupported segment fields")
    return problems


def cmd_annotate(api, args):
    record = Path(args.record)
    from record_privacy import private_capture_target
    private_capture_target(record)
    evidence = api.load_evidence(record)
    packet = json.loads(Path(args.input).read_text(encoding="utf-8"))
    if not isinstance(packet, dict) or set(packet) - {"turn", "text_sha256", "segments"}:
        raise ValueError("annotation packet needs turn, text_sha256 and segments")
    turn = next((t for t in evidence["turns"] if t["id"] == packet.get("turn")), None)
    if turn is None or packet.get("text_sha256") != text_hash(turn["text"]):
        raise ValueError("annotation input does not match the retained turn and text hash")
    if not isinstance(packet.get("segments"), list):
        raise ValueError("segments must be a list")
    segments = []
    for supplied in packet["segments"]:
        if not isinstance(supplied, dict):
            raise ValueError("each segment must be an object")
        s = dict(supplied)
        quote = s.pop("quote", None)
        if quote is not None:
            if not isinstance(quote, str) or not quote:
                raise ValueError("quote must be nonempty text from the retained turn")
            if "start" not in s and "end" not in s:
                # Refuse ambiguous anchors, including overlapping matches.
                positions = [i for i in range(len(turn["text"])) if turn["text"].startswith(quote, i)]
                if len(positions) != 1:
                    raise ValueError("quote is absent or ambiguous; supply exact start/end offsets")
                s.update(start=positions[0], end=positions[0] + len(quote))
            if (type(s.get("start")) is not int or type(s.get("end")) is not int or
                    turn["text"][s["start"]:s["end"]] != quote):
                raise ValueError("quote and character offsets disagree")
        s.setdefault("origin", "unknown")
        s.setdefault("basis", "unknown")
        s.setdefault("basis_refs", [])
        segments.append(s)
    if not isinstance(args.by, str) or not args.by.strip():
        raise ValueError("identify the actual annotator")
    turn["annotation"] = {"version": 1, "text_sha256": text_hash(turn["text"]),
                          "by": args.by, "at": dt.datetime.now(dt.timezone.utc).isoformat(),
                          "segments": segments}
    problems = annotation_problems(evidence)
    if problems:
        raise ValueError("; ".join(problems))
    # An annotation can itself contain private attribution/source metadata.
    evidence.setdefault("consent", {})["third_party_review"] = "PENDING"
    from record_privacy import safe_record_files
    safe_record_files(record)
    (record / "evidence.json").write_text(json.dumps(evidence, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    print(f"Annotated {turn['id']}: {len(segments)} passage(s); original text unchanged.")
    print("Review annotation metadata for disclosure, clear consent, and refresh coverage/publication review.")
    return 0


def input_account(evidence):
    """A bounded, quote-free account; no raw text or annotator-supplied source URLs."""
    annotated = []
    unannotated = 0
    for turn in evidence["turns"]:
        a = turn.get("annotation")
        if not a:
            unannotated += 1
            continue
        covered = sum(s["end"] - s["start"] for s in a["segments"])
        annotated.append({"turn": turn["id"], "speaker": turn.get("speaker", "unknown"),
                          "speaker_kind": turn.get("speaker_kind", "unknown"), "by": a["by"],
                          "unclassified_characters": len(turn["text"]) - covered,
                          "segments": [{k: s[k] for k in ("start", "end", "role", "origin", "basis", "basis_refs")}
                                       for s in a["segments"]]})
    return {"annotated_turns": annotated, "unannotated_turns": unannotated,
            "limit": "Roles and origins are recorded interpretations. Explicit means stated in the cited input, not proven origination. Unannotated passages have unknown role/origin; no credit percentages are inferred."}


def input_lines(account):
    lines = ["Input passages (recorded attribution):", account["limit"],
             f"Unannotated turns: {account['unannotated_turns']}."]
    for turn in account["annotated_turns"]:
        lines.append(f"{turn['turn']} submitted by {turn['speaker']} ({turn['speaker_kind']}); annotated by {turn['by']}.")
        for s in turn["segments"]:
            refs = ", ".join(s["basis_refs"]) or "none stated"
            lines.append(f"{turn['turn']}[{s['start']}:{s['end']}]: {s['role']}; origin {s['origin']}; "
                         f"basis {s['basis']} ({refs}).")
        if turn["unclassified_characters"]:
            lines.append(f"{turn['turn']}: remaining text unclassified.")
    return lines


def add_commands(sub, api, invoke):
    p = sub.add_parser("annotate-input", help="annotate input passages without changing their words or guessing origin")
    p.add_argument("record")
    p.add_argument("--input", required=True, help="JSON following input-annotation.example.json")
    p.add_argument("--by", required=True, help="actual annotating human or agent; not the source author")
    p.set_defaults(func=lambda args: invoke(cmd_annotate, args))
