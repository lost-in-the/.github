# .github

Org-wide defaults for `lost-in-the`, including the shared **pii-check** workflow.

`.github/workflows/pii-check.yml` is a reusable workflow (`workflow_call`). It scans every
pull request and push for content that should never be published, and fails the run when it
finds any.

**What it scans**, over the commit range of the event: lines *added* by the diff, commit
messages, and the file paths the diff touches. Author and committer identity are deliberately
not scanned: a commit is expected to carry a real address, so scanning it only ever fails.

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
