#!/usr/bin/env python3
"""The Zotero knowledge-base scripts must read their configuration one way,
plan before they write, and degrade with a named reason.

Added 2026-09-21 with the zotero-knowledge-base example method. The scripts
came from one researcher's working copies, where a personal email, a
collection key and two other projects' .env files were hard-coded; these
tests pin the portable behaviour: every key comes from the project .env,
a missing key is named, the embedding provider follows the keys present,
the routing rules are data, and a write is never the default.

Nothing here touches the network: every function under test is pure, reads a
temporary file, or is a --help invocation.
Run:  python3 -X utf8 -I -S -m unittest discover -s tests
stdlib only; numpy is imported lazily by the two scripts that need it, so
importing them here does not need it.
"""
from __future__ import annotations

import contextlib
import io
import json
import pathlib
import subprocess
import sys
import tempfile
import unittest
import urllib.parse
from unittest import mock

REPO = pathlib.Path(__file__).resolve().parents[1]
SCRIPTS = REPO / "scripts"
# The skill's text is the kit's to pin; an installed project has no starter/
# tree, and its own copy of the skill is its own to edit or retire.
KIT_ZOTERO_SKILL = REPO / "starter/files/.skills/zotero-knowledge-base/SKILL.md"
sys.path.insert(0, str(SCRIPTS))
import cite_suggest  # noqa: E402
import clean_corpus  # noqa: E402
import text_clean  # noqa: E402
import zotero_clean  # noqa: E402
import zotero_common as Z  # noqa: E402
import zotero_dedupe  # noqa: E402
import zotero_enrich  # noqa: E402
import zotero_fill  # noqa: E402
import zotero_index  # noqa: E402
import zotero_reorg  # noqa: E402

ZOTERO_SCRIPTS = ("zotero_common.py", "zotero_clean.py", "zotero_fill.py", "zotero_dedupe.py",
                  "zotero_attach.py", "zotero_add.py", "zotero_reorg.py", "zotero_enrich.py",
                  "zotero_index.py", "zotero_ask.py", "cite_suggest.py", "text_clean.py",
                  "docling_extract_corpus.py", "clean_corpus.py", "zotero_attach_cleantext.py")
CLI_SCRIPTS = tuple(s for s in ZOTERO_SCRIPTS if s not in ("zotero_common.py", "text_clean.py"))


class EnvReading(unittest.TestCase):
    def setUp(self):
        Z._ENV_CACHE = None

    def tearDown(self):
        Z._ENV_CACHE = None

    def test_parse_env_handles_quotes_export_and_comments(self):
        text = 'export ZOTERO_USER_ID="12345"\nZOTERO_API_KEY=\'abc\'\n# comment\nCROSSREF_MAILTO=me@example.org # trailing\n\nBAD LINE\n'
        env = Z.parse_env(text)
        self.assertEqual(env, {"ZOTERO_USER_ID": "12345", "ZOTERO_API_KEY": "abc", "CROSSREF_MAILTO": "me@example.org"})

    def test_parse_env_repairs_cyrillic_homoglyphs_in_keys(self):
        # A Cyrillic capital A and a Cyrillic small o inside a pasted key.
        env = Z.parse_env("ZOTERO_API_KEY=Аbcоdef\n")
        self.assertEqual(env["ZOTERO_API_KEY"], "Abcodef")
        self.assertTrue(env["ZOTERO_API_KEY"].isascii())

    def test_load_env_without_a_file_is_empty(self):
        with tempfile.TemporaryDirectory() as d:
            self.assertEqual(Z.load_env(pathlib.Path(d) / ".env"), {})

    def test_require_names_every_missing_key_and_stops(self):
        Z._ENV_CACHE = {"ZOTERO_USER_ID": "1"}
        with self.assertRaises(SystemExit) as cm:
            Z.require("ZOTERO_USER_ID", "ZOTERO_API_KEY", "OPENROUTER_API_KEY")
        message = str(cm.exception)
        self.assertIn("ZOTERO_API_KEY", message)
        self.assertIn("OPENROUTER_API_KEY", message)
        self.assertNotIn("ZOTERO_USER_ID,", message)
        self.assertIn(".env.example", message)

    def test_require_returns_present_values(self):
        Z._ENV_CACHE = {"ZOTERO_USER_ID": "1", "ZOTERO_API_KEY": "k"}
        self.assertEqual(Z.require("ZOTERO_USER_ID", "ZOTERO_API_KEY"), {"ZOTERO_USER_ID": "1", "ZOTERO_API_KEY": "k"})

    def test_cutoff_uses_the_clock_not_a_typed_year(self):
        self.assertIsNone(Z.cutoff_for(None))
        self.assertEqual(Z.cutoff_for(5), f"{Z.this_year() - 5}-01-01")


class EmbeddingProvider(unittest.TestCase):
    def setUp(self):
        Z._ENV_CACHE = None

    def tearDown(self):
        Z._ENV_CACHE = None

    def test_no_keys_means_no_provider(self):
        Z._ENV_CACHE = {}
        self.assertIsNone(Z.embed_provider())

    def test_deepinfra_wins_when_both_keys_present(self):
        Z._ENV_CACHE = {"DEEPINFRA_API_KEY": "d", "OPENROUTER_API_KEY": "o"}
        name, url, model, key = Z.embed_provider()
        self.assertEqual((name, key), ("deepinfra", "d"))
        self.assertIn("deepinfra", url)
        self.assertEqual(model, "Qwen/Qwen3-Embedding-8B")

    def test_openrouter_is_the_fallback_in_the_same_space(self):
        Z._ENV_CACHE = {"OPENROUTER_API_KEY": "o"}
        name, url, model, key = Z.embed_provider()
        self.assertEqual((name, key), ("openrouter", "o"))
        self.assertEqual(model, "qwen/qwen3-embedding-8b")

    def test_explicit_provider_and_model_override(self):
        Z._ENV_CACHE = {"DEEPINFRA_API_KEY": "d", "OPENROUTER_API_KEY": "o",
                        "EMBED_PROVIDER": "openrouter", "EMBED_MODEL": "openai/text-embedding-3-small"}
        name, _, model, _ = Z.embed_provider()
        self.assertEqual((name, model), ("openrouter", "openai/text-embedding-3-small"))

    def test_custom_provider_needs_all_three_fields(self):
        Z._ENV_CACHE = {"EMBED_PROVIDER": "custom", "EMBED_URL": "https://x/v1/embeddings", "EMBED_API_KEY": "k"}
        self.assertIsNone(Z.embed_provider())
        Z._ENV_CACHE["EMBED_MODEL"] = "m"
        self.assertEqual(Z.embed_provider(), ("custom", "https://x/v1/embeddings", "m", "k"))

    def test_index_needs_the_cloud_keys_only_for_a_collection_or_a_silent_client(self):
        with mock.patch.object(Z, "local_api_answers", return_value=True):
            self.assertFalse(zotero_index.needs_cloud_keys(None))
            self.assertTrue(zotero_index.needs_cloud_keys("ABCD1234"))
        with mock.patch.object(Z, "local_api_answers", return_value=False):
            self.assertTrue(zotero_index.needs_cloud_keys(None))

    def test_embed_without_a_provider_stops_with_the_reason(self):
        Z._ENV_CACHE = {}
        with self.assertRaises(SystemExit) as cm:
            Z.embed(["x"])
        self.assertIn("DEEPINFRA_API_KEY", str(cm.exception))


class BibAndManuscript(unittest.TestCase):
    BIB = """@article{smith2020,
  title = {A {Study} of Things},
  author = {Smith, Jane and Doe, John},
  journal = {Management Science},
  year = {2020},
  doi = {10.1000/ABC.123}
}

@inproceedings{lee2019,
  title = {Other work},
  author = {Lee, K.},
  booktitle = {Proc.},
  year = {2019}
}
"""

    def test_bib_entries_and_index(self):
        with tempfile.TemporaryDirectory() as d:
            bib = pathlib.Path(d) / "bib"
            bib.mkdir()
            (bib / "refs.bib").write_text(self.BIB, encoding="utf-8")
            entries = Z.bib_entries(bib)
            self.assertEqual(entries["smith2020"]["doi"], "10.1000/ABC.123")
            self.assertEqual(entries["smith2020"]["title"], "A Study of Things")
            self.assertEqual(entries["lee2019"]["booktitle"], "Proc.")
            doi2k, t2k = Z.bib_index(bib)
            self.assertEqual(doi2k["10.1000/abc.123"], "smith2020")
            self.assertEqual(t2k[Z.norm_title("A Study of Things", 50)], "smith2020")

    def test_a_one_line_entry_keeps_its_own_fields_and_the_next_entry(self):
        # LESSON (AH-181): the reader ended an entry at the first "\n}", so a
        # one-line entry (the form doi.org returns) swallowed the next one and
        # gave its DOI to the wrong key; zotero_attach then planned Jones's PDF
        # onto Smith's item.
        bib = ("@article{Jones_2019, title={Jones on {Things}}, year={2019}}\n"
               "@article{Smith_2020,\n  title = {A Study of Things},\n  doi = {10.1234/smith.2020}\n}\n")
        with tempfile.TemporaryDirectory() as d:
            path = pathlib.Path(d) / "refs.bib"
            path.write_text(bib, encoding="utf-8")
            entries = Z.bib_entries(path)
            self.assertEqual(sorted(entries), ["Jones_2019", "Smith_2020"])
            self.assertEqual(entries["Jones_2019"]["doi"], "")
            self.assertEqual(entries["Jones_2019"]["title"], "Jones on Things")
            self.assertEqual(entries["Smith_2020"]["doi"], "10.1234/smith.2020")
            self.assertEqual(Z.bib_index(path)[0], {"10.1234/smith.2020": "Smith_2020"})

    def test_directives_escapes_quotes_and_parentheses_do_not_move_entry_bounds(self):
        # LESSON (AH-181): @string, @comment and @preamble are not entries, an
        # escaped brace or a brace inside a quoted value does not end one, and
        # an entry that never closes is dropped rather than read into the next.
        bib = ('@string{ms = "Management Science"}\n'
               "@comment{ @article{fake, doi = {10.9/fake}} }\n"
               '@preamble{"\\newcommand{\\noop}[1]{}"}\n'
               "@article{esc, title = {Braces \\{ escaped}, doi = {10.1/esc}}\n"
               '@article{quoted, title = "A {B} C", doi = "10.1/quoted"}\n'
               '@article(paren, title = "Why (not) now", doi = {10.1/paren})\n'
               "@article{open, title = {Never closed}, doi = {10.1/open},\n"
               "@article{after, title = {After the open one}, doi = {10.1/after}}\n")
        with tempfile.TemporaryDirectory() as d:
            path = pathlib.Path(d) / "refs.bib"
            path.write_text(bib, encoding="utf-8")
            with mock.patch("sys.stderr", new=io.StringIO()) as err:
                entries = Z.bib_entries(path)
            self.assertEqual(sorted(entries), ["after", "esc", "paren", "quoted"])
            self.assertEqual({k: e["doi"] for k, e in entries.items()},
                             {"esc": "10.1/esc", "quoted": "10.1/quoted", "paren": "10.1/paren",
                              "after": "10.1/after"})
            self.assertIn("'open'", err.getvalue())

    def test_a_field_name_inside_another_fields_value_is_not_that_field(self):
        # LESSON (AH-202): bib_field found `title =` anywhere in the entry, so
        # a note reading "see title = X" gave the entry the title "X" and the
        # title index a key under the wrong title.
        bib = ("@article{noted, note = {see title = Wrong one}, title = {The real title}, doi = {10.1/noted}}\n"
               '@article{quoted, note = "a, doi = 10.9/wrong", doi = {10.1/quoted}}\n'
               "@article{only_note, note = {title = Not a title, doi = 10.9/no}}\n"
               "@article{booked, booktitle = {Proceedings}, title = {Paper}}\n")
        with tempfile.TemporaryDirectory() as d:
            path = pathlib.Path(d) / "refs.bib"
            path.write_text(bib, encoding="utf-8")
            entries = Z.bib_entries(path)
        self.assertEqual(entries["noted"]["title"], "The real title")
        self.assertEqual(entries["quoted"]["doi"], "10.1/quoted")
        self.assertEqual((entries["only_note"]["title"], entries["only_note"]["doi"]), ("", ""))
        self.assertEqual((entries["booked"]["title"], entries["booked"]["booktitle"]), ("Paper", "Proceedings"))
        self.assertEqual(Z.bib_field(" title = {Plain}", "title"), "Plain")

    def test_an_escaped_brace_or_quote_does_not_move_a_values_end(self):
        # LESSON (AH-209): the value scanner counted `\{` as an opening brace,
        # so `title={Braces \{ escaped}` ran into the next field and the
        # title carried `doi=10.2/right`; a quoted value ended at `\"`.
        body = r"title={Braces \{ escaped}, doi={10.2/right}"
        self.assertEqual(Z.bib_field(body, "title"), r"Braces \{ escaped")
        self.assertEqual(Z.bib_field(body, "doi"), "10.2/right")
        body = r'author = "M{\"u}ller, Anna", title = "A \"quoted\" word", doi = {10.2/q}'
        self.assertEqual(Z.bib_field(body, "author"), r'M\"uller, Anna')
        self.assertEqual(Z.bib_field(body, "title"), r'A \"quoted\" word')
        self.assertEqual(Z.bib_field(body, "doi"), "10.2/q")
        self.assertEqual(Z.bib_field(r"title = {Closing \} brace}, year = {2020}", "title"),
                         r"Closing \} brace")

    def test_each_bib_file_is_read_on_its_own(self):
        # LESSON (AH-181): the files were joined before parsing, so an entry
        # left open at the end of one file read into the first entry of the next.
        with tempfile.TemporaryDirectory() as d:
            bib = pathlib.Path(d)
            (bib / "a.bib").write_text("@article{left_open, title = {No end},\n", encoding="utf-8")
            (bib / "b.bib").write_text("@article{whole, title = {Whole}, doi = {10.1/whole}}\n", encoding="utf-8")
            with mock.patch("sys.stderr", new=io.StringIO()):
                entries = Z.bib_entries(bib)
            self.assertEqual({k: e["doi"] for k, e in entries.items()}, {"whole": "10.1/whole"})

    def test_no_bib_dir_is_empty_not_an_error(self):
        with tempfile.TemporaryDirectory() as d:
            self.assertEqual(Z.bib_entries(pathlib.Path(d) / "absent"), {})

    def test_bib_entries_from_one_named_file(self):
        with tempfile.TemporaryDirectory() as d:
            bib = pathlib.Path(d) / "refs.bib"
            bib.write_text(self.BIB, encoding="utf-8")
            self.assertEqual(Z.bib_index(bib)[0]["10.1000/abc.123"], "smith2020")

    def test_cited_keys_across_main_and_sections(self):
        with tempfile.TemporaryDirectory() as d:
            paper = pathlib.Path(d)
            (paper / "sections").mkdir()
            (paper / "main.tex").write_text(r"\citep{smith2020} and \citet[p. 3]{lee2019, smith2020}", encoding="utf-8")
            (paper / "sections" / "01.tex").write_text(r"\cite{zhao2021}", encoding="utf-8")
            self.assertEqual(Z.cited_keys(paper / "main.tex"), ["lee2019", "smith2020", "zhao2021"])

    def test_placeholders_found_with_topic_online_and_ours(self):
        with tempfile.TemporaryDirectory() as d:
            f = pathlib.Path(d) / "s.tex"
            f.write_text("Prior work shows X (CITE). Also Y (CITE trust in automation). Z (CITE [search online]). "
                         r"\ours{need a cite on delegation} \ours{fix figure}", encoding="utf-8")
            found = cite_suggest.find_placeholders([f])
            self.assertEqual([p["topic"] for p in found], ["", "trust in automation", "search online", "need a cite on delegation"])
            self.assertEqual([p["online"] for p in found], [False, False, True, False])
            self.assertEqual([p["md"] for p in found], [False] * 4)

    def test_cite_suggest_scans_a_named_markdown_file_against_a_named_bib(self):
        with tempfile.TemporaryDirectory() as d:
            root = pathlib.Path(d)
            note = root / "note.md"
            note.write_text("An applicant may keep the application unpublished before grant (CITE).\n", encoding="utf-8")
            bib = root / "refs.bib"
            bib.write_text(self.BIB, encoding="utf-8")
            hit = (0.91, {"doi": "10.1000/ABC.123", "title": "A Study of Things", "creators": "Smith",
                          "year": "2020", "abstract": "About things."})
            report = root / "out" / "suggestions.md"
            with mock.patch.object(cite_suggest.A, "retrieve", return_value=[hit]) as retrieve, \
                 mock.patch.object(cite_suggest.Z, "llm", return_value="[@smith2020]: it is the study of things.") as llm, \
                 mock.patch.object(cite_suggest, "REPORT", report):
                self.assertEqual(cite_suggest.main([str(note), "--bib", str(bib)]), 0)
                self.assertIn("unpublished before grant", retrieve.call_args.args[0])
                self.assertIn("[@smith2020] | Smith (2020)", llm.call_args.args[0][1]["content"])
                self.assertIn("[@smith2020]: it is the study of things.", report.read_text(encoding="utf-8"))
                self.assertEqual(cite_suggest.main([str(root / "absent.md"), "--bib", str(bib)]), 2)


def crossref_reply(*hits):
    """A Crossref works-search answer holding the hits given as (title, doi, [family, ...])."""
    return {"message": {"items": [{"title": [title], "DOI": doi, "author": [{"family": f} for f in families]}
                                  for title, doi, families in hits]}}


class CrossrefMatch(unittest.TestCase):
    """LESSON (AH-182): crossref_doi took any hit when the normalised title was
    empty (a non-Latin title keeps no a-z letter) or short ("Introduction" sits
    inside many titles), and zotero_fill planned that DOI with nothing to show
    what it had matched."""

    def lookup(self, title, author, *hits):
        with mock.patch.object(Z, "get_json", return_value=crossref_reply(*hits)):
            return Z.crossref_doi(title, author)

    def test_a_non_latin_title_matches_only_its_own_title(self):
        self.assertIsNone(self.lookup("Исследование вещей", "Иванов",
                                      ("An unrelated paper", "10.9999/unrelated-1", ["Smith"])))
        self.assertEqual(self.lookup("Исследование вещей", "Иванов",
                                     ("Исследование вещей", "10.1/ru", ["Иванов"])), "10.1/ru")

    def test_a_short_title_inside_a_longer_one_is_no_match(self):
        self.assertIsNone(self.lookup("Introduction", "Smith",
                                      ("Introduction to Organizational Behaviour", "10.9/intro", ["Smith"])))
        # A short title that is equal still needs its author to say it is the same work.
        self.assertIsNone(self.lookup("Introduction", None, ("Introduction", "10.9/editorial", ["Lee"])))
        self.assertIsNone(self.lookup("Introduction", "Smith", ("Introduction", "10.9/editorial", ["Lee"])))
        self.assertEqual(self.lookup("Introduction", "Smith", ("Introduction", "10.1/smith", ["Smith"])), "10.1/smith")

    def test_the_full_title_must_agree_not_a_prefix(self):
        long_title = "Algorithmic management and the division of labour in platform firms: evidence from ride hailing"
        self.assertIsNone(self.lookup(long_title, "Smith",
                                      (long_title[:60] + " and food delivery", "10.9/other", ["Smith"])))
        self.assertEqual(self.lookup(long_title, "Smith", (long_title.upper(), "10.1/same", ["SMITH"])), "10.1/same")

    def test_two_hits_with_the_same_title_and_different_dois_are_ambiguous(self):
        self.assertIsNone(self.lookup("A Study of Things", "Smith",
                                      ("A study of things", "10.1/a", ["Smith"]),
                                      ("A Study of Things", "10.1/b", ["Smith"])))

    def test_the_fill_plan_shows_the_crossref_title_and_doi_it_matched(self):
        item = {"data": {"key": "ITEM0001", "itemType": "journalArticle", "title": "A Study of Things",
                         "abstractNote": "An abstract that is already there.", "creators": [{"lastName": "Smith"}]},
                "version": 7}
        with tempfile.TemporaryDirectory() as d:
            plan_path = pathlib.Path(d) / "fill-plan.json"
            out = io.StringIO()
            with mock.patch.object(Z, "cloud_items", return_value=[item]), \
                 mock.patch.object(Z, "get_json", return_value=crossref_reply(("A study of things", "10.1/a", ["Smith"]))), \
                 mock.patch.object(zotero_fill, "PLAN", plan_path), \
                 mock.patch.object(zotero_fill.time, "sleep"), contextlib.redirect_stdout(out):
                self.assertEqual(zotero_fill.build_plan(5), 0)
            plan = json.loads(plan_path.read_text(encoding="utf-8"))
        self.assertEqual(plan[0]["fields"], {"DOI": "10.1/a"})
        self.assertEqual(plan[0]["matched"]["DOI"], {"doi": "10.1/a", "crossref_title": "A study of things"})
        self.assertIn("10.1/a", out.getvalue())
        self.assertIn("A study of things", out.getvalue())


class OnlinePlaceholderQuery(unittest.TestCase):
    """LESSON (AH-188): `(CITE [search online])` sent the words "search online"
    to the library and to Crossref, not the sentence that needs the citation.
    The parser keeps its stored topic; the query is chosen afterwards."""

    def test_the_online_placeholder_queries_the_sentence_and_its_extra_topic(self):
        with tempfile.TemporaryDirectory() as d:
            root = pathlib.Path(d)
            note = root / "note.md"
            note.write_text("Delegating hiring to algorithms lowers the offer rate (CITE [search online]).\n"
                            "Teams trust automation unevenly (CITE [search online] trust calibration).\n",
                            encoding="utf-8")
            bib = root / "refs.bib"
            bib.write_text(BibAndManuscript.BIB, encoding="utf-8")
            urls = []

            def get_json(url, **_):
                urls.append(urllib.parse.parse_qs(urllib.parse.urlsplit(url).query)["query"][0])
                return {"message": {"items": []}}

            with mock.patch.object(cite_suggest.A, "retrieve", return_value=[]) as retrieve, \
                 mock.patch.object(cite_suggest.Z, "llm", return_value="None fit.") as llm, \
                 mock.patch.object(cite_suggest.Z, "get_json", side_effect=get_json), \
                 mock.patch.object(cite_suggest, "REPORT", root / "out.md"), \
                 contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(cite_suggest.main([str(note), "--bib", str(bib)]), 0)
            queries = [c.args[0] for c in retrieve.call_args_list]
            self.assertIn("Delegating hiring to algorithms lowers the offer rate", queries[0])
            self.assertEqual(queries[1], "trust calibration")
            self.assertEqual(urls, queries)
            for q in queries + [c.args[0][1]["content"] for c in llm.call_args_list]:
                self.assertNotIn("search online", q)
            self.assertEqual([p["topic"] for p in cite_suggest.find_placeholders([note])],
                             ["search online", "search online] trust calibration"])


class AuditAndDedupe(unittest.TestCase):
    def test_audit_groups_by_doi_then_by_title(self):
        items = [
            {"key": "A", "itemType": "journalArticle", "title": "Same paper", "DOI": "10.1/x", "abstractNote": "a"},
            {"key": "B", "itemType": "journalArticle", "title": "Same paper", "DOI": "10.1/X", "abstractNote": ""},
            {"key": "C", "itemType": "journalArticle", "title": "Another: paper", "abstractNote": "c"},
            {"key": "D", "itemType": "journalArticle", "title": "Another paper", "abstractNote": ""},
            {"key": "E", "itemType": "webpage", "title": "Site | Page", "abstractNote": ""},
        ]
        a = zotero_clean.audit(items)
        self.assertEqual(list(a["dup_doi"]), ["10.1/x"])
        self.assertEqual([x["key"] for x in a["dup_title"][Z.norm_title("Another paper")]], ["C", "D"])
        self.assertEqual((a["no_abstract"], a["no_doi"], a["scraped"]), (3, 2, 1))

    def test_dedupe_master_prefers_children_then_abstract_then_older(self):
        def item(key, children, abstract, added, version=1):
            return {"data": {"key": key, "DOI": "10.1/dup", "title": "T", "abstractNote": abstract, "dateAdded": added},
                    "version": version, "meta": {"numChildren": children}}
        plan = zotero_dedupe.merge_plan([
            item("NEW", 0, "abstract", "2026-01-01T00:00:00Z"),
            item("OLD", 0, "abstract", "2020-01-01T00:00:00Z"),
            item("KIDS", 2, "", "2025-01-01T00:00:00Z"),
        ])
        self.assertEqual(len(plan), 1)
        self.assertEqual(plan[0]["master"], "KIDS")
        self.assertEqual([d["key"] for d in plan[0]["dupes"]], ["OLD", "NEW"])

    def test_single_items_are_not_a_group(self):
        plan = zotero_dedupe.merge_plan([{"data": {"key": "A", "DOI": "10.1/a"}, "version": 1, "meta": {}},
                                         {"data": {"key": "B", "DOI": "10.1/b"}, "version": 1, "meta": {}}])
        self.assertEqual(plan, [])


class Taxonomy(unittest.TestCase):
    EXAMPLE = SCRIPTS / "zotero_taxonomy.example.json"

    def test_example_validates_and_every_rule_names_a_leaf(self):
        tax = zotero_reorg.load_taxonomy(self.EXAMPLE)
        ids = {n["id"] for n in tax["tree"]}
        self.assertEqual(len(tax["tree"]), 22)
        self.assertTrue(all(r["leaf"] in ids for r in tax["rules"]))
        self.assertEqual(tax["root"], "REPLACE_WITH_COLLECTION_KEY")

    def test_rule_order_is_priority_and_none_excludes(self):
        rules = [{"leaf": "SPECIFIC", "any": ["explainab"]},
                 {"leaf": "STRAT", "any": ["strateg"], "none": ["economic structure"]},
                 {"leaf": "WORK", "any": ["economic"]}]
        self.assertEqual(zotero_reorg.route({"theme": "x"}, "Explainable strategy", rules), "SPECIFIC")
        self.assertEqual(zotero_reorg.route({"theme": "x"}, "Strategy and firms", rules), "STRAT")
        self.assertEqual(zotero_reorg.route({"theme": "x"}, "Strategy and economic structure", rules), "WORK")
        self.assertEqual(zotero_reorg.route({"theme": "x"}, "nothing", rules), "UNSORTED")

    def test_theme_any_reads_the_theme_field_only(self):
        rules = [{"leaf": "OFF", "theme_any": ["off-topic"]}]
        self.assertEqual(zotero_reorg.route({"theme": "Off-topic"}, "anything", rules), "OFF")
        self.assertEqual(zotero_reorg.route({"theme": "core"}, "an off-topic title", rules), "UNSORTED")

    def test_other_string_fields_join_the_text(self):
        rules = [{"leaf": "L", "any": ["delegat"]}]
        self.assertEqual(zotero_reorg.route({"theme": "x", "transform_code": "delegation to agents"}, "T", rules), "L")

    def test_taxonomy_with_unknown_parent_is_refused(self):
        with tempfile.TemporaryDirectory() as d:
            p = pathlib.Path(d) / "t.json"
            p.write_text(json.dumps({"root": "R", "tree": [{"id": "A", "name": "A", "parent": "NOPE"}], "rules": []}))
            with self.assertRaises(SystemExit):
                zotero_reorg.load_taxonomy(p)

    def test_missing_taxonomy_names_the_example(self):
        with self.assertRaises(SystemExit) as cm:
            zotero_reorg.load_taxonomy(pathlib.Path("/nonexistent/taxonomy.json"))
        self.assertIn("zotero_taxonomy.example.json", str(cm.exception))


class ReorgLibrary:
    """Collections and items behind a fake Z.zreq and urlopen. A collection
    create answers with create_result; every call is recorded."""

    def __init__(self, collections, create_result=None):
        self.collections = {k: {"key": k, "name": n, "parentCollection": p or False, "version": 3}
                            for k, (n, p) in collections.items()}
        self.items = {"ITEM0001": {"key": "ITEM0001", "version": 2, "collections": [], "tags": []}}
        self.create_result = create_result or OK_WRITE
        self.calls = []

    def zreq(self, method, path, data=None, headers=None, raw=False, tries=3, timeout=60):
        self.calls.append((method, path))
        if method == "GET" and path.startswith("/collections?"):
            return None, ([{"data": c} for c in self.collections.values()] if "start=0" in path else [])
        if method == "POST" and path == "/collections":
            return None, self.create_result
        if method == "GET" and path.startswith("/items?itemKey="):
            return None, [{"data": dict(self.items[k])} for k in path.split("=", 1)[1].split("&")[0].split(",")
                          if k in self.items]
        if method == "POST" and path == "/items":
            updates = json.loads(data)
            return None, {"successful": {str(i): u for i, u in enumerate(updates)}, "failed": {}}
        raise AssertionError(f"unexpected call {method} {path}")

    def urlopen(self, request, timeout=None):
        self.calls.append((request.get_method(), request.full_url))
        return io.BytesIO(b"")

    def sent(self, *methods):
        return [c for c in self.calls if c[0] in methods]


class Reorg(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.state = pathlib.Path(tmp.name)
        self.taxonomy = self.state / "taxonomy.json"
        self.taxonomy.write_text(json.dumps({"root": "ROOT0001", "tree": [{"id": "A", "name": "Leaf A", "parent": None}],
                                             "rules": []}), encoding="utf-8")
        (self.state / "graded.json").write_text(json.dumps([{"key": "ITEM0001", "tier": "core"}]), encoding="utf-8")
        Z._ENV_CACHE = {"ZOTERO_USER_ID": "1", "ZOTERO_API_KEY": "k"}
        self.addCleanup(setattr, Z, "_ENV_CACHE", None)

    def run_reorg(self, library, *argv):
        out = io.StringIO()
        paths = {name: self.state / file for name, file in (("STATE", "."), ("MAPPING", "mapping.json"),
                                                              ("TREE_KEYS", "tree-keys.json"),
                                                              ("CROSSCUT_KEYS", "crosscut-keys.json"))}
        with contextlib.ExitStack() as stack:
            for name, path in paths.items():
                stack.enter_context(mock.patch.object(zotero_reorg, name, path, create=True))
            stack.enter_context(mock.patch.object(Z, "zreq", library.zreq))
            stack.enter_context(mock.patch("urllib.request.urlopen", library.urlopen))
            stack.enter_context(mock.patch("time.sleep"))
            stack.enter_context(contextlib.redirect_stdout(out))
            code = zotero_reorg.main(["--taxonomy", str(self.taxonomy), *argv])
        return code, out.getvalue()

    def crosscut(self, library, *argv):
        return self.run_reorg(library, *argv, "crosscut", "--name", "Core reading",
                              "--graded", str(self.state / "graded.json"), "--tag", "core=tier:core")

    def test_a_failed_crosscut_create_stops_before_any_item_update(self):
        # LESSON (AH-186): crosscut --write went on with no collection key,
        # tagged the items and reported them "filed".
        library = ReorgLibrary({}, create_result={"successful": {}, "success": {}, "unchanged": {},
                                                  "failed": {"0": {"code": 400, "message": "Invalid name"}}})
        code, out = self.crosscut(library, "--write")
        self.assertEqual(code, 1)
        self.assertIn("FAIL", out)
        self.assertIn("Invalid name", out)
        self.assertNotIn("APPLIED", out)
        self.assertEqual([c for c in library.sent("POST") if c[1] == "/items"], [])
        self.assertEqual(library.items["ITEM0001"]["tags"], [])

    def test_the_crosscut_collection_created_or_reused_is_kept_by_delete_old(self):
        # LESSON (AH-187): the cross-cut key was never saved, so delete-old
        # listed the cross-cut collection with the legacy ones.
        library = ReorgLibrary({"LEG00001": ("Old stuff", "ROOT0001")},
                               create_result={"successful": {"0": {"key": "XCUT0001"}}, "success": {"0": "XCUT0001"},
                                              "unchanged": {}, "failed": {}})
        self.assertEqual(self.crosscut(library, "--write")[0], 0)
        library.collections["XCUT0001"] = {"key": "XCUT0001", "name": "Core reading", "parentCollection": "ROOT0001",
                                           "version": 4}
        code, out = self.run_reorg(library, "delete-old")
        self.assertIn("[LEG00001]", out)
        self.assertNotIn("XCUT0001", out)
        # Reused by name in a later write, it is kept as well.
        (self.state / "crosscut-keys.json").unlink()
        self.assertEqual(self.crosscut(library, "--write")[0], 0)
        self.assertNotIn("XCUT0001", self.run_reorg(library, "delete-old")[1])

    def test_a_crosscut_from_before_its_key_was_saved_is_kept_only_by_name(self):
        # LESSON (AH-187): a crosscut --write run made before the fix saved no
        # key, so delete-old lists that collection, and nothing said to name it.
        library = ReorgLibrary({"XCUT0001": ("Core reading", "ROOT0001")})
        self.assertIn("[XCUT0001]", self.run_reorg(library, "delete-old")[1])
        self.assertNotIn("[XCUT0001]", self.run_reorg(library, "delete-old", "--keep-name", "Core reading")[1])
        help_text = subprocess.run([sys.executable, "-X", "utf8", "-I", "-S", str(SCRIPTS / "zotero_reorg.py"),
                                    "delete-old", "--help"], capture_output=True, text=True, timeout=60).stdout
        self.assertIn("saved no key", " ".join(help_text.split()))
        if KIT_ZOTERO_SKILL.is_file():
            self.assertIn("saved none, so name such a collection with `--keep-name` before "
                          "`zotero_reorg.py --write delete-old`",
                          KIT_ZOTERO_SKILL.read_text(encoding="utf-8"))

    def test_delete_old_never_lists_an_ancestor_of_a_kept_collection(self):
        # LESSON (AH-187): create-tree reuses a collection by name anywhere, so
        # a kept leaf can sit under a legacy collection, and deleting that
        # collection deletes the leaf with it.
        library = ReorgLibrary({"OLD00001": ("Old themes", "ROOT0001"), "LEAF0001": ("Leaf A", "OLD00001"),
                                "LEG00001": ("Old stuff", "ROOT0001"), "NAMED001": ("Keep this", "LEG00002"),
                                "LEG00002": ("Old holder", "ROOT0001"), "LEG00003": ("Gone", "ROOT0001")})
        (self.state / "tree-keys.json").write_text(json.dumps({"A": "LEAF0001"}), encoding="utf-8")
        _, out = self.run_reorg(library, "delete-old", "--keep-name", "Keep this")
        listed = [line for line in out.splitlines() if line.startswith("  - ")]
        self.assertEqual(sorted(line.rsplit("[", 1)[1].rstrip("]") for line in listed), ["LEG00001", "LEG00003"])

    def test_delete_old_says_it_deletes_with_subcollections_and_deletes_each_top_once(self):
        # LESSON (AH-189): delete-old said it moved collections to the trash,
        # but a collection DELETE removes the collection and its subcollections.
        claims_trash = r"(?i)\b(to trash|trashed|trashes|trash (the|these|legacy))\b"
        library = ReorgLibrary({"LEG00001": ("Old stuff", "ROOT0001"), "LEG00002": ("Older", "LEG00001")})
        code, out = self.run_reorg(library, "delete-old")
        self.assertEqual(code, 0)
        self.assertNotRegex(out, claims_trash)
        self.assertIn("delete these collections", out)
        self.assertIn("subcollections", out)
        self.assertEqual(library.sent("DELETE"), [])
        code, out = self.run_reorg(library, "--write", "delete-old")
        self.assertEqual([url.rsplit("/", 1)[1] for _, url in library.sent("DELETE")], ["LEG00001"])
        self.assertNotRegex(out, claims_trash)
        self.assertIn("Deleted 1/1", out)
        help_text = subprocess.run([sys.executable, "-X", "utf8", "-I", "-S", str(SCRIPTS / "zotero_reorg.py"), "--help"],
                                   capture_output=True, text=True, timeout=60).stdout
        self.assertNotRegex(help_text, claims_trash)
        self.assertIn("nothing goes to the\n                          trash", zotero_reorg.__doc__)
        if KIT_ZOTERO_SKILL.is_file():
            skill = KIT_ZOTERO_SKILL.read_text(encoding="utf-8")
            self.assertNotRegex(skill.split("## Reorganize a collection")[1].split("## Enrich")[0], claims_trash)

    def test_a_failed_batch_is_reported_and_the_next_batch_still_runs(self):
        # LESSON (AH-201): an HTTP error on one batch's POST (a 412 version
        # conflict) stopped the whole run, so the later batches were never
        # tried or reported, and the exit code said nothing had gone wrong.
        import urllib.error
        keys = [f"ITEM{n:04d}" for n in range(30)]
        (self.state / "mapping.json").write_text(json.dumps({k: {"leaf": "A"} for k in keys}), encoding="utf-8")
        (self.state / "tree-keys.json").write_text(json.dumps({"A": "LEAF0001"}), encoding="utf-8")
        (self.state / "graded.json").write_text(json.dumps([{"key": k, "tier": "core"} for k in keys]),
                                                encoding="utf-8")
        for argv in (("--write", "apply"),
                     ("--write", "crosscut", "--name", "Core reading", "--graded",
                      str(self.state / "graded.json"), "--tag", "core=tier:core")):
            with self.subTest(command=argv[1]):
                library = ReorgLibrary({"XCUT0001": ("Core reading", "ROOT0001")})
                library.items = {k: {"key": k, "version": 2, "collections": [], "tags": []} for k in keys}
                posts = []
                answer = library.zreq

                def zreq(method, path, data=None, **kwargs):
                    if method == "POST" and path == "/items":
                        posts.append([u["key"] for u in json.loads(data)])
                        if len(posts) == 1:
                            raise urllib.error.HTTPError(path, 412, "Precondition Failed", {}, None)
                    return answer(method, path, data=data, **kwargs)

                library.zreq = zreq
                code, out = self.run_reorg(library, *argv)
                self.assertEqual(code, 1)
                self.assertEqual(posts, [keys[:25], keys[25:]])
                self.assertIn("412", out)
                self.assertIn("ITEM0000", out.split("412", 1)[1].splitlines()[0])
                self.assertIn("5 ", out.split("APPLIED: ", 1)[1])
                self.assertIn("25 failed", out.split("APPLIED: ", 1)[1])


class Cards(unittest.TestCase):
    def test_fenced_json_card_parses_and_facets_are_filtered(self):
        text = '```json\n{"tldr": "a", "finding": "b", "method": "c", "relevance": "d", "facets": ["method:survey", "bad", 3]}\n```'
        card = zotero_enrich.parse_card(text)
        self.assertEqual(card["facets"], ["method:survey"])

    def test_preamble_around_the_json_is_ignored(self):
        text = 'Here is the card:\n{"tldr": "a", "finding": "b", "method": "c", "relevance": "d"}\nHope this helps.'
        self.assertEqual(zotero_enrich.parse_card(text)["tldr"], "a")

    def test_card_missing_a_field_is_rejected(self):
        self.assertIsNone(zotero_enrich.parse_card('{"tldr": "a", "finding": "b"}'))
        self.assertIsNone(zotero_enrich.parse_card("not json"))


OK_WRITE = {"successful": {"0": {"key": "NEW00001"}}, "success": {"0": "NEW00001"}, "unchanged": {}, "failed": {}}
FAILED_WRITE = {"successful": {}, "success": {}, "unchanged": {},
                "failed": {"0": {"key": "ITEM0001", "code": 412, "message": "Item has been modified"}}}


class EnrichLibrary:
    """One item, its child notes and its tags, behind a fake Z.zreq that answers
    each POST with the next canned result (a success once the list is used up)."""

    def __init__(self, note_results=(), tag_results=()):
        self.note_results, self.tag_results = list(note_results), list(tag_results)
        self.notes, self.tags, self.calls = [], [{"tag": "keep-me"}], []

    def zreq(self, method, path, data=None, headers=None, raw=False, tries=3, timeout=60):
        self.calls.append((method, path, tries))
        if method == "GET" and path == "/items/ITEM0001":
            return None, {"key": "ITEM0001", "data": {"key": "ITEM0001", "version": 5, "tags": list(self.tags)}}
        if method == "GET" and path.startswith("/items/ITEM0001/children"):
            return None, [{"data": {"key": f"NOTE{i:04}", "itemType": "note", "note": n}} for i, n in enumerate(self.notes)]
        if method == "POST" and path == "/items":
            obj = json.loads(data)[0]
            if obj.get("itemType") == "note":
                result = self.note_results.pop(0) if self.note_results else OK_WRITE
                if result is OK_WRITE:
                    self.notes.append("<div data-schema-version=\"9\">" + obj["note"] + "</div>")
            else:
                result = self.tag_results.pop(0) if self.tag_results else OK_WRITE
                if result is OK_WRITE:
                    self.tags = obj["tags"]
            return None, result
        raise AssertionError(f"unexpected call {method} {path}")


class EnrichApply(unittest.TestCase):
    """LESSON (AH-185): apply discarded both POST results, so a note or a tag
    write Zotero refused inside a 200 answer counted as done; a refused tag
    left no `enriched` mark and the next run posted a second card."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.cards = pathlib.Path(tmp.name) / "cards.json"
        self.cards.write_text(json.dumps([{"key": "ITEM0001", "tldr": "a", "finding": "b", "method": "c",
                                           "relevance": "d", "facets": ["method:survey"], "by": "a model"}]),
                              encoding="utf-8")
        Z._ENV_CACHE = {"ZOTERO_USER_ID": "1", "ZOTERO_API_KEY": "k"}
        self.addCleanup(setattr, Z, "_ENV_CACHE", None)

    def apply(self, library, *argv):
        out = io.StringIO()
        with mock.patch.object(Z, "zreq", library.zreq), mock.patch.object(zotero_enrich, "CARDS", self.cards), \
             mock.patch.object(zotero_enrich.time, "sleep"), contextlib.redirect_stdout(out):
            code = zotero_enrich.main(["apply", *argv])
        return code, out.getvalue()

    def test_a_refused_tag_write_is_reported_and_the_retry_reuses_the_card(self):
        library = EnrichLibrary(tag_results=[FAILED_WRITE])
        code, out = self.apply(library, "--write")
        self.assertEqual(code, 1)
        self.assertIn("FAIL", out)
        self.assertIn("ITEM0001", out)
        self.assertIn("412", out)
        self.assertEqual(len(library.notes), 1)
        self.assertNotIn("enriched", {t["tag"] for t in library.tags})
        # The dry run and the retry both see the card that is already there.
        code, out = self.apply(library)
        self.assertEqual(code, 0)
        self.assertIn("already has its card", out)
        code, out = self.apply(library, "--write")
        self.assertEqual(code, 0)
        self.assertEqual(len(library.notes), 1)
        self.assertEqual({t["tag"] for t in library.tags}, {"keep-me", "method:survey", "enriched"})
        self.assertTrue(all(tries == 1 for m, _, tries in library.calls if m == "POST"))

    def test_a_refused_note_is_reported_and_the_item_is_not_marked_enriched(self):
        library = EnrichLibrary(note_results=[FAILED_WRITE])
        code, out = self.apply(library, "--write")
        self.assertEqual(code, 1)
        self.assertIn("FAIL", out)
        self.assertIn("ITEM0001", out)
        self.assertEqual(library.notes, [])
        self.assertEqual(sum(1 for m, _, _ in library.calls if m == "POST"), 1)  # no tag write after it
        self.assertNotIn("enriched", {t["tag"] for t in library.tags})
        # The next run posts the card, then marks the item.
        self.assertEqual(self.apply(library, "--write")[0], 0)
        self.assertEqual(len(library.notes), 1)
        self.assertIn("enriched", {t["tag"] for t in library.tags})


class CleanText(unittest.TestCase):
    def test_dehyphenation_spaced_letters_and_page_numbers(self):
        raw = "Intro-\nduction to A R T I C L E\nPage 3 of 21\nBody text here.\n"
        cleaned = text_clean.deep_clean(raw)
        self.assertIn("Introduction to ARTICLE", cleaned)
        self.assertNotIn("Page 3", cleaned)

    def test_strip_for_embedding_drops_references_and_citations(self):
        raw = "Findings hold (Smith, 2020) and elsewhere [12].\n\nReferences\nSmith, J. 2020. A paper.\n"
        stripped = text_clean.strip_for_embedding(raw)
        self.assertNotIn("Smith", stripped)
        self.assertNotIn("[12]", stripped)
        self.assertIn("Findings hold", stripped)

    def test_sections_are_marked_and_classified(self):
        body = "Abstract\nx\n# 1 Introduction\n" + "word " * 1200 + "\n## Methods\n" + "word " * 1200 + \
               "\n## Results\n" + "word " * 700 + "\n## Discussion\ny\nReferences\nz"
        marked, found = clean_corpus.mark_sections(body)
        self.assertIn("===== SECTION: METHODS =====", marked)
        self.assertEqual(clean_corpus.classify(body, found), "GOOD")
        self.assertEqual(clean_corpus.classify("short", set()), "MISSING")
        self.assertEqual(clean_corpus.classify("Please sign in to access " + "w " * 2000, {"introduction"}), "JUNK")

    def test_fulltext_opening_reads_the_clean_text_by_cite_key(self):
        # Since AH-183 the index reads clean text only for a key the manifest
        # classifies GOOD or PARTIAL, so this fixture carries its manifest row.
        with tempfile.TemporaryDirectory() as d:
            clean = pathlib.Path(d) / "clean"
            clean.mkdir()
            (clean / "smith2020.txt").write_text("KEY: smith2020\nSOURCE: pdf\n\nThe opening sentence.", encoding="utf-8")
            manifest = write_manifest(pathlib.Path(d), smith2020="GOOD")
            old = zotero_index.CLEAN_DIRS, zotero_index.MANIFEST
            zotero_index.CLEAN_DIRS, zotero_index.MANIFEST = [clean], manifest
            try:
                opening = zotero_index.fulltext_opening({"DOI": "10.1/x", "title": "T"}, {"10.1/x": "smith2020"}, {})
            finally:
                zotero_index.CLEAN_DIRS, zotero_index.MANIFEST = old
            self.assertEqual(opening.strip(), "The opening sentence.")
            self.assertEqual(zotero_index.item_meta({"key": "K", "date": "March 2019", "creators": []})["year"], "2019")


def write_manifest(folder: pathlib.Path, **statuses) -> pathlib.Path:
    """A clean-corpus manifest in folder with one row per key=STATUS."""
    path = folder / "clean-corpus-manifest.csv"
    rows = ["key,source,words,core_sections,sections_found,status"]
    rows += [f"{key},pdf,4000,4,introduction;methods;results;discussion,{status}" for key, status in statuses.items()]
    path.write_text("\n".join(rows) + "\n", encoding="utf-8")
    return path


GOOD_HTML = ("<html><body><h1>A Study</h1>\n<h2>Introduction</h2>\n<p>" + "word " * 1100 + "</p>\n"
             "<h2>Methods</h2>\n<p>" + "word " * 1100 + "</p>\n<h2>Results</h2>\n<p>" + "word " * 1100 + "</p>\n"
             "<h2>Discussion</h2>\n<p>end.</p></body></html>")
LANDING_HTML = ("<html><body><p>A Study of Things</p>\n<p>Purchase PDF to read the full article.</p>\n<p>"
                + "abstract words " * 200 + "</p></body></html>")


class CleanTextEligibility(unittest.TestCase):
    """LESSON (AH-183): clean_corpus.py wrote clean/<key>.txt for any status with
    200 words, JUNK included; docling_extract_corpus.py wrote clean/ with no
    landing-page check; zotero_attach_cleantext.py attached every clean/*.txt
    as "the verified reading text"; zotero_index.py embedded any clean or
    extracted/ file as the paper's full text; and a clean file outlived a later
    reclassification. Eligibility now follows the manifest status, GOOD or
    PARTIAL, for the producers and the consumers."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = pathlib.Path(tmp.name)
        Z._ENV_CACHE = {"ZOTERO_USER_ID": "1", "ZOTERO_API_KEY": "k"}
        self.addCleanup(setattr, Z, "_ENV_CACHE", None)

    def test_clean_corpus_writes_clean_text_only_for_good_or_partial_and_removes_stale_text(self):
        pdfs, clean = self.dir / "pdfs", self.dir / "clean"
        pdfs.mkdir()
        clean.mkdir()
        (pdfs / "good.html").write_text(GOOD_HTML, encoding="utf-8")
        (pdfs / "junk.html").write_text(LANDING_HTML, encoding="utf-8")
        (pdfs / "gone.html").write_text("<p>moved</p>", encoding="utf-8")
        for key in ("junk", "gone"):  # clean text from an earlier run that classified these differently
            (clean / f"{key}.txt").write_text("KEY: " + key + "\n\nold body", encoding="utf-8")
        manifest = self.dir / "clean-corpus-manifest.csv"
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(clean_corpus.main(["--pdfs", str(pdfs), "--quarantine", str(self.dir / "q"),
                                                "--docling-md", str(self.dir / "md"), "--out", str(clean),
                                                "--manifest", str(manifest)]), 0)
        self.assertEqual(Z.clean_statuses(manifest), {"good": "GOOD", "junk": "JUNK", "gone": "MISSING"})
        self.assertEqual(sorted(p.name for p in clean.iterdir()), ["good.txt"])

    def test_docling_holds_back_a_landing_page_and_removes_its_stale_clean_text(self):
        import docling_extract_corpus as D

        class Converter:
            def __init__(self, md):
                self.md = md

            def convert(self, _path):
                return type("R", (), {"document": type("Doc", (), {"export_to_markdown": lambda s: self.md})()})()

        md_dir, clean = self.dir / "md", self.dir / "clean"
        md_dir.mkdir()
        clean.mkdir()
        (clean / "junk.txt").write_text("stale", encoding="utf-8")
        landing = "A Study of Things\n\nPurchase PDF to read the full article.\n\n" + "abstract words " * 200
        body = ("# Introduction\n\n" + "word " * 1100 + "\n\n## Methods\n\n" + "word " * 1100
                + "\n\n## Results\n\n" + "word " * 1100 + "\n\n## Discussion\n\nend.\n")
        ok, _, wrote = D.extract_one(Converter(landing), self.dir / "junk.pdf", md_dir, clean)
        self.assertEqual((ok, wrote), (True, False))
        self.assertTrue((md_dir / "junk.md").is_file())  # the checkpoint and clean_corpus.py's input
        self.assertFalse((clean / "junk.txt").exists())
        ok, _, wrote = D.extract_one(Converter(body), self.dir / "good.pdf", md_dir, clean)
        self.assertEqual((ok, wrote), (True, True))
        self.assertTrue((clean / "good.txt").is_file())

    def run_attach(self, *argv):
        import zotero_attach as A
        import zotero_attach_cleantext as AC
        bib = self.dir / "refs.bib"
        bib.write_text("@article{good, title={Good}}\n@article{junk, title={Junk}}\n@article{gone, title={Gone}}\n",
                       encoding="utf-8")
        out = io.StringIO()
        with mock.patch.object(Z, "BIB_DIR", bib), \
             mock.patch.object(A, "web_match", return_value=("ITEM0001", "T")), \
             mock.patch.object(A, "has_children", return_value=(False, False, set())), \
             contextlib.redirect_stdout(out):
            code = AC.main(["--clean", str(self.dir / "clean"), "--manifest", str(self.dir / "clean-corpus-manifest.csv"),
                            *argv])
        return code, out.getvalue()

    def test_attach_cleantext_plans_only_manifest_eligible_text(self):
        clean = self.dir / "clean"
        clean.mkdir()
        for key in ("good", "junk", "gone"):
            (clean / f"{key}.txt").write_text("text", encoding="utf-8")
        write_manifest(self.dir, good="PARTIAL", junk="JUNK")
        code, out = self.run_attach()
        self.assertEqual(code, 0)
        self.assertIn("PLAN   good", out)
        self.assertNotIn("PLAN   junk", out)
        self.assertNotIn("PLAN   gone", out)
        code, out = self.run_attach("junk", "gone")
        self.assertIn("HELD   junk", out)
        self.assertIn("HELD   gone", out)
        self.assertNotIn("PLAN", out)

    def test_attach_cleantext_without_a_manifest_attaches_nothing(self):
        (self.dir / "clean").mkdir()
        (self.dir / "clean" / "good.txt").write_text("text", encoding="utf-8")
        code, out = self.run_attach()
        self.assertEqual(code, 2)
        self.assertIn("clean_corpus.py", out)
        self.assertNotIn("PLAN", out)

    def test_index_reads_neither_junk_clean_text_nor_the_extracted_folder(self):
        clean = self.dir / "clean"
        clean.mkdir()
        (clean / "junk.txt").write_text("Purchase PDF to read the full article.", encoding="utf-8")
        (clean / "unlisted.txt").write_text("Text no manifest row speaks for.", encoding="utf-8")
        manifest = write_manifest(self.dir, junk="JUNK")
        with mock.patch.object(zotero_index, "MANIFEST", manifest), \
             mock.patch.object(zotero_index, "CLEAN_DIRS", [clean]):
            self.assertEqual(zotero_index.fulltext_opening({"DOI": "10.1/j"}, {"10.1/j": "junk"}, {}), "")
            self.assertEqual(zotero_index.fulltext_opening({"DOI": "10.1/u"}, {"10.1/u": "unlisted"}, {}), "")
        with mock.patch.object(zotero_index, "MANIFEST", self.dir / "absent.csv"), \
             mock.patch.object(zotero_index, "CLEAN_DIRS", [clean]):
            self.assertEqual(zotero_index.fulltext_opening({"DOI": "10.1/u"}, {"10.1/u": "unlisted"}, {}), "")
        # extracted/ holds text no kit script writes and no gate classified.
        self.assertEqual(zotero_index.CLEAN_DIRS, [Z.PAPERS / "clean"])


class ShippedForm(unittest.TestCase):
    def test_every_cli_script_prints_help_without_site_packages(self):
        for name in CLI_SCRIPTS:
            r = subprocess.run([sys.executable, "-X", "utf8", "-I", "-S", str(SCRIPTS / name), "--help"],
                               capture_output=True, text=True, timeout=60)
            self.assertEqual(r.returncode, 0, f"{name}: {r.stderr[:200]}")
            self.assertIn("usage:", r.stdout)

    def test_no_personal_or_machine_specific_values_remain(self):
        banned = ("@gmail.com", "YT45LR7Q", "Developer/narration", ".env.production", "UCL", "SURFdrive", "—")
        for name in ZOTERO_SCRIPTS:
            text = (SCRIPTS / name).read_text(encoding="utf-8")
            for token in banned:
                self.assertNotIn(token, text, f"{name} still carries {token!r}")

class PlanLibrary:
    """A library every plan pass can act on, behind a fake Z.zreq and a fake
    urlopen. Every request is recorded; a write is answered as a success, so a
    leaked write is recorded rather than crashing the run before it is seen."""

    ITEM = {"key": "ITEM0001", "version": 5, "itemType": "journalArticle", "title": "A Study of Things",
            "DOI": "", "abstractNote": "", "creators": [{"lastName": "Smith"}], "collections": [], "tags": [],
            "dateAdded": "2099-01-01T00:00:00Z"}

    def __init__(self):
        self.calls = []

    def items(self):
        twin = dict(self.ITEM, key="ITEM0002", DOI="10.1/dup", abstractNote="Has one.")
        return [{"key": i["key"], "version": i["version"], "data": dict(i), "meta": {"numChildren": 1}}
                for i in (self.ITEM, twin, dict(twin, key="ITEM0003"))]

    def answer(self, method, url):
        if method != "GET":
            return OK_WRITE
        page = "start=0" in url or "start=" not in url
        if "127.0.0.1" in url:  # the client's local API, read by the match index
            return [{"data": dict(self.ITEM, DOI="10.1000/ABC.123")}] if page else []
        if "api.crossref.org/works?" in url:
            return {"message": {"items": [{"title": ["A Study of Things"], "DOI": "10.1/found",
                                           "author": [{"family": "Smith"}]}]}}
        if "api.crossref.org/works/" in url:
            return {"message": {"title": ["A Study of Things"], "author": [{"given": "Jane", "family": "Smith"}],
                                "abstract": "<jats:p>" + "An abstract long enough to be planned. " * 3 + "</jats:p>"}}
        if "api.openalex.org" in url or "/items/new?" in url:
            return {}
        path = url.split("/users/1", 1)[-1]
        if path.startswith("/collections?"):
            cols = {"ROOT0001": ("Root", False), "LEG00001": ("Old stuff", "ROOT0001")}
            return [{"data": {"key": k, "name": n, "parentCollection": p, "version": 3}}
                    for k, (n, p) in cols.items()] if page else []
        if path.startswith("/collections/"):
            return {"data": {"key": "ROOT0001", "name": "Root", "parentCollection": False, "version": 3}}
        if "/children" in path:
            return [{"key": "KID00001", "version": 1, "data": {"key": "KID00001", "itemType": "note", "note": "n"}}]
        if path.startswith("/items/ITEM"):
            return {"key": "ITEM0001", "data": dict(self.ITEM)}
        if path.startswith("/items?itemKey="):
            return [{"data": dict(self.ITEM)}]
        if path.startswith("/items?") or path.startswith("/collections/"):
            return self.items() if page else []
        raise AssertionError(f"no fixture for GET {url}")

    def zreq(self, method, path, data=None, headers=None, raw=False, tries=3, timeout=60):
        self.calls.append((method, path))
        return None, self.answer(method, path)

    def urlopen(self, request, timeout=None):
        method, url = request.get_method(), request.full_url
        self.calls.append((method, url))
        response = io.BytesIO(json.dumps(self.answer(method, url)).encode())
        response.status = 200
        return response


class WritesNeedTheWriteFlag(unittest.TestCase):
    """LESSON (AH-196): the gate test only checked that the text "--write"
    appears in each script, which stays true after the gate itself is removed.
    Each mutating pass now runs without --write against a library it could
    act on, with Z.zreq and urllib.request.urlopen intercepted (delete-old
    calls urlopen directly), and must send no POST, PATCH, PUT or DELETE."""

    WRITES = ("POST", "PATCH", "PUT", "DELETE")

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = pathlib.Path(tmp.name)
        Z._ENV_CACHE = {"ZOTERO_USER_ID": "1", "ZOTERO_API_KEY": "k"}
        self.addCleanup(setattr, Z, "_ENV_CACHE", None)
        import zotero_attach
        self.addCleanup(setattr, zotero_attach, "_INDEX", None)
        (self.dir / "refs.bib").write_text(BibAndManuscript.BIB, encoding="utf-8")
        for sub in ("pdfs", "notes", "clean"):
            (self.dir / sub).mkdir()
        (self.dir / "pdfs" / "smith2020.pdf").write_bytes(b"%PDF-1.4")
        (self.dir / "notes" / "smith2020.md").write_text("# Notes\n\nRead it.", encoding="utf-8")
        (self.dir / "clean" / "smith2020.txt").write_text("KEY: smith2020\n\nBody.", encoding="utf-8")
        self.manifest = write_manifest(self.dir, smith2020="GOOD")
        (self.dir / "fill-plan.json").write_text(json.dumps([{"key": "ITEM0001", "version": 5, "title": "T",
                                                              "fields": {"DOI": "10.1/found"}}]), encoding="utf-8")
        (self.dir / "dedupe-plan.json").write_text(json.dumps([{"doi": "10.1/dup", "title": "T", "master": "ITEM0002",
                                                                "master_children": 1,
                                                                "dupes": [{"key": "ITEM0003", "version": 5,
                                                                           "children": 1}]}]), encoding="utf-8")
        (self.dir / "cards.json").write_text(json.dumps([{"key": "ITEM0001", "tldr": "a", "finding": "b", "method": "c",
                                                          "relevance": "d", "facets": ["method:survey"]}]),
                                             encoding="utf-8")
        self.taxonomy = self.dir / "taxonomy.json"
        self.taxonomy.write_text(json.dumps({"root": "ROOT0001", "rules": [],
                                             "tree": [{"id": "A", "name": "Leaf A", "parent": None},
                                                      {"id": "B", "name": "Leaf B", "parent": None}]}),
                                 encoding="utf-8")
        (self.dir / "mapping.json").write_text(json.dumps({"ITEM0001": {"leaf": "A"}}), encoding="utf-8")
        (self.dir / "tree-keys.json").write_text(json.dumps({"A": "LEAF0001"}), encoding="utf-8")
        (self.dir / "graded.json").write_text(json.dumps([{"key": "ITEM0001", "tier": "core"}]), encoding="utf-8")
        (self.dir / "fixes.json").write_text(json.dumps([{"key": "ITEM0001", "confidence": "high",
                                                          "proposed": {"date": "2020", "venue": "Management Science"}}]),
                                             encoding="utf-8")

    def run_plan(self, module, argv, planned, **attrs):
        """Run module.main(argv) without --write; return what it printed and every request it made."""
        library, out = PlanLibrary(), io.StringIO()
        with contextlib.ExitStack() as stack:
            for name, value in attrs.items():
                target, attr = (Z, name[2:]) if name.startswith("Z_") else (module, name)
                stack.enter_context(mock.patch.object(target, attr, value, create=True))
            stack.enter_context(mock.patch.object(Z, "zreq", library.zreq))
            stack.enter_context(mock.patch("urllib.request.urlopen", library.urlopen))
            stack.enter_context(mock.patch("time.sleep"))
            stack.enter_context(contextlib.redirect_stdout(out))
            module.main(argv)
        label = f"{module.__name__} {' '.join(argv)}"
        self.assertNotIn("--write", argv)
        self.assertEqual([c for c in library.calls if c[0] in self.WRITES], [], label)
        self.assertTrue(any(c[0] == "GET" for c in library.calls), f"{label} read nothing, so it tested nothing")
        self.assertIn(planned, out.getvalue(), f"{label} planned no write:\n{out.getvalue()}")

    def test_fill_dedupe_add_and_attach_plan_without_writing(self):
        import zotero_add
        import zotero_attach
        import zotero_attach_cleantext
        self.run_plan(zotero_fill, [], "PLAN: 1 items", PLAN=self.dir / "fill-plan.json")
        self.run_plan(zotero_dedupe, [], "trash 1 duplicates", PLAN=self.dir / "dedupe-plan.json",
                      REPORT=self.dir / "dedupe-plan.md")
        self.run_plan(zotero_add, ["smith2020"], "preview smith2020", Z_BIB_DIR=self.dir / "refs.bib")
        zotero_attach._INDEX = None
        self.run_plan(zotero_attach, ["smith2020"], "-> attach PDF", Z_BIB_DIR=self.dir / "refs.bib",
                      PDF_DIRS=[self.dir / "pdfs"], NOTES=self.dir / "notes")
        zotero_attach._INDEX = None
        self.run_plan(zotero_attach_cleantext, ["--clean", str(self.dir / "clean"), "--manifest", str(self.manifest)],
                      "-> attach smith2020.txt", Z_BIB_DIR=self.dir / "refs.bib")

    def test_every_reorg_subcommand_and_enrich_plan_without_writing(self):
        state = {"STATE": self.dir, "MAPPING": self.dir / "mapping.json", "TREE_KEYS": self.dir / "tree-keys.json",
                 "CROSSCUT_KEYS": self.dir / "crosscut-keys.json"}
        tax = ["--taxonomy", str(self.taxonomy)]
        for argv, planned in (
                (["create-tree"], "WOULD CREATE  Leaf B"),
                (["apply"], "DRY-RUN: 1 memberships"),
                (["crosscut", "--name", "Core reading", "--graded", str(self.dir / "graded.json"),
                  "--tag", "core=tier:core"], "WOULD CREATE collection"),
                (["meta-apply", "--fixes", str(self.dir / "fixes.json")], "WOULD UPDATE ITEM0001"),
                (["rename", "--name", "New root"], "WOULD set name"),
                (["delete-old"], "[LEG00001]")):
            self.run_plan(zotero_reorg, tax + argv, planned, **state)
        self.assertFalse((self.dir / "crosscut-keys.json").exists())
        self.run_plan(zotero_enrich, ["apply"], "WOULD enrich ITEM0001", CARDS=self.dir / "cards.json")


if __name__ == "__main__":
    unittest.main()
