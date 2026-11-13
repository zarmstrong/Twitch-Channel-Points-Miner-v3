# AGENTS.md: workflows and release automation

Read the root [AGENTS.md](../AGENTS.md) first. Its release sections explain how versions, Release Please and release notes work. This file covers editing what's in `.github/`.

## Workflows

- `workflows/tests.yml` runs on every push and pull request:
  - Black and pytest on Python 3.11 to 3.14.
  - The Node tests for the release scripts.
  - Both Windows executables and the installer.

  Documentation-only pull requests skip the code jobs through the `changes` job. Keep that working when you add a job: depend on `changes` and skip when `needs.changes.outputs.code == 'false'`.
- `workflows/release-please.yml` creates releases and publishes the Docker image (through `deploy-docker.yml`) and the Windows files.
- `workflows/deploy-docker.yml` owns the `<!-- docker-image:start -->` / `<!-- docker-image:end -->` block in release notes. A job that runs alongside it must not write back a copy of the release body it read earlier, or the block can disappear.
- `workflows/mark-issues-awaiting-release.yml` labels issues fixed on `master` until a release ships them.

## Supply chain and secrets

- Pin every third-party action to a full commit SHA, with the version in a trailing comment (`uses: owner/action@<sha> # vX.Y.Z`). Dependabot updates them.
- Give each job the narrowest `permissions:` it needs, and set `persist-credentials: false` on checkouts that don't push.
- Pass untrusted values (tags, PR titles, inputs) to scripts through `env:`, never by expanding `${{ }}` inside `run:`.
- Never print tokens or write them to files that get uploaded.

## Release scripts

`scripts/release-linked-issues.js` closes issues a release ships, and `scripts/release-pull-links.js` relinks PR references in release notes. Each has a `*.test.js` file using Node's built-in test runner:

```
node .github/scripts/release-linked-issues.test.js
node .github/scripts/release-pull-links.test.js
```

The scripts receive `github`, `context` and `core` from `actions/github-script`. Tests pass fakes, so they run offline. Add a test with any behaviour change.

## Validating workflow changes

There is no local runner for workflows. At minimum, check the YAML parses, then review the expressions by hand. Remember that a step's `if:` can't be skipped for matrix jobs without leaving the check unreported. Say in the pull request what could only be verified on GitHub.
