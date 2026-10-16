#!/usr/bin/env python3
"""Ask a question of the Zotero library: retrieve by meaning, then synthesize with citations.

The question is embedded with the provider that built the index, the closest
items are retrieved from workspace/shared/zotero/index/ (built by
zotero_index.py), and a chat model writes a short synthesis grounded only in
the retrieved abstracts, saying where the library has no evidence. Read-only.

--list prints the ranked hits and makes no chat call, which is the right mode
when an agent will read the retrieved set itself.

Needs numpy. The chat call uses OPENROUTER_API_KEY and OPENROUTER_MODEL from
.env; the embedding call uses the provider .env selects. If that provider or
model differs from the one recorded in the index, the mismatch is reported
and the query stops, because two spaces cannot be compared.

Usage:
  python3 scripts/zotero_ask.py "what is the evidence on X?" [-k 12]
  python3 scripts/zotero_ask.py "what is the evidence on X?" --list
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import zotero_common as Z  # noqa: E402
import zotero_index as I  # noqa: E402


def load_index():
    """(matrix, meta, info), or exits with the reason there is none."""
    try:
        import numpy as np
    except ImportError:
        raise SystemExit("numpy is not installed; retrieval needs it (pip install numpy)")
    if not I.VECTORS.is_file() or not I.META.is_file():
        raise SystemExit(f"no index under {Z.rel(I.INDEX_DIR)}; run: python3 scripts/zotero_index.py")
    info = json.loads(I.INFO.read_text(encoding="utf-8")) if I.INFO.is_file() else {}
    provider = Z.embed_provider()
    if provider is None:
        raise SystemExit("no embedding provider in .env; the index was built with "
                         f"{info.get('provider', 'an unrecorded provider')}:{info.get('model', '?')}")
    if info and (info.get("model") or "").lower().split("/")[-1] != provider[2].lower().split("/")[-1]:
        raise SystemExit(f"the index was built with {info['provider']}:{info['model']} and .env now selects "
                         f"{provider[0]}:{provider[2]}; rebuild the index or set EMBED_MODEL to match")
    return np.load(I.VECTORS), json.loads(I.META.read_text(encoding="utf-8")), info


def retrieve(question: str, k: int) -> list[tuple[float, dict]]:
    import numpy as np
    mat, meta, _ = load_index()
    q = np.array(Z.embed([question])[0], dtype=np.float32)
    q /= (np.linalg.norm(q) + 1e-9)
    sims = mat @ q
    top = np.argsort(-sims)[:k]
    return [(float(sims[i]), meta[i]) for i in top]


def synthesize(question: str, hits: list[tuple[float, dict]]) -> str:
    catalog = "\n".join(f"[{m['creators']} {m['year']}] {m['title']}. {m['abstract']}" for _, m in hits)
    return Z.llm([
        {"role": "system", "content":
         "Answer only from the provided library abstracts. Cite inline as [Author Year]. "
         "If the library lacks evidence for part of the question, say so explicitly. Never invent a reference."},
        {"role": "user", "content":
         f"Question: {question}\n\nRetrieved library items ({len(hits)}, by semantic similarity):\n{catalog}\n\n"
         f"Write a concise, cited synthesis, then list the most relevant items."}])


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("question", nargs="+")
    ap.add_argument("-k", type=int, default=12, help="how many items to retrieve (default 12)")
    ap.add_argument("--list", action="store_true", help="print the ranked hits only; no chat call")
    args = ap.parse_args(argv)
    question = " ".join(args.question).strip()
    hits = retrieve(question, args.k)
    if args.list:
        print(f"Top {len(hits)} for: {question}\n")
        for s, m in hits:
            print(f"  {s:.3f}  {m['creators']} ({m['year']}): {m['title'][:70]}  [{m['key']}]")
        return 0
    answer = synthesize(question, hits)
    provider = Z.embed_provider()
    print(f"Q: {question}\n[retrieval={provider[2] if provider else '?'}, synthesis={Z.chat_model()}, top-{args.k}]\n\n{answer}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
