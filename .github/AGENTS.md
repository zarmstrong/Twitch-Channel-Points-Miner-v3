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
- `workflows/release-please.yml` creates releases and publishes the Docker image (through `deploy-docker.yml`) and the Windows files.
  - The publishing jobs depend on `resolve_release`, not on `release_please`. It runs even when `release_please` failed. When Release Please reports no new release, `scripts/release-resolve.js` publishes the manifest's release if its Docker notes block or Windows files are missing, whichever commit it is on: another run's Release Please can create it, since it reads `master`, and the release commit's own waiting run can be replaced by a newer push. Only the missing parts are published: Docker when the release has no Docker block, Windows when either file is missing, so a published image is never rebuilt over its version tag. A part whose publishing keeps failing is retried on every push until the next release merges: only the manifest's release is checked, so finish an older release by re-running its release commit's workflow run, which publishes whatever it is missing. A failed attempt of the release commit's own run, or any re-run, also closes the release's issues again (test: `node .github/scripts/release-resolve.test.js`). The Docker block is written only after every Docker step succeeded. Re-running the release commit's workflow run publishes a release whose first run stopped early.
  - The Windows files are added with `gh release upload --clobber`, which never writes the release body.
  - `deploy-docker.yml` doesn't build a version whose images are already on Docker Hub, so retrying a failed `latest`, description or notes step never replaces a published image. When only one of the two repositories has the version, it's copied to the other, and when they hold different builds of it (a `rebuild` that failed partway), `twitch-channel-points-miner` is copied over `twitch-channel-points-miner-v2`. A registry error other than "not found" fails the job instead of rebuilding. Dispatch it with `rebuild` to build one again. If that dispatch fails, dispatch it again: the release notes still hold the Docker block from the earlier publish, so the release workflow treats Docker as published and won't retry it.
  - The jobs run only on `master`, and `master` runs share one concurrency group (`release-please-refs/heads/master`), so two Release Please runs never edit a release body at once. Keep any job that writes the body inside this workflow, or give it the same protection. A manual dispatch of `deploy-docker.yml` runs outside this group, so don't start one while a Release Please run is in progress. Publishes of the same version, by hand or from a release run, queue in their own `deploy-docker-<version>` group, so two never both build it.
- `workflows/deploy-docker.yml` owns the `<!-- docker-image:start -->` / `<!-- docker-image:end -->` block in release notes. A job that runs alongside it must not write back a copy of the release body it read earlier, or the block can disappear.
- `workflows/mark-issues-awaiting-release.yml` labels issues fixed on `master` until a release ships them.

## Supply chain and secrets

- Actions are referenced by a major-version tag (`actions/checkout@v7`), and Dependabot updates them. Follow that for new ones.
- Give each job the narrowest `permissions:` it needs. Set `persist-credentials: false` on a new checkout that doesn't push.
- Pass untrusted values (tags, PR titles, inputs) to scripts through `env:`, never by expanding `${{ }}` inside `run:`.
- Never print tokens or write them to files that get uploaded.

## Release scripts

- `scripts/release-linked-issues.js` closes issues a release ships. A release cycle runs from one full release to the next: a GitHub prerelease, such as a nightly build, doesn't end one and closes no issues.
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
