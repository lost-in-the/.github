# .github

Org-wide defaults for `lost-in-the`, including the shared **pii-check** workflow.

`.github/workflows/pii-check.yml` is a reusable workflow (`workflow_call`). It scans every
pull request and push for content that should never be published, and fails the run when it
finds any.

**What it scans**, over the commit range of the event: lines *added* by the diff, raw author
and committer names and email addresses, full commit messages (including multiline bodies
and trailers), and the file paths the diff touches. Identity fields are checked against the
same secret patterns as content. Configure an appropriate GitHub-provided no-reply address
for future commits when a personal address would match a prohibited pattern.

For pull requests (including `pull_request_target`), ranges use the event's validated
head and base commit SHAs. Commit metadata and messages are scanned
over `base..head`, including genuine merge commits in the submitted history. Added lines
and paths use the `base...head` diff from the merge base. GitHub's generated PR merge
commit and commits reachable only from the base are outside the submitted commit range.
Missing, malformed, or unavailable PR endpoints, or a missing merge base, fail the run.
Ordinary `pull_request` events check out the submitted head and use its allow file.
`pull_request_target` retains the trusted base-context event checkout (`github.sha`)
and its allow file; submitted allow rules cannot suppress target-context findings.

Push range selection is unchanged: an available previous commit uses `before..HEAD`;
new refs and unavailable previous commits retain the existing HEAD-only fallback.
An initial-commit fallback scans from the empty tree; an unresolvable range fails.

**How it is configured**: the match rules live only in the `PII_PATTERNS` Actions secret,
one extended regular expression per line. Nothing sensitive is committed to this repo. If the
secret is missing or empty the run fails loudly rather than passing silently.

**What it prints**: the `file:line` or commit sha of each hit, with the matched text replaced
by `[REDACTED]`, so a failing log never republishes what it caught.

**Opting a line out**: a repo may commit a `.pii-allow` file, one extended regular expression
per line; any scanned line matching it is exempt.

**Using it** from another repo in the org:

```yaml
name: pii-check
on: [pull_request, push]
jobs:
  pii-check:
    uses: lost-in-the/.github/.github/workflows/pii-check.yml@main
    secrets:
      PII_PATTERNS: ${{ secrets.PII_PATTERNS }}
```

## Scanner regression tests

Run `python3 -m unittest discover -s tests -v` with Bash, Git, grep, sed, and awk
available. The tests execute the reusable workflow's actual shell blocks in disposable
repositories with synthetic identities and patterns; they do not need Actions secrets
or access to a consumer repository.
