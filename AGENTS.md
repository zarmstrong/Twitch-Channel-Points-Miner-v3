# AGENTS.md

This repository is a Python Twitch channel points miner. Keep changes focused, preserve the existing class-based structure, and prefer linking to existing docs instead of restating them.

## Where to start

- Main runtime entry point: [TwitchChannelPointsMiner/TwitchChannelPointsMiner.py](TwitchChannelPointsMiner/TwitchChannelPointsMiner.py)
- Example configuration and usage: [config.example.py](config.example.py)
- Project overview and setup notes: [README.md](README.md)
- Contribution and style expectations: [CONTRIBUTING.md](CONTRIBUTING.md)

## Working conventions

- Preserve `__slots__` usage and existing enum-based settings patterns when touching classes.
- Keep GraphQL and Twitch API logic in the existing modules under [TwitchChannelPointsMiner/classes/](TwitchChannelPointsMiner/classes/).
- Use the current logging style from [TwitchChannelPointsMiner/logger.py](TwitchChannelPointsMiner/logger.py); logs are intentionally emoji-rich and colorized.
- Avoid adding new dependencies unless the existing stack cannot reasonably support the change.
- Do not introduce secrets or real credentials into examples, docs, or code.

## Releases and versioning

- The current release baseline is `3.2.0`.
- Releases are managed by Release Please through [release-please-config.json](release-please-config.json), [.release-please-manifest.json](.release-please-manifest.json), and [.github/workflows/release-please.yml](.github/workflows/release-please.yml).
- [TwitchChannelPointsMiner/__init__.py](TwitchChannelPointsMiner/__init__.py) is the package version source consumed by `setup.py`; keep its `__version__` value synchronized with the Release Please manifest when manually changing the release baseline.
- Preserve the `x-release-please-version` annotation on the `__version__` line so Release Please can update it.
- Use Conventional Commit prefixes such as `feat:`, `fix:`, and `docs:` so Release Please can determine versions and generate changelog entries.
- Preserve the repository's existing bare SemVer tags (for example, `3.0.0` rather than `v3.0.0`).

### Release workflow

- Let the open Release Please PR accumulate all ordinary PRs intended for the next release; merge the release PR only when that release is ready to publish.
- Give each ordinary PR a Conventional Commit title and preserve that title when squash-merging so Release Please can produce a distinct changelog entry.
- Prefer separate PRs or commits for independently notable features, fixes, performance changes, and documentation updates instead of hiding them in one broad squash commit.
- When a single PR intentionally contains multiple user-facing changes, list each additional change at the bottom of the squash commit message as its own Conventional Commit entry (for example, `fix(gql): ...` or `feat(drops): ...`) so Release Please includes each one in the changelog. Keep the PR title as the primary entry and follow the [Release Please multiple-changes guidance](https://github.com/googleapis/release-please#what-if-my-pr-contains-multiple-fixes-or-features).
- Keep any metadata for an additional entry indented directly below that entry, including `BREAKING-CHANGE: ...`, and do not place unrelated text after the additional entries.
- Before merging the Release Please PR, review its generated changelog and release notes for user-facing completeness and expand the summary when a single entry contains several notable changes.
- Do not enable auto-merge on the Release Please PR because merging it is the action that publishes the combined release.

### Updating published release notes

- Follow the formatting established by the [3.5.0 release](https://github.com/zarmstrong/Twitch-Channel-Points-Miner-v3/releases/tag/3.5.0) and its predecessor, the [3.4.0 release](https://github.com/zarmstrong/Twitch-Channel-Points-Miner-v3/releases/tag/3.4.0).
- Start with `## [VERSION](COMPARE_URL) (YYYY-MM-DD)`, where `COMPARE_URL` compares the previous bare SemVer tag with the current bare SemVer tag.
- Keep generated changes grouped under the applicable `### Features`, `### Bug Fixes`, `### Performance Improvements`, and `### Documentation` headings, in that order. Omit empty sections.
- Format each generated entry as `* **scope:** summary ([#PR](PR_URL)) ([short-sha](COMMIT_URL))`; omit the scope only when the source Conventional Commit has no scope.
- Add or expand a concise user-facing summary only when the generated entries do not adequately explain the release. Preserve the generated headings, issue or PR links, and commit links so the notes remain auditable.
- Read the existing body before editing a published release, write the complete revised Markdown to a file, and apply it with `gh release edit TAG --notes-file FILE`. Do not replace the body with only the new text.
- Preserve the `<!-- docker-image:start -->` and `<!-- docker-image:end -->` block exactly when it is already present. The Docker publish workflow owns and replaces that entire block, including the `### Docker image` heading, tagged-image link, and `docker pull` command; do not add a second Docker section manually.

## Validation

- Install dependencies with `pip install -r requirements.txt`.
- Run the automated test suite with `python -m pytest`; prefer focused tests for the files or behavior you change.
- The project follows Black formatting; keep edits compatible with the existing style and line lengths.

## Pull request reviews

- When an agent fixes an actionable Copilot review item, verify the change and resolve the corresponding review thread. Do not resolve comments that remain unfixed or unverified.
- After pushing new commits to a branch with an open pull request, request a fresh Copilot review so the latest changes are reviewed. Do not request another review when one is already pending for the current head commit.

## Implementation notes

- The top-level `TwitchChannelPointsMiner` class coordinates Twitch access, websocket handling, and streamer state.
- Entity models live under [TwitchChannelPointsMiner/classes/entities/](TwitchChannelPointsMiner/classes/entities/); keep data-model changes localized there.
- If you need deeper setup or contribution guidance, link back to the README or CONTRIBUTING file rather than duplicating it here.
