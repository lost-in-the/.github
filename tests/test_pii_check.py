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
                        GITHUB_ENV=str(self.root / 'env'),
                        GITHUB_OUTPUT=str(self.root / 'output'))
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

    def test_added_line_location_redacts_matching_path(self):
        (self.repo / (MARKER + '.txt')).write_text(MARKER + '\n')
        self.git('add', '.')
        self.commit()
        result = self.scan()
        self.assert_hit(result, 'added-line')
        self.assertIn('path', result.stdout)

    def test_added_line_location_respects_anchored_patterns(self):
        self.patterns.write_text('^' + MARKER + '$\n')
        (self.repo / MARKER).write_text(MARKER + '\n')
        self.git('add', '.')
        self.commit()
        result = self.scan()
        self.assert_hit(result, 'added-line')
        self.assertIn('[REDACTED]:1:', result.stdout)

    def test_added_line_location_respects_full_location_patterns(self):
        self.patterns.write_text('^' + MARKER + ':1$\n')
        (self.repo / MARKER).write_text(MARKER + ':1\n')
        self.git('add', '.')
        self.commit()
        self.assert_hit(self.scan(), 'added-line')

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
                 ('workflow_dispatch', '', '', 'HEAD~1..HEAD')]
        self.git('update-ref', 'refs/remotes/origin/main', self.base)
        for event, before, base_ref, expected in cases:
            with self.subTest(event=event, before=before, base=base_ref):
                result = self.run_step('Determine range', EVENT_NAME=event,
                                       BEFORE_SHA=before, BASE_REF=base_ref)
                self.assertEqual(0, result.returncode, result.stdout)
                self.assertIn('PII_RANGE=' + expected, Path(self.env['GITHUB_ENV']).read_text())
                Path(self.env['GITHUB_ENV']).write_text('')


    def determine_pr(self, base, head, event='pull_request'):
        return self.run_step('Determine range', EVENT_NAME=event,
                             PR_BASE_SHA=base, PR_HEAD_SHA=head, BEFORE_SHA='', BASE_REF='main')

    def selected_scan(self):
        values = dict(line.split('=', 1) for line in Path(self.env['GITHUB_ENV']).read_text().splitlines())
        return self.scan(values['PII_RANGE'], PII_COMMIT_RANGE=values.get('PII_COMMIT_RANGE', values['PII_RANGE']))

    def test_pr_generated_merge_is_excluded(self):
        self.git('checkout', '-q', '-b', 'submitted')
        head = self.commit()
        self.git('checkout', '-q', 'main')
        self.commit('Synthetic base-only commit')
        base = self.git('rev-parse', 'HEAD')
        self.git('merge', '-q', '--no-ff', 'submitted', '-m', 'Synthetic generated merge',
                 env={'GIT_COMMITTER_EMAIL': MARKER + '@example.invalid'})
        generated = self.git('rev-parse', 'HEAD')
        self.assert_hit(self.scan(base + '...HEAD'), sha=generated)
        for event in ('pull_request', 'pull_request_target'):
            with self.subTest(event=event):
                result = self.determine_pr(base, head, event)
                self.assertEqual(0, result.returncode, result.stdout)
                result = self.selected_scan()
                self.assertEqual(0, result.returncode, result.stdout)
                self.assertNotIn(generated, result.stdout)

    def test_pr_checks_all_submitted_identity_fields(self):
        self.git('checkout', '-q', '-b', 'submitted')
        for field in ('GIT_AUTHOR_NAME', 'GIT_AUTHOR_EMAIL',
                      'GIT_COMMITTER_NAME', 'GIT_COMMITTER_EMAIL'):
            with self.subTest(field=field):
                base = self.git('rev-parse', 'HEAD')
                head = self.commit(**{field: MARKER})
                result = self.determine_pr(base, head)
                self.assertEqual(0, result.returncode, result.stdout)
                self.assert_hit(self.selected_scan(), sha=head)

    def test_pr_genuine_merge_and_side_commit_are_scanned(self):
        self.git('checkout', '-q', '-b', 'side')
        side = self.commit(GIT_AUTHOR_EMAIL=MARKER + '@example.invalid')
        self.git('checkout', '-q', 'main')
        self.commit()
        self.git('merge', '-q', '--no-ff', 'side', '-m', 'Synthetic submitted merge',
                 env={'GIT_COMMITTER_NAME': MARKER})
        head = self.git('rev-parse', 'HEAD')
        result = self.determine_pr(self.base, head)
        self.assertEqual(0, result.returncode, result.stdout)
        result = self.selected_scan()
        self.assert_hit(result, sha=side)
        self.assertIn(head, result.stdout)

    def test_pr_base_only_metadata_is_not_submitted(self):
        self.git('checkout', '-q', '-b', 'submitted')
        head = self.commit()
        self.git('checkout', '-q', 'main')
        base = self.commit(GIT_AUTHOR_NAME=MARKER)
        result = self.determine_pr(base, head)
        self.assertEqual(0, result.returncode, result.stdout)
        result = self.selected_scan()
        self.assertEqual(0, result.returncode, result.stdout)

    def test_pr_diff_and_path_checks_use_submitted_head(self):
        self.git('checkout', '-q', '-b', 'submitted')
        (self.repo / (MARKER + '.txt')).write_text(MARKER + '\n')
        self.git('add', '.')
        head = self.commit()
        self.git('checkout', '-q', 'main')
        self.commit()
        base = self.git('rev-parse', 'HEAD')
        result = self.determine_pr(base, head)
        self.assertEqual(0, result.returncode, result.stdout)
        result = self.selected_scan()
        self.assert_hit(result, 'added-line')
        self.assertIn('path', result.stdout)

    def test_pr_missing_malformed_or_unavailable_commits_fail_closed(self):
        head = self.commit()
        for base, pr_head in [('', head), (self.base, ''), ('not-a-sha', head),
                              (self.base, '--all'), ('f' * 40, head),
                              (self.base, 'f' * 40), (self.base + '\nextra', head)]:
            with self.subTest(base_valid=base == self.base, head_valid=pr_head == head):
                result = self.determine_pr(base, pr_head)
                self.assertEqual(1, result.returncode, result.stdout)
                self.assertNotIn('PII_RANGE=', Path(self.env['GITHUB_ENV']).read_text()
                                 if Path(self.env['GITHUB_ENV']).exists() else '')
                self.assertNotIn('pii-check: clean', result.stdout)

    def test_pr_disconnected_history_fails_closed(self):
        self.git('checkout', '-q', '--orphan', 'unrelated')
        head = self.commit('Synthetic unrelated root')
        result = self.determine_pr(self.base, head)
        self.assertEqual(1, result.returncode, result.stdout)

    def test_invalid_commit_range_fails_closed(self):
        self.commit()
        result = self.scan(PII_COMMIT_RANGE='missing..HEAD')
        self.assertEqual(1, result.returncode, result.stdout)
        self.assertNotIn('pii-check: clean', result.stdout)

    def test_checkout_selection_uses_validated_event_sha(self):
        head = self.commit()
        for event, expected in [('pull_request', self.base),
                                ('pull_request_target', head), ('push', head)]:
            with self.subTest(event=event):
                result = self.run_step('Select checkout', EVENT_NAME=event,
                                       PR_HEAD_SHA=self.base, EVENT_SHA=head)
                self.assertEqual(0, result.returncode, result.stdout)
                self.assertIn('sha=' + expected, Path(self.env['GITHUB_OUTPUT']).read_text())
                Path(self.env['GITHUB_OUTPUT']).write_text('')
        for invalid in ('', '--all', head + '\nextra', 'g' * 40):
            result = self.run_step('Select checkout', EVENT_NAME='pull_request',
                                   PR_HEAD_SHA=invalid, EVENT_SHA=head)
            self.assertEqual(1, result.returncode, result.stdout)
            self.assertEqual('', Path(self.env['GITHUB_OUTPUT']).read_text())

    def test_selected_checkout_reads_submitted_allow_file(self):
        self.git('checkout', '-q', '-b', 'submitted')
        (self.repo / '.pii-allow').write_text('SYNTHETIC_PII_.*\n')
        self.git('add', '.')
        head = self.commit(MARKER)
        self.git('checkout', '-q', 'main')
        (self.repo / 'base.txt').write_text('safe\n')
        self.git('add', '.')
        base = self.commit('Synthetic base change')
        result = self.run_step('Select checkout', EVENT_NAME='pull_request',
                               PR_HEAD_SHA=head, EVENT_SHA=base)
        self.assertEqual(0, result.returncode, result.stdout)
        chosen = Path(self.env['GITHUB_OUTPUT']).read_text().strip().split('=', 1)[1]
        self.git('checkout', '-q', '--detach', chosen)
        self.assertEqual(head, self.git('rev-parse', 'HEAD'))
        result = self.determine_pr(base, head)
        self.assertEqual(0, result.returncode, result.stdout)
        result = self.selected_scan()
        self.assertEqual(0, result.returncode, result.stdout)
        self.assertIn('.pii-allow active', result.stdout)

    def test_target_event_does_not_trust_submitted_allow_file(self):
        self.git('checkout', '-q', '-b', 'submitted')
        (self.repo / '.pii-allow').write_text('SYNTHETIC_PII_.*\n')
        (self.repo / 'submitted.txt').write_text(MARKER + '\n')
        self.git('add', '.')
        head = self.commit('Synthetic submitted violation', GIT_AUTHOR_NAME=MARKER,
                           GIT_AUTHOR_EMAIL=MARKER + '@example.invalid',
                           GIT_COMMITTER_NAME=MARKER,
                           GIT_COMMITTER_EMAIL=MARKER + '@example.invalid')
        result = self.run_step('Select checkout', EVENT_NAME='pull_request_target',
                               PR_HEAD_SHA=head, EVENT_SHA=self.base)
        self.assertEqual(0, result.returncode, result.stdout)
        chosen = Path(self.env['GITHUB_OUTPUT']).read_text().strip().split('=', 1)[1]
        self.git('checkout', '-q', '--detach', chosen)
        result = self.determine_pr(self.base, head, 'pull_request_target')
        self.assertEqual(0, result.returncode, result.stdout)
        result = self.selected_scan()
        self.assert_hit(result, 'commit', sha=head)
        self.assertIn('added-line', result.stdout)
        self.assertNotIn('.pii-allow active', result.stdout)
        self.assertFalse((self.repo / '.pii-allow').exists())
        self.assertEqual(self.base, self.git('rev-parse', 'HEAD'))

    def test_target_checkout_rejects_invalid_trusted_event_sha(self):
        head = self.commit()
        for invalid in ('', '--all', head + '\nextra', 'g' * 40):
            with self.subTest(invalid_length=len(invalid)):
                result = self.run_step('Select checkout', EVENT_NAME='pull_request_target',
                                       PR_HEAD_SHA=head, EVENT_SHA=invalid)
                self.assertEqual(1, result.returncode, result.stdout)
                self.assertFalse(Path(self.env['GITHUB_OUTPUT']).exists())

    def test_pr_missing_commit_can_be_fetched_by_validated_sha(self):
        remote = self.root / 'remote.git'
        contributor = self.root / 'contributor'
        self.git('clone', '-q', '--bare', str(self.repo), str(remote))
        self.git('clone', '-q', str(remote), str(contributor))
        self.git('-C', str(contributor), 'config', 'commit.gpgsign', 'false')
        self.git('-C', str(contributor), 'commit', '-q', '--allow-empty',
                 '-m', 'Synthetic remote head', env={'GIT_AUTHOR_NAME': MARKER})
        head = self.git('-C', str(contributor), 'rev-parse', 'HEAD')
        self.git('-C', str(contributor), 'push', '-q', 'origin', 'HEAD:refs/heads/submitted')
        self.git('remote', 'add', 'origin', str(remote))
        missing = subprocess.run(['git', 'cat-file', '-e', head + '^{commit}'],
                                 cwd=self.repo, env=self.env, capture_output=True)
        self.assertNotEqual(0, missing.returncode)
        result = self.determine_pr(self.base, head)
        self.assertEqual(0, result.returncode, result.stdout)
        self.assert_hit(self.selected_scan(), sha=head)

    def test_new_branch_and_missing_previous_sha_keep_head_only_policy(self):
        self.commit(GIT_AUTHOR_NAME=MARKER)
        self.commit('Synthetic clean tip')
        for before in ('0' * 40, '', 'f' * 40):
            with self.subTest(before=before):
                result = self.run_step('Determine range', EVENT_NAME='push',
                                       BEFORE_SHA=before, PR_BASE_SHA='', PR_HEAD_SHA='')
                self.assertEqual(0, result.returncode, result.stdout)
                result = self.selected_scan()
                self.assertEqual(0, result.returncode, result.stdout)

    def test_initial_push_fallback_cannot_hide_root_metadata(self):
        self.git('checkout', '-q', '--orphan', 'synthetic-root')
        head = self.commit('Synthetic root metadata', GIT_AUTHOR_NAME=MARKER)
        result = self.run_step('Determine range', EVENT_NAME='push', BEFORE_SHA='',
                               PR_BASE_SHA='', PR_HEAD_SHA='')
        self.assertEqual(0, result.returncode, result.stdout)
        result = self.selected_scan()
        self.assertEqual(1, result.returncode, result.stdout)
        self.assertNotIn('pii-check: clean', result.stdout)
        self.assertNotIn(MARKER, result.stdout)
        if 'match(es) found' in result.stdout:
            self.assert_hit(result, sha=head)
        else:
            self.assertIn('cannot resolve commit range', result.stdout)

    def test_root_range_fallback_is_unchanged(self):
        result = self.run_step('Determine range', EVENT_NAME='push', BEFORE_SHA='', BASE_REF='')
        self.assertEqual(0, result.returncode, result.stdout)
        empty_tree = self.git('hash-object', '-t', 'tree', os.devnull)
        self.assertIn('PII_RANGE=' + empty_tree + '..HEAD', Path(self.env['GITHUB_ENV']).read_text())


if __name__ == '__main__':
    unittest.main()
