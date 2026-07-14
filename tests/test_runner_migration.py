import ast
import stat

from TwitchChannelPointsMiner import runner_migration


def test_schema_tracks_keyword_names_without_values():
    first = "miner = TwitchChannelPointsMiner(username='a', enabled=True)\nminer.mine([], followers=False)"
    second = "miner = TwitchChannelPointsMiner(username='b', enabled=False)\nminer.mine(['x'], followers=True)"

    assert runner_migration._schema(first) == runner_migration._schema(second)
    assert runner_migration._schema_version(first) == runner_migration._schema_version(
        second
    )


def test_portable_default_supports_literals_and_enum_attributes():
    assert runner_migration._portable_default(ast.parse("False").body[0].value) == "False"
    assert runner_migration._portable_default(ast.parse("[]").body[0].value) == "[]"
    assert (
        runner_migration._portable_default(
            ast.parse("FollowersOrder.ASC").body[0].value
        )
        == "'ASC'"
    )
    assert runner_migration._portable_default(ast.parse("factory()").body[0].value) is None


def test_insert_keywords_preserves_comments_and_produces_valid_python():
    source = "miner.mine(\n    [],\n)\n"
    call = runner_migration._runner_calls(ast.parse(source))["mine"][0]

    migrated = runner_migration._insert_keywords(
        source,
        call,
        [("followers", "False", ["# Import followers"], "# Default")],
    )

    ast.parse(migrated)
    assert "# Import followers" in migrated
    assert "followers=False,  # Default" in migrated


def test_migrate_runner_updates_file_preserves_mode_and_requests_restart(
    tmp_path, monkeypatch
):
    runner = tmp_path / "run.py"
    runner.write_text(
        "from TwitchChannelPointsMiner import TwitchChannelPointsMiner\n"
        "miner = TwitchChannelPointsMiner(username='alice')\n"
        "miner.mine([])\n",
        encoding="utf-8",
    )
    runner.chmod(0o640)
    restarts = []
    monkeypatch.setattr(
        runner_migration.os,
        "execv",
        lambda executable, arguments: restarts.append((executable, arguments)),
    )

    assert runner_migration.migrate_runner(runner) is True

    migrated = runner.read_text(encoding="utf-8")
    assert migrated.startswith(runner_migration.MARKER_PREFIX)
    assert stat.S_IMODE(runner.stat().st_mode) == 0o640
    assert restarts[0][1][1] == str(runner)


def test_migrate_runner_skips_non_runner_and_current_schema(tmp_path, monkeypatch):
    other = tmp_path / "custom.py"
    other.write_text("pass\n", encoding="utf-8")
    assert runner_migration.migrate_runner(other) is False

    runner = tmp_path / "run.py"
    canonical = (
        runner_migration.Path(runner_migration.__file__).parent.parent / "example.py"
    )
    marker = runner_migration.MARKER_PREFIX + runner_migration._schema_version(
        canonical.read_text(encoding="utf-8")
    )
    runner.write_text(marker + "\npass\n", encoding="utf-8")
    monkeypatch.setattr(runner_migration.os, "execv", lambda *_: None)

    assert runner_migration.migrate_runner(runner) is False
