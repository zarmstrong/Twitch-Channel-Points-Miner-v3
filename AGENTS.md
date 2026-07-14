# AGENTS.md

This repository is a Python Twitch channel points miner. Keep changes focused, preserve the existing class-based structure, and prefer linking to existing docs instead of restating them.

## Where to start

- Main runtime entry point: [TwitchChannelPointsMiner/TwitchChannelPointsMiner.py](TwitchChannelPointsMiner/TwitchChannelPointsMiner.py)
- Example configuration and usage: [example.py](example.py)
- Project overview and setup notes: [README.md](README.md)
- Contribution and style expectations: [CONTRIBUTING.md](CONTRIBUTING.md)

## Working conventions

- Preserve `__slots__` usage and existing enum-based settings patterns when touching classes.
- Keep GraphQL and Twitch API logic in the existing modules under [TwitchChannelPointsMiner/classes/](TwitchChannelPointsMiner/classes/).
- Use the current logging style from [TwitchChannelPointsMiner/logger.py](TwitchChannelPointsMiner/logger.py); logs are intentionally emoji-rich and colorized.
- Avoid adding new dependencies unless the existing stack cannot reasonably support the change.
- Do not introduce secrets or real credentials into examples, docs, or code.

## Releases and versioning

- The current release baseline is `3.0.0`.
- Releases are managed by Release Please through [release-please-config.json](release-please-config.json), [.release-please-manifest.json](.release-please-manifest.json), and [.github/workflows/release-please.yml](.github/workflows/release-please.yml).
- [TwitchChannelPointsMiner/__init__.py](TwitchChannelPointsMiner/__init__.py) is the package version source consumed by `setup.py`; keep its `__version__` value synchronized with the Release Please manifest when manually changing the release baseline.
- Preserve the `x-release-please-version` annotation on the `__version__` line so Release Please can update it.
- Use Conventional Commit prefixes such as `feat:`, `fix:`, and `docs:` so Release Please can determine versions and generate changelog entries.
- Preserve the repository's existing bare SemVer tags (for example, `3.0.0` rather than `v3.0.0`).

## Validation

- Install dependencies with `pip install -r requirements.txt`.
- There is no formal automated test suite in the repo; prefer focused smoke checks and format validation for the files you touch.
- The project follows Black formatting; keep edits compatible with the existing style and line lengths.

## Implementation notes

- The top-level `TwitchChannelPointsMiner` class coordinates Twitch access, websocket handling, and streamer state.
- Entity models live under [TwitchChannelPointsMiner/classes/entities/](TwitchChannelPointsMiner/classes/entities/); keep data-model changes localized there.
- If you need deeper setup or contribution guidance, link back to the README or CONTRIBUTING file rather than duplicating it here.
