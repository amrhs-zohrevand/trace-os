"""Trace stats must not turn incomplete evidence into reassuring status."""
import contextlib
import copy
import datetime as dt
import io
import json
from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import trace_stats as ph

SHA = 'a' * 40
NOW = dt.datetime(2026, 9, 8, 12, tzinfo=dt.timezone.utc)


SUITE, PARITY, RECORDS, REGISTRIES, REVIEW, LEDGER, ACTIONS, BUILD = range(8)


def report():
    totals = {p: {'recorded': 1, 'missing': 2, 'not-applicable': 3, 'unknown': 0} for p in ph.PRINCIPLES}
    outcomes = [
        {'run': 12, 'failures': 0, 'errors': 0, 'skipped': 1,
         'skip_reasons': ['archive is unavailable']},
        {'applicable': True, 'checked': 3, 'mismatched': []},
        {'evaluated': 2, 'with_problems': []},
        {'rows': {'acceptances': 3, 'intake': 4, 'transitions': 1}, 'problems': [], 'notes': []},
        {'commits_since_epoch': 10, 'unreviewed': 7,
         'reviewed_by_a_model': 3, 'accepted_by_a_person': 0,
         'accepted_by_a_delegated_actor': 0},
        {'states': {'open': 4, 'closed': 2}},
        {'commits_since_baseline': 6, 'commits_with_a_missing_record': 2, 'totals': totals,
         'ledger': {'closed_without_verification': 2, 'fixed_without_recheck': 0},
         'intake': {'rows': 4, 'checked_nothing_stated': 1}, 'transitions': {'rows': 1}},
        {'bst_staleness': {'status': 'unavailable', 'reason': 'TeX not installed'}},
    ]
    return {'schema_version': 2, 'generated_at': '2026-09-08T11:00:00Z',
            'evaluated_commit': SHA, 'dirty': False, 'shared_mode': False,
            'checks': [dict(name=name, status='info' if i in (REVIEW, LEDGER, ACTIONS) else 'pass',
                            outcome=outcomes[i]) for i, name in enumerate(ph.CHECKS)]}


class HealthRendering(unittest.TestCase):
    def render(self, data=None, **kwargs):
        return ph.render(data or report(), 'report.json',
                         **dict({'head': SHA, 'dirty': False, 'now': NOW}, **kwargs))

    def test_skips_and_accountability_states_remain_separate(self):
        text = self.render()
        for expected in ('12 run', '1 skipped', 'archive is unavailable',
                         'TeX not installed', '7 unreviewed', '3 reviewed by a model',
                         '3 installed copies checked; 0 mismatched',
                         '0 accepted by a person', '0 accepted by a delegated actor',
                         '2 closed; 0 verified', '0 unresolved',
                         '3 acceptances, 4 intake, 1 transitions rows valid',
                         '6 commits since baseline; 2 with a missing record',
                         '| Track records | 1 | 2 | 3 | 0 |',
                         '2 closed without verification', '1 with nothing checked',
                         'PDF build success', 'Matches this clean checkout'):
            self.assertIn(expected, text)

    def test_failures_errors_and_recurrence_remain_visible(self):
        data = report()
        for i, status, reason in [(SUITE, 'fail', 'assertion failed'),
                                  (RECORDS, 'error', 'record unreadable'),
                                  (REGISTRIES, 'fail', '1 malformed registry row(s)'),
                                  (LEDGER, 'warning', 'a fix recurred')]:
            data['checks'][i].update(status=status)
            data['checks'][i]['outcome']['reason'] = reason
        data['checks'][LEDGER]['outcome'].update(warning='a fix recurred',
                                                 states={'recurred': 1, 'unresolved': 1})
        text = self.render(data)
        for expected in ('FAIL', 'assertion failed', 'ERROR', 'record unreadable',
                         'malformed registry row', 'WARNING', 'a fix recurred',
                         '1 recurred; 1 unresolved'):
            self.assertIn(expected, text)
        self.assertNotIn('No failure, execution error', text)

    def test_old_commit_old_time_dirty_and_unknown_are_never_fresh(self):
        cases = [(dict(head='b' * 40), 'differs from this checkout'),
                 (dict(now=NOW + dt.timedelta(days=2)), 'older than 24 hours'),
                 (dict(dirty=True), 'local edits'),
                 (dict(head=None, dirty=None), 'could not be compared')]
        for kwargs, expected in cases:
            with self.subTest(kwargs=kwargs):
                text = self.render(**kwargs)
                self.assertIn(expected, text)
                self.assertNotIn('Matches this clean checkout', text)
        data = report()
        data['dirty'] = True
        self.assertIn('local edits', self.render(data))

    def test_partial_duplicate_and_unsupported_reports_are_invalid(self):
        cases = [dict(report(), schema_version=3), dict(report(), checks=[])]
        wrong_status = report()
        wrong_status['checks'][0]['status'] = 'info'
        cases.append(wrong_status)
        duplicate = report()
        duplicate['checks'][0] = copy.deepcopy(duplicate['checks'][1])
        cases.append(duplicate)
        for data in cases:
            with self.subTest(data=data):
                with self.assertRaises(ValueError):
                    self.render(data)

    def test_a_report_carrying_the_skill_handoff_check_reads_it(self):
        # Added with kit 0.1.0-rc8. The check is informational, so its states
        # belong in the table; the limit travels with them.
        data = report()
        data['checks'].append({'name': 'skill handoff', 'status': 'info',
                               'outcome': {'checked': 9, 'handoff_stated': 7,
                                           'handoff_not_stated': 2, 'states': []}})
        text = self.render(data)
        self.assertIn('| skill handoff | info |', text)
        self.assertIn('9 skills read; 2 state no handoff to the record', text)
        self.assertIn('not whether one was followed', text)

    def test_a_report_carrying_the_shared_folder_check_reads_it(self):
        # Added after 0.1.0-rc20; a report written before it renders without it.
        data = report()
        data['checks'].append({'name': 'shared folder', 'status': 'info',
                               'outcome': {'applicable': True, 'state': 'not connected',
                                           'next': 'ask the person where the folder is'}})
        text = self.render(data)
        self.assertIn('| shared folder | info | not connected on this machine; next: ask the person', text)
        data = report()
        data['checks'].append({'name': 'shared folder', 'status': 'info',
                               'outcome': {'applicable': False, 'reason': 'no script'}})
        self.assertIn('Not applicable: no script', self.render(data))

    def test_an_rc8_report_naming_the_old_state_key_still_renders(self):
        # LESSON: a stored report is a record of a run. rc8 wrote the state as
        # handoff_missing; rc9 renamed it to handoff not stated, and rejecting
        # last week's report would make a rename look like a broken report.
        data = report()
        data['checks'].append({'name': 'skill handoff', 'status': 'info',
                               'outcome': {'checked': 9, 'handoff_stated': 7,
                                           'handoff_missing': 2, 'states': []}})
        text = self.render(data)
        self.assertIn('9 skills read; 2 state no handoff to the record', text)

    def test_a_report_written_before_that_check_existed_still_renders(self):
        # LESSON: a stored report is a record of a run. A renderer that refused
        # last week's report would make the absence of a later check look like a
        # broken report.
        text = self.render(report())
        self.assertNotIn('skill handoff', text)
        self.assertIn('12 run', text)

    def test_material_no_intake_row_names_is_shown_and_an_older_report_still_renders(self):
        # rc22: the report counts what no intake row names; a report written
        # before it existed renders as it did, with no clause invented.
        self.assertNotIn('named by no row', self.render())
        data = report()
        data['checks'][ACTIONS]['outcome']['intake']['unnamed_material'] = {
            'sources': {'total': 5, 'unnamed': 2, 'examples': []},
            'shared_inventory': {'total': 0, 'unnamed': 0, 'examples': []},
            'snapshots': {'total': 1, 'unnamed': 1, 'examples': []}}
        self.assertIn('1 with nothing checked; named by no row: 2 data files added under sources/, '
                      '0 shared-inventory files, 1 snapshots', self.render(data))

    def test_skill_handoff_counts_that_do_not_sum_are_not_inferred(self):
        data = report()
        data['checks'].append({'name': 'skill handoff', 'status': 'info',
                               'outcome': {'checked': 9, 'handoff_stated': 7,
                                           'handoff_not_stated': 1}})
        with self.assertRaises(ValueError):
            self.render(data)

    def test_an_absent_skills_tree_is_reported_as_not_applicable(self):
        data = report()
        data['checks'].append({'name': 'skill handoff', 'status': 'info',
                               'outcome': {'applicable': False, 'checked': 0,
                                           'skip_reasons': ['no .skills directory']}})
        text = self.render(data)
        self.assertIn('Not applicable: no skills directory', text)
        self.assertIn('Skipped (skill handoff): no .skills directory', text)

    def test_a_check_this_renderer_does_not_know_is_still_invalid(self):
        data = report()
        data['checks'].append({'name': 'something new', 'status': 'pass', 'outcome': {}})
        with self.assertRaises(ValueError):
            self.render(data)

    def test_version_one_report_without_skill_parity_remains_readable(self):
        data = report()
        data['schema_version'] = 1
        data['checks'] = [c for c in data['checks'] if c['name'] != 'installed skill parity']
        text = self.render(data)
        self.assertNotIn('installed skill parity', text)
        self.assertIn('12 run', text)

    def test_missing_or_contradictory_counts_are_not_inferred_as_zero(self):
        for check_index, key, value in [(SUITE, 'run', 0), (SUITE, 'failures', 2),
                                        (RECORDS, 'with_problems', ['bad']),
                                        (REGISTRIES, 'problems', ['bad row']),
                                        (REVIEW, 'commits_since_epoch', 99),
                                        (REVIEW, 'unreviewed', None),
                                        (ACTIONS, 'totals', {'T': {}}),
                                        (ACTIONS, 'commits_with_a_missing_record', None)]:
            data = report()
            data['checks'][check_index]['outcome'][key] = value
            with self.subTest(key=key):
                with self.assertRaises(ValueError):
                    self.render(data)

    def test_future_and_timezone_free_timestamps_are_invalid(self):
        for time in ('2026-09-09T11:00:00Z', '2026-09-08T11:00:00'):
            data = report()
            data['generated_at'] = time
            with self.assertRaises(ValueError):
                self.render(data)

    def test_report_text_cannot_inject_links_html_or_table_rows(self):
        data = report()
        data['checks'][0]['outcome']['skip_reasons'] = [
            '<script>bad</script> | [click](https://invalid.example)\n# New heading']
        text = self.render(data)
        self.assertNotIn('<script>', text)
        self.assertNotIn('[click]', text)
        self.assertNotIn('\n# New heading', text)
        self.assertIn('\\|', text)

    def test_missing_or_malformed_report_replaces_an_old_overview(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / 'missing.json'
            target = Path(directory) / 'health.md'
            for content in (None, '{', json.dumps({'schema_version': 1})):
                if content is not None:
                    source.write_text(content)
                target.write_text('old passing overview')
                with contextlib.redirect_stdout(io.StringIO()):
                    code = ph.main(['--report', str(source), '--out', str(target)])
                self.assertEqual(code, 2)
                self.assertIn('UNAVAILABLE', target.read_text())
                self.assertNotIn('old passing overview', target.read_text())

    def test_what_the_check_left_unchecked_or_unrun_is_shown(self):
        # LESSON (AH-203): a skipped bibliography-style check and the project
        # test files the suite did not run were in the report and nowhere in
        # the overview, and a build checker that crashed had no wording of its own.
        data = report()
        data['checks'][BUILD]['outcome']['bst_staleness'] = {
            'status': 'not_checked',
            'reason': 'scripts/check_build_config.py has no bibliography-style comparison'}
        data['checks'][SUITE]['outcome'].update(
            not_run={'count': 2, 'files': ['tests/test_own.py', 'tests/pkg/test_more.py'],
                     'reason': "the project's own test files; the project runs them itself"},
            kit_tests_absent=['tests/test_gone.py'], installation_state='.trace-os/install.json')
        text = self.render(data)
        for expected in ('Skipped (bibliography-style staleness): scripts/check\\_build\\_config.py '
                         'has no bibliography-style comparison',
                         '12 run, 0 failures, 0 errors, 1 skipped; 2 project test file(s) not run',
                         'Not run (portable test suite): 2 test file(s) under tests/, '
                         "the project's own test files; the project runs them itself: "
                         'tests/test\\_own.py, tests/pkg/test\\_more.py',
                         'Absent (portable test suite): 1 kit test module(s) listed in '
                         '.trace-os/install.json: tests/test\\_gone.py'):
            self.assertIn(expected, text)
        data = report()
        data['checks'][BUILD].update(status='error')
        data['checks'][BUILD]['outcome']['reason'] = 'the build checker did not complete: KeyError'
        text = self.render(data)
        self.assertIn('| build configuration | error | Execution error, configuration not checked: '
                      'the build checker did not complete: KeyError |', text)
        self.assertNotIn('Configuration checked', text)
        data['checks'][BUILD]['outcome'].pop('reason')
        self.assertIn('Execution error, configuration not checked: reason not recorded', self.render(data))

    def test_source_report_cannot_be_overwritten(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / 'report.json'
            original = json.dumps(report())
            source.write_text(original)
            with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
                ph.main(['--report', str(source), '--out', str(source)])
            self.assertEqual(source.read_text(), original)


if __name__ == '__main__':
    unittest.main()
