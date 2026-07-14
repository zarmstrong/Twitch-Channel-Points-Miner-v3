# Testing

The test suite uses [pytest](https://docs.pytest.org/) and is designed to run
without Twitch credentials or network access.

## Set up a development environment

From the repository root, create and activate a virtual environment, then
install the application and test dependencies:

```sh
python3 -m venv .venv-tests
source .venv-tests/bin/activate
python3 -m pip install -r requirements-dev.txt
```

On Windows, activate the environment with:

```powershell
.venv-tests\Scripts\Activate.ps1
```

## Run the suite

Run every test from the repository root:

```sh
python3 -m pytest
```

Useful commands while developing include:

```sh
# Run one test module.
python3 -m pytest tests/test_bet.py

# Run tests whose names contain a term.
python3 -m pytest -k migration

# Stop after the first failure and show local variables.
python3 -m pytest -x -l

# Show more detail for each test.
python3 -m pytest -v
```

Pytest discovers tests under `tests/` according to [pytest.ini](../pytest.ini).

## Test coverage

The current suite covers these areas:

| Module | Behavior |
|---|---|
| `test_utils.py` | General helpers, default settings, chunking, nonce generation, and text cleanup |
| `test_bet.py` | Prediction settings, outcome calculations, strategies, spending limits, and filters |
| `test_campaign.py` | Campaign filtering, Drop synchronization and claiming, and community goals |
| `test_entities.py` | Streams, Drops, PubSub messages, topics, timestamps, and payload encoding |
| `test_config_migration.py` | Legacy runner conversion, invalid inputs, output permissions, and migration markers |
| `test_runner_migration.py` | Runner schemas, portable defaults, keyword insertion, and automatic migration |
| `test_streamer.py` | Streamer settings, state transitions, histories, multipliers, Drops, and prediction timing |

`conftest.py` contains fixtures shared across the suite, including the minimal
logger configuration expected by entity models.

## Adding tests

- Name test files `test_<feature>.py` and test functions `test_<behavior>`.
- Keep tests deterministic and independent of execution order.
- Mock Twitch, GraphQL, websocket, notification, filesystem, and clock behavior
  at the narrowest useful boundary.
- Do not use real accounts, credentials, external endpoints, or live Twitch
  responses. Keep representative response data small and local to the test.
- Prefer testing observable behavior over private implementation details.
- Add a regression test when fixing a bug.

Use pytest's built-in `monkeypatch` and `tmp_path` fixtures for isolated state.
Only add another test dependency when pytest and the Python standard library
cannot reasonably support the case.

## Continuous integration

[The Tests workflow](../.github/workflows/tests.yml) runs the complete suite on
Python 3.11, 3.12, and 3.13 for pushes and pull requests. A change should pass
locally before it is submitted, but the workflow remains the final check across
all supported Python versions.
