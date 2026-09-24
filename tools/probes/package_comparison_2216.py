"""Fork-only packaging and verification for nasa/trick#2216; AI-assisted."""

import json
import os
from pathlib import Path
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[2]
BASE = '8b25adf131b4886dfc11f1229c1acce621646326'
BRANCH = 'fix/trickops-comparison-retry-2216'
SOURCE = 'share/trick/trickops/TrickWorkflow.py'
TESTS = 'share/trick/trickops/tests/ut_TrickWorkflow.py'

NEW_TESTS = '''class TrickWorkflowComparisonRetryTestCase(unittest.TestCase):
    """Check that comparison results reflect each attempt's files."""

    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.test_file = Path(directory.name) / 'test.dat'
        self.baseline_file = Path(directory.name) / 'baseline.dat'
        self.comparison = TrickWorkflow.Comparison(
            str(self.test_file), str(self.baseline_file))

    def test_missing_files_recover_when_both_arrive(self):
        self.assertEqual(self.comparison.compare(), Job.Status.FAILED)
        self.assertEqual(self.comparison.missing,
                         [str(self.test_file), str(self.baseline_file)])
        self.test_file.write_bytes(b'same')
        self.baseline_file.write_bytes(b'same')
        self.assertEqual(self.comparison.compare(), Job.Status.SUCCESS)
        self.assertEqual(self.comparison.missing, [])

    def test_retries_report_only_files_still_missing(self):
        for _ in range(2):
            self.assertEqual(self.comparison.compare(), Job.Status.FAILED)
            self.assertEqual(self.comparison.missing,
                             [str(self.test_file), str(self.baseline_file)])
        self.test_file.write_bytes(b'same')
        self.assertEqual(self.comparison.compare(), Job.Status.FAILED)
        self.assertEqual(self.comparison.missing, [str(self.baseline_file)])

    def test_success_then_missing_then_mismatch_then_recovery(self):
        self.test_file.write_bytes(b'same')
        self.baseline_file.write_bytes(b'same')
        self.assertEqual(self.comparison.compare(), Job.Status.SUCCESS)
        self.test_file.unlink()
        self.assertEqual(self.comparison.compare(), Job.Status.FAILED)
        self.assertEqual(self.comparison.missing, [str(self.test_file)])
        self.test_file.write_bytes(b'different')
        self.assertEqual(self.comparison.compare(), Job.Status.FAILED)
        self.assertEqual(self.comparison.missing, [])
        self.test_file.write_bytes(b'same')
        self.assertEqual(self.comparison.compare(), Job.Status.SUCCESS)
        self.assertEqual(self.comparison.missing, [])

'''

DRIVER = '''import json, sys, unittest
import ut_TrickWorkflow as module

def flatten(suite):
    for item in suite:
        if isinstance(item, unittest.TestSuite):
            yield from flatten(item)
        else:
            yield item

registered = list(flatten(unittest.TestSuite(module.suite())))
retry_tests = [case for case in registered
               if isinstance(case, module.TrickWorkflowComparisonRetryTestCase)]
assert len(retry_tests) == 3, 'Retry tests must be registered in suite()'
if sys.argv[1] == 'regressions':
    selected = retry_tests
else:
    selected = [case for case in registered
                if isinstance(case, module.TrickWorkflowTestCase)
                and (case._testMethodName.startswith('test_comparison')
                     or case._testMethodName.startswith('test_run_compare')
                     or case._testMethodName == 'test_compare')]
    assert len(selected) >= 3, 'Expected existing comparison tests'
print('SELECTED_TESTS ' + json.dumps([case.id() for case in selected]), flush=True)
result = unittest.TextTestRunner(verbosity=2).run(unittest.TestSuite(selected))
print('RESULT_JSON ' + json.dumps({
    'tests': result.testsRun, 'failures': len(result.failures),
    'errors': len(result.errors), 'skips': len(result.skipped),
}), flush=True)
sys.exit(0 if result.wasSuccessful() else 1)
'''


def git(*args, cwd=ROOT):
    return subprocess.run(['git', *args], cwd=cwd, check=True,
                          capture_output=True, text=True, timeout=90).stdout.strip()


def replace_once(text, before, after):
    if text.count(before) != 1:
        raise RuntimeError('Expected exactly one edit anchor: ' + repr(before))
    return text.replace(before, after, 1)


def main():
    if os.environ.get('GITHUB_REPOSITORY') != 'kvnloo/trick':
        raise RuntimeError('This packaging helper is restricted to kvnloo/trick')
    candidate = ROOT.parent / 'comparison-2216-candidate'
    reports = ROOT / 'probe-results'
    reports.mkdir(exist_ok=True)
    git('worktree', 'add', '--detach', str(candidate), BASE)
    production = candidate / SOURCE
    test_file = candidate / TESTS
    original = production.read_text()
    tests = test_file.read_text()
    tests = replace_once(tests, 'import unittest\n',
                         'import unittest\nimport tempfile\nfrom pathlib import Path\n')
    tests = replace_once(tests, '    return (suites)\n',
                         '    suites.append(unittest.TestLoader().loadTestsFromTestCase(TrickWorkflowComparisonRetryTestCase))\n    return (suites)\n')
    tests = replace_once(tests, 'class TrickWorkflowSingleRunTestCase',
                         NEW_TESTS + 'class TrickWorkflowSingleRunTestCase')
    test_file.write_text(tests)
    tests_dir = candidate / 'share/trick/trickops/tests'

    def check(label, group, expected_failures):
        completed = subprocess.run([sys.executable, '-c', DRIVER, group],
                                   cwd=tests_dir, capture_output=True,
                                   text=True, timeout=120)
        output = completed.stdout + completed.stderr
        (reports / (label + '.log')).write_text(output)
        print('=== ' + label + ' ===\n' + output, flush=True)
        markers = [line[len('RESULT_JSON '):] for line in completed.stdout.splitlines()
                   if line.startswith('RESULT_JSON ')]
        if len(markers) != 1:
            raise RuntimeError(label + ': missing test summary')
        summary = json.loads(markers[0])
        if (summary['failures'] != expected_failures or summary['errors'] != 0
                or summary['skips'] != 0
                or (group == 'regressions' and summary['tests'] != 3)
                or completed.returncode != (1 if expected_failures else 0)):
            raise RuntimeError(label + ': unexpected outcome ' + str(summary))
        return summary

    results = {'baseline_commit': BASE, 'python': sys.version}
    results['baseline_regressions'] = check('baseline-regressions', 'regressions', 3)
    results['baseline_existing'] = check('baseline-existing', 'existing', 0)
    needle = '            for hs in [self.test_data, self.baseline_data]:\n'
    production.write_text(replace_once(original, needle,
                                      '            self.missing = []\n' + needle))
    results['candidate_regressions'] = check('candidate-regressions', 'regressions', 0)
    results['candidate_existing'] = check('candidate-existing', 'existing', 0)
    git('diff', '--check', cwd=candidate)
    paths = set(git('diff', '--name-only', cwd=candidate).splitlines())
    if paths != {SOURCE, TESTS}:
        raise RuntimeError('Unexpected changed paths: ' + str(paths))
    stats = {line.split('\t')[2]: line.split('\t')[:2] for line in
             git('diff', '--numstat', cwd=candidate).splitlines()}
    if stats[SOURCE] != ['1', '0'] or int(stats[TESTS][0]) > 65 or stats[TESTS][1] != '0':
        raise RuntimeError('Diff exceeds the intended one-line fix and focused tests')
    patch = subprocess.run(['git', 'diff', '--', SOURCE, TESTS], cwd=candidate,
                           check=True, capture_output=True, timeout=30).stdout
    (reports / 'fix-with-regression-tests.patch').write_bytes(patch)
    git('diff', '--stat', cwd=candidate)
    # Refuse to update a branch that advanced while this job ran.
    remote = git('ls-remote', '--heads', 'origin', 'refs/heads/' + BRANCH)
    if not remote or remote.split()[0] != BASE:
        raise RuntimeError('Destination branch is not at the expected baseline')
    git('add', '--', SOURCE, TESTS, cwd=candidate)
    git('-c', 'user.name=kvnloo',
        '-c', 'user.email=7121943+kvnloo@users.noreply.github.com',
        'commit', '-m', 'Fix TrickOps comparison state on retry (#2216)',
        '-m', 'Reset missing-file diagnostics before checking the current files.\n'
              'Add three filesystem regressions to the existing TrickOps suite.\n\n'
              'AI assistance: ChatGPT generated the fix and tests.\n'
              'Validation ran in GitHub Actions on the contributor fork.', cwd=candidate)
    commit = git('rev-parse', 'HEAD', cwd=candidate)
    if git('rev-parse', 'HEAD^', cwd=candidate) != BASE:
        raise RuntimeError('Candidate must be a single commit on the upstream baseline')
    git('push', 'origin', commit + ':refs/heads/' + BRANCH)
    results.update({'commit': commit, 'branch': BRANCH, 'changed_paths': sorted(paths),
                    'diff_check': 'passed', 'registered_regressions': 3,
                    'scope': 'Registered retry tests and existing comparison tests; full modules and real files',
                    'not_run': ['complete TrickOps unit/doc suite', 'simulation build', 'hardware tests'],
                    'workflow_url': 'https://github.com/kvnloo/trick/actions/runs/' + os.environ['GITHUB_RUN_ID']})
    (reports / 'results.json').write_text(json.dumps(results, indent=2) + '\n')
    print('PUBLISHED ' + json.dumps(results), flush=True)


if __name__ == '__main__':
    main()
