import re
from pathlib import Path

from TwitchChannelPointsMiner.classes.AnalyticsServer import (
    MAX_LOG_TAIL_BYTES,
    bounded_log_start,
    filter_datas,
    get_streamer_summary,
)
from TwitchChannelPointsMiner.classes.Settings import Settings


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


def test_filter_datas_defaults_missing_prior_balance_to_zero():
    result = filter_datas(
        "1970-01-02",
        "1970-01-02",
        {"series": [{"x": 1000, "z": "Watch"}]},
    )

    assert [entry["y"] for entry in result["series"]] == [0, 0]


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
