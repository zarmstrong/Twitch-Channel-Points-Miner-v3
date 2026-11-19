# AGENTS.md: workflows and release automation

Read the root [AGENTS.md](../AGENTS.md) first. Its release sections explain how versions, Release Please and release notes work. This file covers editing what's in `.github/`.

## Workflows

- `workflows/tests.yml` runs on pushes to `master`, on every pull request and on manual dispatch:
  - Black (on Python 3.13) and pytest (on Python 3.11 to 3.14).
  - The Node tests for the release scripts.
  - Both Windows executables and the installer.

  Documentation-only pull requests skip the code work through the `changes` job. Keep that working when you add a job, and depend on `changes`:
  - A single job can skip itself with `if: ${{ !cancelled() && needs.changes.outputs.code != 'false' }}`.
  - A matrix job whose checks are required must keep running and skip its steps instead, as the `pytest` job does. A skipped matrix job never reports its expanded checks (`Python 3.11` and so on), so required checks stay pending.
  - A job that needs another job is skipped when that job fails, and a skipped required check counts as passing. Include `!cancelled()` in its `if:` and fail explicitly, as `windows_installer` does.
- `actions/build-windows-exe` builds and smoke-tests one Windows executable. `tests.yml` and `release-please.yml` both call it after checking out the source, so change the build steps there, not in a workflow. The release build uses the copy in the released tag.
- `workflows/release-please.yml` creates releases and publishes the Docker image (through `deploy-docker.yml`) and the Windows files.
- `workflows/deploy-docker.yml` owns the `<!-- docker-image:start -->` / `<!-- docker-image:end -->` block in release notes. A job that runs alongside it must not write back a copy of the release body it read earlier, or the block can disappear.
- `workflows/mark-issues-awaiting-release.yml` labels issues fixed on `master` until a release ships them.

## Supply chain and secrets

- Actions are referenced by a major-version tag (`actions/checkout@v7`), and Dependabot updates them. Follow that for new ones.
- Give each job the narrowest `permissions:` it needs. Set `persist-credentials: false` on a new checkout that doesn't push.
- Pass untrusted values (tags, PR titles, inputs) to scripts through `env:`, never by expanding `${{ }}` inside `run:`.
- Never print tokens or write them to files that get uploaded.

## Release scripts

- `scripts/release-linked-issues.js` closes issues a release ships.
- `scripts/release-pull-links.js` relinks PR references in release notes.
- `scripts/docker-latest-tag.js` decides whether a Docker publish moves `latest` and the Docker Hub description. Only the newest plain `MAJOR.MINOR.PATCH` tag does, so a prerelease or an older release published again leaves them alone. `deploy-docker.yml` checks it out from the workflow's commit, because an older release tag doesn't contain it.

Each has a `*.test.js` file using Node's built-in test runner:

```
node .github/scripts/release-linked-issues.test.js
node .github/scripts/release-pull-links.test.js
node .github/scripts/docker-latest-tag.test.js
```

The scripts receive `github`, `context` and `core` from `actions/github-script`. Tests pass fakes, so they run offline. Add a test with any behaviour change.

## Validating workflow changes

There is no local runner for workflows. At minimum, check the YAML parses, then review the expressions by hand, including the skip rules above. Say in the pull request what could only be verified on GitHub.
