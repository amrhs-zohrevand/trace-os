"""Grounding must state whether it checked source bytes or reader-visible prose."""
import contextlib
import io
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "scripts"))
import review_grounding as rg  # noqa: E402


class SourceRepresentation(unittest.TestCase):
    def test_latex_comment_supports_only_literal_source_review(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "draft.tex"
            source.write_text("Visible claim.\n% Hidden support sentence.\n", encoding="utf-8")
            quote = "Hidden support sentence."
            self.assertEqual(rg.verdict(quote, rg.source_text(source)), "invented")
            self.assertEqual(rg.verdict(quote, rg.source_text(source, "literal-source")),
                             "grounded")

    def test_escaped_percent_remains_reader_visible(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "draft.tex"
            source.write_text("The response rate was 95\\%.\n", encoding="utf-8")
            prepared = rg.source_text(source)
            self.assertEqual(rg.verdict("The response rate was 95%.", prepared), "grounded")

    def test_a_percent_sign_outside_latex_is_content(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "memo.md"
            source.write_text("In 95% of the cases we coded by hand, reviewers agreed.\n", encoding="utf-8")
            self.assertEqual(rg.verdict("95% of the cases we coded by hand", rg.source_text(source)),
                             "grounded")

    @unittest.skipUnless((REPO / "scripts" / "deep_review.py").is_file(),
                         "deep_review.py is not shipped with the starter")
    def test_deep_review_strips_latex_comments_from_tex_sources_only(self):
        # LESSON (AH-156): the artifact a deep review sends and grounds against
        # stripped `%` to the line end from every file, so "95% of the cases"
        # in Markdown reached the reviewers, and the grounding, as "95".
        import deep_review
        with tempfile.TemporaryDirectory() as directory:
            memo = Path(directory) / "memo.md"
            memo.write_text("In 95% of the cases we coded by hand, reviewers agreed.\n", encoding="utf-8")
            tex = Path(directory) / "draft.tex"
            tex.write_text("Visible claim.\n% Hidden support sentence.\n", encoding="utf-8")
            artifact = deep_review.build_artifact([memo, tex], Path(directory) / "artifact.tex")
            self.assertIn("95% of the cases we coded by hand", artifact)
            self.assertNotIn("Hidden support sentence", artifact)
            self.assertEqual(rg.verdict("95% of the cases we coded by hand", rg.norm(artifact)), "grounded")


class EveryFragmentIsChecked(unittest.TestCase):
    """LESSON (AH-152): fragments of twelve characters or fewer were skipped, so a short verbatim
    quote read invented and a short made-up clause rode along unchecked."""

    SOURCE = rg.norm("The sample holds n = 1,204 firms. We find that the effect was "
                     "significantly smaller in the second period.")

    def test_a_short_verbatim_quote_is_grounded(self):
        for quote in ("n = 1,204", "1,204 firms"):
            with self.subTest(quote=quote):
                self.assertEqual(rg.verdict(quote, self.SOURCE), "grounded")

    def test_a_short_made_up_fragment_leaves_the_quote_invented(self):
        for quote in ("We find that the effect was ... p < 0.001",
                      "The sample holds n = 1,204 firms. Not true."):
            with self.subTest(quote=quote):
                self.assertEqual(rg.verdict(quote, self.SOURCE), "invented")

    def test_real_sentences_joined_across_a_gap_are_still_stitched(self):
        quote = "The sample holds n = 1,204 firms. Significantly smaller in the second period."
        self.assertEqual(rg.verdict(quote, self.SOURCE), "stitched")


class IssueHeadings(unittest.TestCase):
    """LESSON (AH-164): the heading pattern was case-sensitive and allowed one bullet, so
    `Issue 2 (High):` and `**Issue 3 (medium):**` were dropped and counted nowhere, and an
    issue the parser skipped lent its quote to the issue before it."""

    REVIEW = "\n".join([
        "Issue 1 (high): the first claim",
        '  Quote: "first span"',
        "Issue 2 (High): the second claim",
        '  Quote: "second span"',
        "**Issue 3 (medium):** the third claim",
        '  Quote: "third span"',
        "### Issue 4 (LOW): the fourth claim",
        "1. **Issue 5 (medium): the fifth claim**",
        '   Quote: "fifth span"',
        "Issue 6 [low] - a heading in another shape",
        '  Quote: "sixth span"',
        "- Issue 7 (low): the seventh claim",
        '  Quote: "seventh span"',
    ])

    def test_markdown_wrappers_and_capitals_are_read(self):
        issues, unparsed = rg.split_issues(self.REVIEW)
        self.assertEqual([(i["severity"], i["claim"], i["quote"]) for i in issues], [
            ("high", "the first claim", "first span"),
            ("high", "the second claim", "second span"),
            ("medium", "the third claim", "third span"),
            ("low", "the fourth claim", None),
            ("medium", "the fifth claim", "fifth span"),
            ("low", "the seventh claim", "seventh span"),
        ])
        self.assertEqual(rg.parse_issues(self.REVIEW), issues)

    def test_an_unparsed_heading_keeps_its_quote_and_is_counted(self):
        issues, unparsed = rg.split_issues(self.REVIEW)
        self.assertEqual(unparsed, ["Issue 6 [low] - a heading in another shape"])
        self.assertNotIn("sixth span", [i["quote"] for i in issues])

    def test_the_grounding_report_counts_unparsed_headings(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "artifact.md"
            source.write_text("first span second span third span fifth span seventh span\n",
                              encoding="utf-8")
            reviews = Path(directory) / "reviews"
            reviews.mkdir()
            (reviews / "reviewer-a.md").write_text(self.REVIEW + "\n", encoding="utf-8")
            out = io.StringIO()
            with mock.patch.object(sys, "argv", ["review_grounding.py", "--input", str(source),
                                                 "--dir", str(reviews)]), \
                    contextlib.redirect_stdout(out):
                rg.main()
            report = json.loads((reviews / "grounding.json").read_text(encoding="utf-8"))
            row = report["per_reviewer"]["reviewer-a"]
            self.assertEqual((row["issues"], row["quotes"], row["grounded"], row["noquote"]), (6, 5, 5, 1))
            self.assertEqual(row["unparsed"], ["Issue 6 [low] - a heading in another shape"])
            self.assertIn("UNPARSED [reviewer-a]: Issue 6 [low]", out.getvalue())

    @unittest.skipUnless((REPO / "scripts" / "deep_review.py").is_file(),
                         "deep_review.py is not shipped with the starter")
    def test_a_deep_review_reports_unparsed_headings(self):
        # LESSON (AH-164): deep_review.py still read issues with parse_issues, so a heading
        # it could not read was graded nowhere and appeared in no count, report or file.
        import deep_review
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "artifact.md"
            source.write_text("first span second span third span fifth span seventh span\n",
                              encoding="utf-8")
            out_dir = Path(directory) / "run"

            def ask(job, *_):
                return {**job, "text": self.REVIEW, "cost": 0.0}

            clustered = {"defects": [{"id": "D1", "label": "one defect", "severity": "high",
                                      "members": [0]}], "cost": 0.0}
            err = io.StringIO()
            with mock.patch.object(deep_review, "ask", ask), \
                    mock.patch.object(deep_review, "key", lambda: "no-key"), \
                    mock.patch.object(deep_review, "cluster", lambda rows, tok: clustered), \
                    mock.patch.object(sys, "argv", [
                        "deep_review.py", "--input", str(source), "--out-dir", str(out_dir),
                        "--models", "a/model", "--no-record"]), \
                    contextlib.redirect_stderr(err):
                deep_review.main()
            lenses = len(deep_review.LENSES)
            self.assertIn(f"{lenses} issue heading(s) did not parse", err.getvalue())
            self.assertIn("Issue 6 [low] - a heading in another shape", err.getvalue())
            report = (out_dir / "report.md").read_text(encoding="utf-8")
            self.assertIn(f"{lenses} issue heading(s) did not parse", report)
            self.assertIn("Issue 6 [low] - a heading in another shape", report)
            saved = json.loads((out_dir / "defects.json").read_text(encoding="utf-8"))
            self.assertEqual(len(saved["unparsed"]), lenses)
            self.assertEqual(saved["unparsed"][0]["heading"],
                             "Issue 6 [low] - a heading in another shape")


class QuoteLines(unittest.TestCase):
    """LESSON (AH-200): the Quote line was read only as a bare `Quote:`, so a reviewer who bolded,
    bulleted or block-quoted it, or wrote it in capitals, had its quote read as missing and its
    issue dropped as unquoted although the quote was there."""

    def test_markdown_wrappers_and_capitals_are_read(self):
        review = "\n".join([
            "Issue 1 (high): bold label",
            '**Quote:** "first span"',
            "Issue 2 (high): bulleted",
            '- Quote: "second span"',
            "Issue 3 (medium): block quote, no quotation marks",
            "> Quote: third span",
            "Issue 4 (low): capitals",
            '  QUOTE: "fourth span"',
            "Issue 5 (low): the whole line in bold",
            '**Quote: "fifth span"**',
            "Issue 6 (low): italic label",
            '  * *Quote*: "sixth span"',
            "Issue 7 (low): a plain line still reads as before",
            '  Quote: "seventh span"',
            "Issue 8 (low): a line that only mentions a quote",
            "Quoted elsewhere: not a quote",
        ])
        issues = rg.parse_issues(review)
        self.assertEqual([i["quote"] for i in issues], [
            "first span", "second span", "third span", "fourth span", "fifth span",
            "sixth span", "seventh span", None])


class SourceQuotes(unittest.TestCase):
    """LESSON (rc22): an issue carried one quote, from the reviewed file, so the source
    side of a mismatch (the data or the paper the text misreports) went unchecked."""

    REVIEW = "\n".join([
        "Issue 1 (high): the table misreports the sample",
        '  Quote: "1,204 firms"',
        "  Source: data/survey.md",
        '  Source quote: "The sample holds 1,240 firms."',
        "Issue 2 (medium): the paper says otherwise",
        '  Quote: "the effect was larger"',
        '  **Source quote:** "the effect was significantly smaller"',
        "Issue 3 (low): a made-up source",
        '  Quote: "1,204 firms"',
        '  Source quote: "no such sentence anywhere"',
        "Issue 4 (low): no source at all",
        '  Quote: "1,204 firms"',
    ])

    def test_the_source_lines_are_read_beside_the_quote(self):
        issues = rg.parse_issues(self.REVIEW)
        self.assertEqual([(i["quote"], i["source_quote"], i["source"]) for i in issues], [
            ("1,204 firms", "The sample holds 1,240 firms.", "data/survey.md"),
            ("the effect was larger", "the effect was significantly smaller", None),
            ("1,204 firms", "no such sentence anywhere", None),
            ("1,204 firms", None, None),
        ])

    def run_main(self, directory: Path, *sources: Path) -> tuple[dict, str]:
        artifact = directory / "table.md"
        artifact.write_text("The table reports 1,204 firms; the effect was larger.\n",
                            encoding="utf-8")
        reviews = directory / "reviews"
        reviews.mkdir()
        (reviews / "reviewer-a.md").write_text(self.REVIEW + "\n", encoding="utf-8")
        argv = ["review_grounding.py", "--input", str(artifact), "--dir", str(reviews)]
        for source in sources:
            argv += ["--source", str(source)]
        out = io.StringIO()
        with mock.patch.object(sys, "argv", argv), contextlib.redirect_stdout(out):
            rg.main()
        return json.loads((reviews / "grounding.json").read_text(encoding="utf-8")), out.getvalue()

    def test_each_source_quote_is_labelled_against_the_sources_given(self):
        import hashlib
        with tempfile.TemporaryDirectory() as directory:
            directory = Path(directory)
            (directory / "data").mkdir()
            survey = directory / "data" / "survey.md"
            survey.write_text("The sample holds 1,240 firms.\n", encoding="utf-8")
            paper = directory / "paper.md"
            paper.write_text("We find that the effect was significantly smaller.\n", encoding="utf-8")
            report, out = self.run_main(directory, survey, paper)
        rows = {row["claim"]: row for row in report["issues"]}
        named = rows["the table misreports the sample"]
        self.assertEqual((named["source_verdict"], named["source_held_by"]),
                         ("grounded", str(survey)))
        self.assertEqual(rows["the paper says otherwise"]["source_verdict"], "grounded")
        self.assertEqual(rows["the paper says otherwise"]["source_held_by"], str(paper))
        self.assertEqual(rows["a made-up source"]["source_verdict"], "invented")
        self.assertNotIn("source_verdict", rows["no source at all"])
        self.assertEqual(report["per_reviewer"]["reviewer-a"]["source_quotes"],
                         {"grounded": 2, "stitched": 0, "invented": 1, "not compared": 0})
        self.assertEqual(report["sources"][0]["sha256"],
                         hashlib.sha256(b"The sample holds 1,240 firms.\n").hexdigest())
        self.assertIn("INVENTED SOURCE QUOTE [reviewer-a]: no such sentence anywhere", out)
        # The candidate's own quotes are counted as before.
        self.assertEqual(report["per_reviewer"]["reviewer-a"]["grounded"], 4)

    def test_without_a_source_a_source_quote_is_not_compared_rather_than_invented(self):
        with tempfile.TemporaryDirectory() as directory:
            report, out = self.run_main(Path(directory))
        self.assertEqual(report["per_reviewer"]["reviewer-a"]["source_quotes"]["not compared"], 3)
        self.assertNotIn("sources", report)
        self.assertIn("no --source given, so none was compared", out)

    def test_a_source_that_is_not_text_is_named_and_not_compared(self):
        with tempfile.TemporaryDirectory() as directory:
            directory = Path(directory)
            pdf = directory / "paper.pdf"
            pdf.write_bytes(b"%PDF-1.7\n\xff\xfe\x00binary")
            report, out = self.run_main(directory, pdf)
        self.assertEqual(report["sources"][0]["compared"], False)
        self.assertEqual(report["per_reviewer"]["reviewer-a"]["source_quotes"]["not compared"], 3)
        self.assertIn("is not UTF-8", out)

    def test_a_named_source_that_was_not_given_is_not_compared_against_the_others(self):
        # LESSON (rc22 review, f1ca7a5 F002): an issue whose Source line named
        # a file that no --source gave had its quote looked for in every
        # source given, so text from a file the reviewer did not name read
        # grounded.
        with tempfile.TemporaryDirectory() as directory:
            directory = Path(directory)
            # The quote Issue 1 attributes to data/survey.md is in this other file.
            other = directory / "codebook.md"
            other.write_text("The sample holds 1,240 firms.\n", encoding="utf-8")
            report, out = self.run_main(directory, other)
        rows = {row["claim"]: row for row in report["issues"]}
        named = rows["the table misreports the sample"]
        self.assertEqual((named["source_verdict"], named["source_held_by"]), ("not compared", ""))
        self.assertEqual(named["source_named"], "data/survey.md")
        self.assertIn("NOT COMPARED [reviewer-a]: the issue names data/survey.md", out)
        # An issue that names no source is still checked against each one given.
        self.assertEqual(rows["a made-up source"]["source_verdict"], "invented")
        self.assertEqual(rg.source_verdict("The sample holds 1,240 firms.", None,
                                           {str(other): rg.norm("The sample holds 1,240 firms.")}),
                         ("grounded", str(other)))

    def test_a_review_without_source_lines_reads_as_before(self):
        with tempfile.TemporaryDirectory() as directory:
            directory = Path(directory)
            source = directory / "artifact.md"
            source.write_text("first span\n", encoding="utf-8")
            reviews = directory / "reviews"
            reviews.mkdir()
            (reviews / "r.md").write_text('Issue 1 (low): c\n  Quote: "first span"\n',
                                          encoding="utf-8")
            with mock.patch.object(sys, "argv", ["review_grounding.py", "--input", str(source),
                                                 "--dir", str(reviews)]), \
                    contextlib.redirect_stdout(io.StringIO()):
                rg.main()
            report = json.loads((reviews / "grounding.json").read_text(encoding="utf-8"))
        self.assertNotIn("source_quotes", report["per_reviewer"]["r"])
        self.assertEqual(sorted(report["issues"][0]),
                         ["claim", "grounded", "quote", "reviewer", "severity", "verdict"])


if __name__ == "__main__":
    unittest.main()
