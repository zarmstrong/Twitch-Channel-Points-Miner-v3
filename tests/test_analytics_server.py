import ast
import re
from io import BytesIO
from pathlib import Path

import pytest

from TwitchChannelPointsMiner.classes.AnalyticsServer import (
    MAX_LOG_TAIL_BYTES,
    bounded_log_start,
    filter_datas,
    get_streamer_summary,
    seek_log_start,
)
from TwitchChannelPointsMiner.classes.Settings import Settings
from TwitchChannelPointsMiner.config_editor import (
    ConfigEditError,
    add_web_config_value,
    read_web_config,
)


def test_bounded_log_start_caps_legacy_request_without_tail_bytes():
    file_size = MAX_LOG_TAIL_BYTES * 10

    assert bounded_log_start(file_size, 0) == file_size - MAX_LOG_TAIL_BYTES


def test_bounded_log_start_honors_smaller_initial_tail():
    file_size = MAX_LOG_TAIL_BYTES * 10

    assert bounded_log_start(file_size, 0, tail_bytes=128 * 1024) == (
        file_size - (128 * 1024)
    )


def test_bounded_log_start_keeps_recent_incremental_position():
    file_size = MAX_LOG_TAIL_BYTES * 10
    position = file_size - 1024

    assert bounded_log_start(file_size, position) == position


def test_bounded_log_start_recovers_from_rotated_log_position():
    file_size = 4096

    assert bounded_log_start(file_size, file_size + 1) == 0


def test_seek_log_start_keeps_complete_line_at_boundary():
    log_file = BytesIO(b"first\nsecond\nthird\n")

    seek_log_start(log_file, 6, discard_partial_line=True)

    assert log_file.read() == b"second\nthird\n"


def test_seek_log_start_discards_partial_first_line():
    log_file = BytesIO(b"first\nsecond\nthird\n")

    seek_log_start(log_file, 8, discard_partial_line=True)

    assert log_file.read() == b"third\n"


def test_get_streamer_summary_uses_latest_timestamp(tmp_path, monkeypatch):
    monkeypatch.setattr(Settings, "analytics_path", str(tmp_path), raising=False)
    (tmp_path / "example.json").write_text(
        '{"series": [{"x": 20, "y": 200}, {"x": 10, "y": 100}]}',
        encoding="utf-8",
    )

    assert get_streamer_summary("example.json") == {
        "points": 200,
        "last_activity": 20,
    }


def test_get_streamer_summary_handles_invalid_file(tmp_path, monkeypatch):
    monkeypatch.setattr(Settings, "analytics_path", str(tmp_path), raising=False)
    (tmp_path / "broken.json").write_text("not json", encoding="utf-8")

    assert get_streamer_summary("broken.json") == {
        "points": 0,
        "last_activity": 0,
    }


def test_get_streamer_summary_handles_non_list_series(tmp_path, monkeypatch):
    monkeypatch.setattr(Settings, "analytics_path", str(tmp_path), raising=False)
    (tmp_path / "broken-series.json").write_text(
        '{"series": 123}', encoding="utf-8"
    )

    assert get_streamer_summary("broken-series.json") == {
        "points": 0,
        "last_activity": 0,
    }


def test_get_streamer_summary_ignores_non_numeric_timestamps(
    tmp_path, monkeypatch
):
    monkeypatch.setattr(Settings, "analytics_path", str(tmp_path), raising=False)
    (tmp_path / "malformed-series.json").write_text(
        '{"series": [{"x": "later", "y": 999}, {"x": 10, "y": "bad"}]}',
        encoding="utf-8",
    )

    assert get_streamer_summary("malformed-series.json") == {
        "points": 0,
        "last_activity": 10,
    }


def test_filter_datas_filters_and_sorts_chart_records():
    data = {
        "series": [
            {"x": 2000, "y": 20, "z": "Watch"},
            {"x": 1000, "y": 10, "z": "Watch"},
            {"x": 3000, "y": 30, "z": "Watch"},
        ],
        "annotations": [
            {"x": 2500, "label": "later"},
            {"x": 1500, "label": "earlier"},
        ],
    }

    result = filter_datas(None, None, data)

    assert [entry["x"] for entry in result["series"]] == [1000, 2000, 3000]
    assert [entry["x"] for entry in result["annotations"]] == [1500, 2500]


def test_filter_datas_builds_no_stream_line_from_prior_balance():
    data = {
        "series": [
            {"x": 1000, "y": 10, "z": "Watch"},
            {"x": 2000, "y": 20, "z": "Watch"},
        ]
    }

    result = filter_datas("1970-01-02", "1970-01-02", data)

    assert [entry["y"] for entry in result["series"]] == [20, 20]
    assert all(entry["z"] == "No Stream" for entry in result["series"])


def test_filter_datas_handles_non_list_series_and_annotations():
    result = filter_datas(
        None,
        None,
        {"series": 123, "annotations": {"x": 1000}},
    )

    assert result == {"series": [], "annotations": []}


def test_filter_datas_handles_non_dict_document():
    assert filter_datas(None, None, [{"x": 1000}]) == {
        "series": [],
        "annotations": [],
    }


def test_filter_datas_defaults_missing_prior_balance_to_zero():
    result = filter_datas(
        "1970-01-02",
        "1970-01-02",
        {"series": [{"x": 1000, "z": "Watch"}]},
    )

    assert [entry["y"] for entry in result["series"]] == [0, 0]


def test_filter_datas_ignores_malformed_numeric_fields():
    result = filter_datas(
        "1970-01-02",
        "1970-01-02",
        {
            "series": [
                {"x": "bad", "y": 999},
                {"x": 1000, "y": "bad"},
            ],
            "annotations": [
                {"x": "bad", "label": "invalid"},
                {"x": 120000000, "label": "valid"},
            ],
        },
    )

    assert [entry["y"] for entry in result["series"]] == [0, 0]
    assert [entry["label"] for entry in result["annotations"]] == ["valid"]


def test_points_tab_reapplies_annotations_after_becoming_visible():
    script_path = Path(__file__).resolve().parents[1] / "assets" / "script.js"
    script = script_path.read_text(encoding="utf-8")
    switch_tab = script.split("function switchDashboardTab", 1)[1].split(
        "var startDate", 1
    )[0]

    assert "requestAnimationFrame" in switch_tab
    assert "renderPointsChart();" in switch_tab
    assert "chartRendered" in switch_tab

    assert "chart.render().then" in script
    assert "switchDashboardTab(savedDashboardTab);" in script
    assert "!chartRendered || $('#points-panel').is(':hidden')" in script
    assert "pointSeries = response[\"series\"] || [];" in script


def test_analytics_error_clears_only_after_both_endpoints_recover():
    script = (
        Path(__file__).resolve().parents[1] / "assets" / "script.js"
    ).read_text(encoding="utf-8")

    assert "var pointsLoaded = false;" in script
    assert "var dropsLoaded = false;" in script
    clear_error = script.split("function clearAnalyticsLoadError", 1)[1].split(
        "function switchDashboardTab", 1
    )[0]
    assert "pointsLoaded && dropsLoaded" in clear_error
    assert "$('#analytics-load-error').text('').hide();" in clear_error

    points_request = script.split("function getStreamers", 1)[1].split(
        "function renderStreamers", 1
    )[0]
    assert "pointsLoaded = true;" in points_request
    assert "pointsLoaded = false;" in points_request

    drops_request = script.split("function getDropsByCategory", 1)[1].split(
        "function getDropTimestamp", 1
    )[0]
    assert "dropsLoaded = true;" in drops_request
    assert "dropsLoaded = false;" in drops_request


def test_analytics_external_blank_links_prevent_reverse_tabnabbing():
    template = (
        Path(__file__).resolve().parents[1] / "assets" / "charts.html"
    ).read_text(encoding="utf-8")
    blank_links = re.findall(r'<a\b[^>]*target="_blank"[^>]*>', template)

    assert blank_links
    assert all('rel="noopener noreferrer"' in link for link in blank_links)


def test_log_panel_uses_one_preference_and_starts_hidden_for_new_users():
    script = (
        Path(__file__).resolve().parents[1] / "assets" / "script.js"
    ).read_text(encoding="utf-8")

    assert script.count("$('#log').change(function ()") == 1
    assert "localStorage.getItem('logCheckboxState')" in script
    assert "localStorage.getItem('log-enabled') || 'false'" in script
    assert "var isLogCheckboxChecked = savedLogPreference === 'true';" in script
    assert "$('#log-box').toggle(isLogCheckboxChecked);" in script
    assert "$('#auto-update-log').toggle(isLogCheckboxChecked);" in script


def test_dark_theme_keeps_config_panel_headings_readable():
    stylesheet = (
        Path(__file__).resolve().parents[1] / "assets" / "dark-theme.css"
    ).read_text(encoding="utf-8")

    assert "#config-panel .title" in stylesheet
    assert "color: #fff;" in stylesheet.split("#config-panel .title", 1)[1].split(
        "}", 1
    )[0]


def test_web_config_adds_streamer_and_category_without_losing_comments(tmp_path):
    config = tmp_path / "config.py"
    config.write_text(
        'STREAMERS = [\n    Streamer("existing", settings=custom), # keep me\n]\n'
        'MINE_CONFIG = {"categories": [\n    "warframe", # keep this too\n]}\n',
        encoding="utf-8",
    )

    add_web_config_value(config, "streamers", "new_streamer")
    result = add_web_config_value(config, "categories", "arc-raiders")

    assert result == {
        "streamers": ["existing", "new_streamer"],
        "categories": ["warframe", "arc-raiders"],
    }
    updated = config.read_text(encoding="utf-8")
    assert "settings=custom" in updated
    assert "# keep me" in updated
    assert "# keep this too" in updated
    ast.parse(updated)


def test_web_config_supports_empty_inline_lists(tmp_path):
    config = tmp_path / "config.py"
    config.write_text(
        "STREAMERS = []\nMINE_CONFIG = {'categories': []}\n", encoding="utf-8"
    )

    add_web_config_value(config, "streamers", "example")
    add_web_config_value(config, "categories", "just-chatting")

    assert read_web_config(config) == {
        "streamers": ["example"],
        "categories": ["just-chatting"],
    }


def test_web_config_supports_inline_list_with_trailing_comma(tmp_path):
    config = tmp_path / "config.py"
    config.write_text(
        'STREAMERS = ["one",]\nMINE_CONFIG = {"categories": []}\n',
        encoding="utf-8",
    )

    add_web_config_value(config, "streamers", "two")

    assert read_web_config(config)["streamers"] == ["one", "two"]


def test_web_config_rejects_duplicates_and_invalid_values(tmp_path):
    config = tmp_path / "config.py"
    config.write_text(
        'STREAMERS = ["Example"]\nMINE_CONFIG = {"categories": []}\n',
        encoding="utf-8",
    )

    with pytest.raises(ConfigEditError, match="already configured"):
        add_web_config_value(config, "streamers", "example")
    with pytest.raises(ConfigEditError, match="Invalid streamer"):
        add_web_config_value(config, "streamers", "../bad")
