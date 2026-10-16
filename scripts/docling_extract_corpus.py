#!/usr/bin/env python3
"""Extract every PDF in the literature folder to clean text with Docling.

Docling rebuilds the reading order and drops running headers, footers and
page numbers at the source, so its markdown is far cleaner than a pdftotext
dump; text_clean.clean_pdf_text then polishes it. OCR is off, because a
digital PDF does not need it and a scan needs a decision, not a default.

Per PDF, named by its basename (the cite key, by the library convention):
  workspace/shared/papers/docling-md/<key>.md   Docling markdown, headings kept
  workspace/shared/papers/clean/<key>.txt       polished reading text, only when
                                                it passes clean_corpus.py's gate

The polished text is classified by the rule clean_corpus.py applies. A
landing page, an abstract page or a short text (JUNK or MISSING) is held back: its
markdown is kept, its clean text is not written, and a clean file an earlier
run left for it is removed. Run clean_corpus.py afterwards: its manifest is
what the attach and index steps read, and they use a key's clean text only
when its status there is GOOD or PARTIAL.

Checkpointed: a PDF whose markdown exists is skipped, so a rerun resumes.
Shardable: --shard i N takes the PDFs whose index modulo N is i, so N
processes can run side by side. An extraction under 200 words is treated as
a failure and nothing is written for it.

Needs the docling package (pip install docling); without it the script says
so and exits. Nothing here touches Zotero.

Usage:
  python3 scripts/docling_extract_corpus.py                       # every PDF under workspace/shared/papers/pdfs
  python3 scripts/docling_extract_corpus.py --pdfs DIR
  for i in 0 1 2 3; do python3 scripts/docling_extract_corpus.py --shard $i 4 & done
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import zotero_common as Z  # noqa: E402
from clean_corpus import classify, mark_sections  # noqa: E402
from text_clean import clean_pdf_text  # noqa: E402

PDFS = Z.PAPERS / "pdfs"
MD = Z.PAPERS / "docling-md"
CLEAN = Z.PAPERS / "clean"
MIN_WORDS = 200


def make_converter():
    from docling.datamodel.base_models import InputFormat
    from docling.datamodel.pipeline_options import PdfPipelineOptions
    from docling.document_converter import DocumentConverter, PdfFormatOption
    opts = PdfPipelineOptions()
    opts.do_ocr = False
    opts.do_table_structure = False
    return DocumentConverter(format_options={InputFormat.PDF: PdfFormatOption(pipeline_options=opts)})


def extract_one(converter, pdf: Path, md_dir: Path, clean_dir: Path) -> tuple[bool, str, bool]:
    """Write <key>.md, and <key>.txt when the text is GOOD or PARTIAL, for one PDF.

    Returns (extracted, reason, clean text written).
    """
    result = converter.convert(str(pdf))
    md = result.document.export_to_markdown()
    words = len(md.split())
    if words < MIN_WORDS:
        return False, f"{words} words, under {MIN_WORDS}", False
    (md_dir / f"{pdf.stem}.md").write_text(md, encoding="utf-8")
    text = clean_pdf_text(md)
    status = classify(text, mark_sections(text)[1])
    clean = clean_dir / f"{pdf.stem}.txt"
    if status not in Z.CLEAN_ELIGIBLE:
        if clean.is_file():
            clean.unlink()
        return True, f"{words} words, {status}: no clean text written", False
    clean.write_text(text, encoding="utf-8")
    return True, f"{words} words, {status}", True


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--pdfs", type=Path, default=PDFS, help=f"default {Z.rel(PDFS)}")
    ap.add_argument("--out-md", type=Path, default=MD)
    ap.add_argument("--out-clean", type=Path, default=CLEAN)
    ap.add_argument("--shard", nargs=2, type=int, default=[0, 1], metavar=("I", "N"))
    ap.add_argument("--limit", type=int, default=0, help="at most N PDFs (a pilot)")
    args = ap.parse_args(argv)
    i, n = args.shard
    if not args.pdfs.is_dir():
        print(f"no PDF directory at {Z.rel(args.pdfs)}")
        return 2
    try:
        import docling  # noqa: F401
    except ImportError:
        print("docling is not installed (pip install docling); no extraction was performed")
        return 2
    Z.ensure_dir(args.out_md)
    Z.ensure_dir(args.out_clean)
    pdfs = sorted(p for p in args.pdfs.glob("*.pdf") if p.is_file())
    todo = [p for idx, p in enumerate(pdfs) if idx % n == i and not (args.out_md / f"{p.stem}.md").is_file()]
    if args.limit:
        todo = todo[:args.limit]
    print(f"[shard {i}/{n}] {len(todo)} of {len(pdfs)} PDFs to extract from {Z.rel(args.pdfs)}", flush=True)
    if not todo:
        return 0
    converter = make_converter()
    ok = fail = held = 0
    for j, pdf in enumerate(todo, 1):
        try:
            good, reason, wrote = extract_one(converter, pdf, args.out_md, args.out_clean)
        except Exception as e:
            good, reason, wrote = False, f"{type(e).__name__} {str(e)[:60]}", False
        if good:
            ok += 1
            if not wrote:
                held += 1
                print(f"  held back {pdf.name}: {reason}", flush=True)
        else:
            fail += 1
            print(f"  fail {pdf.name}: {reason}", flush=True)
        if j % 25 == 0:
            print(f"  [shard {i}] {j}/{len(todo)} ok {ok} fail {fail}", flush=True)
    print(f"[shard {i}] done: {ok} extracted ({held} held back as JUNK or MISSING, no clean text), {fail} failed "
          f"-> {Z.rel(args.out_md)}/ and {Z.rel(args.out_clean)}/; run clean_corpus.py for the manifest", flush=True)
    return 0 if not fail else 1


if __name__ == "__main__":
    sys.exit(main())
