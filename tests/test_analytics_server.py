import re
from io import BytesIO
from pathlib import Path
from types import SimpleNamespace

from TwitchChannelPointsMiner.classes.AnalyticsServer import (
    AnalyticsServer,
    MAX_LOG_TAIL_BYTES,
    UPDATE_DISMISSAL_COOKIE,
    bounded_log_start,
    filter_datas,
    get_streamer_summary,
    seek_log_start,
)
from TwitchChannelPointsMiner.classes.Settings import Settings


def test_bounded_log_start_caps_legacy_request_without_tail_bytes():
    file_size = MAX_LOG_TAIL_BYTES * 10

    assert bounded_log_start(file_size, 0) == file_size - MAX_LOG_TAIL_BYTES


def test_config_endpoints_require_analytics_authentication():
    server = AnalyticsServer(password=None)

    read_response = server.app.test_client().get("/config")
    response = server.app.test_client().post(
        "/config", json={"action": "add", "kind": "streamers", "value": "one"}
    )
    notification_response = server.app.test_client().post(
        "/config/notifications/discord/test"
    )

    assert read_response.status_code == 403
    assert response.status_code == 403
    assert notification_response.status_code == 403
    assert "analytics username and password" in read_response.get_json()["error"]


def test_dashboard_shows_version_update_banner_and_footer(monkeypatch):
    monkeypatch.setattr(Settings, "logger", SimpleNamespace(date_format="dd/mm/yy"))
    monkeypatch.setattr(Settings, "latest_release_version", "3.8.0", raising=False)
    monkeypatch.setattr(
        Settings,
        "update_instructions",
        "Pull the latest image and recreate the container.",
        raising=False,
    )
    server = AnalyticsServer(password=None)

    response = server.app.test_client().get("/")
    page = response.get_data(as_text=True)

    assert response.status_code == 200
    assert 'id="update-available-banner"' in page
    assert "Version 3.8.0 is available." in page
    assert "Pull the latest image and recreate the container." in page
    assert "Running version" in page
    assert "Upgrade available: 3.8.0" in page
    assert "Tkd-Alex" in page


def test_dashboard_hides_banner_for_dismissed_version_but_keeps_footer(monkeypatch):
    monkeypatch.setattr(Settings, "logger", SimpleNamespace(date_format="dd/mm/yy"))
    monkeypatch.setattr(Settings, "latest_release_version", "3.8.0", raising=False)
    monkeypatch.setattr(Settings, "update_instructions", "Upgrade now.", raising=False)
    server = AnalyticsServer(password=None)
    client = server.app.test_client()
    client.set_cookie(UPDATE_DISMISSAL_COOKIE, "3.8.0")

    page = client.get("/").get_data(as_text=True)

    assert 'id="update-available-banner"' not in page
    assert "Upgrade available: 3.8.0" in page


def test_authenticated_config_writes_reach_the_endpoint(tmp_path, monkeypatch):
    monkeypatch.setattr(Settings, "config_path", str(tmp_path), raising=False)
    monkeypatch.setitem(
        __import__(
            "TwitchChannelPointsMiner.classes.AnalyticsServer", fromlist=["web_config"]
        ).web_config.__globals__,
        "update_managed_web_config",
        lambda _path, _payload: {"streamers": []},
    )
    server = AnalyticsServer(username="user", password="secret")

    response = server.app.test_client().post(
        "/config",
        json={"action": "add", "kind": "streamers", "value": "one"},
        headers={"Authorization": "Basic dXNlcjpzZWNyZXQ="},
    )

    assert response.status_code == 200


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
    assert "#config-panel .config-item-name" in stylesheet
    assert "#config-panel .config-item strong" in stylesheet
    assert "#config-panel .input::placeholder" in stylesheet


def test_successful_config_message_fades_after_ten_seconds():
    script = (
        Path(__file__).resolve().parents[1] / "assets" / "script.js"
    ).read_text(encoding="utf-8")
    show_message = script.split("function showConfigMessage", 1)[1].split(
        "function loadWebConfig", 1
    )[0]

    assert "if (!isError)" in show_message
    assert "clearTimeout(configMessageTimeout);" in show_message
    assert "$('#config-message').fadeOut(250);" in show_message
    assert "}, 10000);" in show_message


def test_config_ui_exposes_requested_management_controls():
    root = Path(__file__).resolve().parents[1]
    template = (root / "assets" / "charts.html").read_text(encoding="utf-8")
    script = (root / "assets" / "script.js").read_text(encoding="utf-8")

    for selector in (
        "category-settings-form",
        "source-settings-form",
        "logging-settings-form",
        "update-settings-form",
        "notification-settings",
    ):
        assert f'id="{selector}"' in template
    for setting in (
        "favorite",
        "make_predictions",
        "follow_raid",
        "claim_drops",
        "claim_moments",
        "chat",
        "points_limit",
    ):
        assert setting in script
    assert "reorder_categories" in script
    assert "remove-streamer" in script
    assert "data-secret" in script
    assert "Configured — leave blank to keep" in script
    assert "test-notification" in script
    assert "update_check" in template.lower().replace("-", "_")
    assert "update_updates" in script
    assert "/config/notifications/${encodeURIComponent(provider)}/test" in script
    assert "'aria-label': `Move ${category} up`" in script
    assert "'aria-label': `Move ${category} down`" in script


def test_notification_forms_do_not_nest_two_column_grids():
    stylesheet = (
        Path(__file__).resolve().parents[1] / "assets" / "style.css"
    ).read_text(encoding="utf-8")
    notification_fields = stylesheet.split(".notification-fields {", 1)[1].split(
        "}", 1
    )[0]

    assert "grid-template-columns: minmax(0, 1fr);" in notification_fields
    assert ".notification-config" in stylesheet
    assert ".notification-fields .input" in stylesheet
    assert "min-width: 0;" in stylesheet


def test_notification_events_use_clickable_capsules():
    root = Path(__file__).resolve().parents[1]
    script = (root / "assets" / "script.js").read_text(encoding="utf-8")
    stylesheet = (root / "assets" / "style.css").read_text(encoding="utf-8")

    assert "config.notification_event_options" in script
    assert "event-capsules" in script
    assert "event-capsule" in script
    assert "aria-pressed" in script
    assert ".event-capsule[aria-pressed=\"true\"]" in script
    assert ".event-capsules" in stylesheet
