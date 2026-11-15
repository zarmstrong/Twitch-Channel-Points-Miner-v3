# AGENTS.md: tests

Read the root [AGENTS.md](../AGENTS.md) first. The suite runs with `python -m pytest`. Prefer running the files you touched, for example `python -m pytest tests/test_discord.py`.

## Offline only

Tests never reach Twitch, notification services or the internet.

- Mock `requests` and the GraphQL layer, and fake websocket and IRC objects.
- Use `tmp_path` for files. Never write to the real `config/`, `cookies/`, `analytics/` or `logs/` folders.
- Don't sleep for real. Patch `time.sleep` (or the module's own sleep helper) and drive clocks from the test.

## How objects are built

- `tests/conftest.py` sets a minimal `Settings.logger` for every test.
- Many tests build `Twitch` or `TwitchChannelPointsMiner` with `object.__new__` and set only the attributes the code under test reads. Code that reads optional state with `getattr(self, ..., default)` keeps working this way.
- Private methods are patched by their mangled names, for example `patch.object(Twitch, "_Twitch__get_inventory")`.
- `SimpleNamespace` stand-ins are common for streamers, websockets and notifiers.

## Writing a regression test

- Make the test fail without the fix. Check it by restoring only the source file, not the test, and running the test again.
- For a race, drive the interleaving deterministically (patch the sleep or the call where the other thread would run) rather than hoping threads collide.
- A test that waits on a thread must use a join timeout, so a bug fails the test instead of hanging the run.

## Windows-specific tests

- `tests/test_windows_launcher.py` covers the desktop app. Mock `ctypes`, `winreg`, `subprocess` and pywebview. Never create real mutexes, registry values, scheduled tasks, processes or message boxes. A real `MessageBoxW` blocks the run until someone clicks it.
- Tests that need Windows or a non-Windows platform skip themselves with `pytest.mark.skipif`.

## Formatting

Format tests with Black like the rest of the code.
