"""Exercise the reusable workflow's actual shell blocks using synthetic Git data."""
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest


WORKFLOW = Path(__file__).resolve().parents[1] / '.github/workflows/pii-check.yml'
MARKER = 'SYNTHETIC_PII_TOKEN'


def step(name):
    source = WORKFLOW.read_text()
    section = source.split('      - name: ' + name + '\n', 1)[1]
    body = section.split('        run: |\n', 1)[1]
    lines = []
    for line in body.splitlines():
        if line and not line.startswith('          '):
            break
        lines.append(line[10:])
    return '\n'.join(lines) + '\n'


class ScannerTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.repo = self.root / 'repo'
        self.repo.mkdir()
        self.env = {k: v for k, v in os.environ.items() if not k.startswith('GIT_')}
        self.env.update(GIT_CONFIG_GLOBAL=os.devnull, GIT_CONFIG_NOSYSTEM='1',
                        GIT_AUTHOR_NAME='Synthetic Author',
                        GIT_AUTHOR_EMAIL='author@example.invalid',
                        GIT_COMMITTER_NAME='Synthetic Committer',
                        GIT_COMMITTER_EMAIL='committer@example.invalid',
                        RUNNER_TEMP=str(self.root / 'runner'),
                        GITHUB_ENV=str(self.root / 'env'))
        Path(self.env['RUNNER_TEMP']).mkdir()
        self.git('init', '-q', '-b', 'main')
        self.git('config', 'commit.gpgsign', 'false')
        self.base = self.commit()
        self.patterns = self.root / 'patterns'
        self.patterns.write_text(MARKER + '\n')

    def git(self, *args, env=None):
        result = subprocess.run(['git', *args], cwd=self.repo,
                                env=self.env | (env or {}), text=True,
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        self.assertEqual(0, result.returncode, result.stderr)
        return result.stdout.strip()

    def commit(self, message='Synthetic safe message', **identity):
        self.git('commit', '-q', '--allow-empty', '-m', message, env=identity)
        return self.git('rev-parse', 'HEAD')

    def run_step(self, name, **env):
        return subprocess.run(['bash', '-c', step(name)], cwd=self.repo,
                              env=self.env | env, text=True,
                              stdout=subprocess.PIPE, stderr=subprocess.STDOUT)

    def scan(self, range_=None, **env):
        return self.run_step('Scan', PII_PATTERN_FILE=str(self.patterns),
                             PII_RANGE=range_ or self.base + '..HEAD', **env)

    def assert_hit(self, result, label='commit', sha=None):
        self.assertEqual(1, result.returncode, result.stdout)
        self.assertIn(label, result.stdout)
        self.assertIn('[REDACTED]', result.stdout)
        self.assertNotIn(MARKER.lower(), result.stdout.lower())
        self.assertIn('match(es) found', result.stdout)
        if sha:
            self.assertIn(sha, result.stdout)

    def test_identity_fields(self):
        for field in ('GIT_AUTHOR_NAME', 'GIT_AUTHOR_EMAIL',
                      'GIT_COMMITTER_NAME', 'GIT_COMMITTER_EMAIL'):
            with self.subTest(field=field):
                before = self.git('rev-parse', 'HEAD')
                value = MARKER if field.endswith('NAME') else MARKER + '@example.invalid'
                sha = self.commit(**{field: value})
                self.assert_hit(self.scan(before + '..HEAD'), sha=sha)

    def test_multiline_message_and_trailer(self):
        for message in ('Safe subject\n\nSafe body\n' + MARKER + '\nLast line',
                        'Safe subject\n\nCo-authored-by: Synthetic <' + MARKER + '@example.invalid>'):
            with self.subTest(message_type=message.splitlines()[-1].split(':')[0]):
                before = self.git('rev-parse', 'HEAD')
                sha = self.commit(message)
                self.assert_hit(self.scan(before + '..HEAD'), sha=sha)

    def test_case_insensitive_identity(self):
        self.commit(GIT_AUTHOR_NAME=MARKER.lower())
        self.assert_hit(self.scan())

    def test_raw_identity_is_not_hidden_by_mailmap(self):
        (self.repo / '.mailmap').write_text(
            'Safe <safe@example.invalid> Synthetic Author <' + MARKER + '@example.invalid>\n')
        self.commit(GIT_AUTHOR_EMAIL=MARKER + '@example.invalid')
        self.assert_hit(self.scan())

    def test_safe_noreply_and_multiline_message(self):
        self.commit('Safe subject\n\nSafe body\n\nCo-authored-by: Synthetic <safe@example.invalid>',
                    GIT_AUTHOR_EMAIL='12345+synthetic@users.noreply.github.com',
                    GIT_COMMITTER_EMAIL='12345+synthetic@users.noreply.github.com')
        result = self.scan()
        self.assertEqual(0, result.returncode, result.stdout)
        self.assertIn('pii-check: clean', result.stdout)

    def test_merge_traversal_includes_side_branch(self):
        self.git('checkout', '-q', '-b', 'side')
        sha = self.commit(GIT_COMMITTER_NAME=MARKER)
        self.git('checkout', '-q', 'main')
        self.commit()
        self.git('merge', '-q', '--no-ff', 'side', '-m', 'Synthetic merge')
        self.assert_hit(self.scan(), sha=sha)

    def test_push_range_excludes_old_metadata(self):
        before = self.commit(GIT_AUTHOR_NAME=MARKER)
        self.commit()
        result = self.scan(before + '..HEAD')
        self.assertEqual(0, result.returncode, result.stdout)

    def test_pr_range_includes_branch_metadata(self):
        self.git('update-ref', 'refs/remotes/origin/main', self.base)
        self.commit(GIT_COMMITTER_EMAIL=MARKER + '@example.invalid')
        self.assert_hit(self.scan('origin/main...HEAD'))

    def test_added_line_and_path_are_redacted(self):
        for path, content, label in [('safe.txt', MARKER, 'added-line'),
                                     (MARKER + '.txt', 'safe', 'path')]:
            with self.subTest(label=label):
                before = self.git('rev-parse', 'HEAD')
                (self.repo / path).write_text(content + '\n')
                self.git('add', path)
                self.commit()
                self.assert_hit(self.scan(before + '..HEAD'), label)

    def test_removed_lines_are_not_scanned(self):
        path = self.repo / 'safe.txt'
        path.write_text(MARKER + '\n')
        self.git('add', '.')
        before = self.commit()
        path.write_text('safe\n')
        self.git('add', '.')
        self.commit()
        result = self.scan(before + '..HEAD')
        self.assertEqual(0, result.returncode, result.stdout)

    def test_existing_allow_behavior(self):
        (self.repo / '.pii-allow').write_text('SYNTHETIC_PII_.*\n')
        self.commit(MARKER)
        result = self.scan()
        self.assertEqual(0, result.returncode, result.stdout)

    def test_invalid_range_fails_closed(self):
        result = self.scan('missing-ref..HEAD')
        self.assertEqual(1, result.returncode)
        self.assertIn('cannot resolve commit range', result.stdout)
        self.assertNotIn('pii-check: clean', result.stdout)

    def test_git_failures_fail_closed(self):
        self.commit()
        real_git = shutil.which('git')
        wrappers = self.root / 'bin'
        wrappers.mkdir()
        wrapper = wrappers / 'git'
        wrapper.write_text('#!/bin/bash\n'
                           'if [ "$1" = "$FAIL_COMMAND" ] && '
                           '{ [ -z "$FAIL_SECOND" ] || [ "$2" = "$FAIL_SECOND" ]; }; then\n'
                           '  exit 2\nfi\nexec "$REAL_GIT" "$@"\n')
        wrapper.chmod(0o755)
        for command, second in [('log', ''), ('log', '-1'), ('diff', ''), ('diff', '--name-only')]:
            with self.subTest(command=command, second=second):
                # The path scan places the range before --name-only.
                if second == '--name-only':
                    wrapper.write_text('#!/bin/bash\nfor arg in "$@"; do\n'
                                       'if [ "$arg" = "--name-only" ]; then exit 2; fi\ndone\n'
                                       'exec "$REAL_GIT" "$@"\n')
                result = self.scan(PATH=str(wrappers) + os.pathsep + self.env['PATH'],
                                   REAL_GIT=real_git, FAIL_COMMAND=command, FAIL_SECOND=second)
                self.assertEqual(1, result.returncode, result.stdout)
                self.assertIn('::error::', result.stdout)
                self.assertNotIn('pii-check: clean', result.stdout)

    def test_invalid_scan_patterns_fail_closed(self):
        self.patterns.write_text('[\n')
        self.commit()
        result = self.scan()
        self.assertEqual(1, result.returncode)
        self.assertIn('grep failed', result.stdout)
        self.assertNotIn('pii-check: clean', result.stdout)

    def test_pattern_loading(self):
        for patterns, success in [('', False), (' \n\t\n', False), ('[', False),
                                  ('\n' + MARKER + '\n\n', True)]:
            with self.subTest(success=success, empty=not patterns):
                result = self.run_step('Load patterns', PII_PATTERNS=patterns)
                self.assertEqual(0 if success else 1, result.returncode, result.stdout)
                self.assertNotIn(MARKER, result.stdout)
                if success:
                    self.assertEqual(MARKER + '\n',
                                     (Path(self.env['RUNNER_TEMP']) / 'patterns').read_text())
                    self.assertEqual(0o600, (Path(self.env['RUNNER_TEMP']) / 'patterns').stat().st_mode & 0o777)
                self.assertFalse((Path(self.env['RUNNER_TEMP']) / 'patterns.raw').exists())

    def test_event_ranges(self):
        self.commit()
        cases = [('push', self.base, '', self.base + '..HEAD'),
                 ('push', '0' * 40, '', 'HEAD~1..HEAD'),
                 ('push', 'missing', '', 'HEAD~1..HEAD'),
                 ('pull_request', '', 'main', 'origin/main...HEAD'),
                 ('pull_request_target', '', 'main', 'origin/main...HEAD'),
                 ('pull_request', '', 'missing', 'HEAD~1..HEAD')]
        self.git('update-ref', 'refs/remotes/origin/main', self.base)
        for event, before, base_ref, expected in cases:
            with self.subTest(event=event, before=before, base=base_ref):
                result = self.run_step('Determine range', EVENT_NAME=event,
                                       BEFORE_SHA=before, BASE_REF=base_ref)
                self.assertEqual(0, result.returncode, result.stdout)
                self.assertIn('PII_RANGE=' + expected, Path(self.env['GITHUB_ENV']).read_text())
                Path(self.env['GITHUB_ENV']).write_text('')

    def test_root_range_fallback_is_unchanged(self):
        result = self.run_step('Determine range', EVENT_NAME='push', BEFORE_SHA='', BASE_REF='')
        self.assertEqual(0, result.returncode, result.stdout)
        empty_tree = self.git('hash-object', '-t', 'tree', os.devnull)
        self.assertIn('PII_RANGE=' + empty_tree + '..HEAD', Path(self.env['GITHUB_ENV']).read_text())


if __name__ == '__main__':
    unittest.main()
