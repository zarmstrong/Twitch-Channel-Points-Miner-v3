from types import SimpleNamespace

import requests

from TwitchChannelPointsMiner.classes.StreamSegmentWatcher import (
    StreamSegmentWatcher,
)

MASTER_URL_FRAGMENT = "usher.ttvnw.net"
MEDIA_PLAYLIST_URL = "https://cdn.example/hls/example/media.m3u8"

MASTER_PLAYLIST = (
    "#EXTM3U\n"
    "#EXT-X-STREAM-INF:BANDWIDTH=1000000,RESOLUTION=1280x720\n"
    "https://cdn.example/hls/example/720p.m3u8\n"
    "#EXT-X-STREAM-INF:BANDWIDTH=160000\n"
    "https://cdn.example/hls/example/audio_only.m3u8\n"
)

MEDIA_PLAYLIST = (
    "#EXTM3U\n"
    "#EXT-X-VERSION:3\n"
    "#EXT-X-TARGETDURATION:2\n"
    "#EXTINF:2.000,\n"
    "0.ts\n"
    "#EXTINF:2.000,\n"
    "1.ts\n"
)


class FakeResponse:
    def __init__(self, status_code, text=""):
        self.status_code = status_code
        self.text = text


def _token(value="token-value", signature="token-signature", forbidden=False):
    return SimpleNamespace(
        value=value,
        signature=signature,
        authorization=SimpleNamespace(is_forbidden=forbidden),
    )


def test_parse_media_playlist_returns_absolute_segments():
    segments = StreamSegmentWatcher.parse_playlist(
        MEDIA_PLAYLIST, MEDIA_PLAYLIST_URL, master=False
    )

    assert segments == [
        "https://cdn.example/hls/example/0.ts",
        "https://cdn.example/hls/example/1.ts",
    ]


def test_parse_master_playlist_returns_variants_only():
    playlist = (
        "#EXTM3U\n"
        "#EXT-X-STREAM-INF:BANDWIDTH=1000000\n"
        "720p.m3u8\n"
        "#EXT-X-STREAM-INF:BANDWIDTH=160000\n"
        "audio_only.m3u8\n"
        "# a comment\n"
        "https://example.test/not-a-playlist\n"
    )

    variants = StreamSegmentWatcher.parse_playlist(
        playlist, "https://usher.example/channel.m3u8", master=True
    )

    assert variants == [
        "https://usher.example/720p.m3u8",
        "https://usher.example/audio_only.m3u8",
    ]


def test_parse_playlist_rejects_non_playlist_text():
    assert (
        StreamSegmentWatcher.parse_playlist("<html>", MEDIA_PLAYLIST_URL, False) == []
    )
    assert StreamSegmentWatcher.parse_playlist("", MEDIA_PLAYLIST_URL, False) == []


def _route_get(monkeypatch, media_responses, master_response=None):
    """Patch requests.get to serve the master URL then queued media responses."""
    media_queue = list(media_responses)
    calls = {"master": 0}

    def fake_get(url, **kwargs):
        if MASTER_URL_FRAGMENT in url:
            calls["master"] += 1
            return master_response or FakeResponse(200, MASTER_PLAYLIST)
        assert media_queue, "unexpected extra media playlist request"
        return media_queue.pop(0)

    monkeypatch.setattr(requests, "get", fake_get)
    return calls


def _patch_head(monkeypatch, status=200):
    head_calls = []

    def fake_head(url, **kwargs):
        head_calls.append(url)
        return FakeResponse(status)

    monkeypatch.setattr(requests, "head", fake_head)
    return head_calls


def test_poll_resolves_playlist_and_requests_each_segment_once(monkeypatch):
    _route_get(
        monkeypatch,
        [FakeResponse(200, MEDIA_PLAYLIST), FakeResponse(200, MEDIA_PLAYLIST)],
    )
    head_calls = _patch_head(monkeypatch)
    watcher = StreamSegmentWatcher(
        "example", SimpleNamespace(get_playback_access_token=lambda _u: _token()), "ua"
    )

    assert watcher.poll(now=1000) is True
    assert head_calls == [
        "https://cdn.example/hls/example/0.ts",
        "https://cdn.example/hls/example/1.ts",
    ]

    # A second poll within the interval is skipped entirely.
    assert watcher.poll(now=1005) is False
    assert len(head_calls) == 2

    # After the interval, the same segments are not re-requested.
    assert watcher.poll(now=1011) is False
    assert len(head_calls) == 2


def test_poll_targets_last_master_variant(monkeypatch):
    requested = {}

    def fake_get(url, **kwargs):
        if MASTER_URL_FRAGMENT in url:
            return FakeResponse(200, MASTER_PLAYLIST)
        requested["media"] = url
        return FakeResponse(200, MEDIA_PLAYLIST)

    monkeypatch.setattr(requests, "get", fake_get)
    _patch_head(monkeypatch)
    watcher = StreamSegmentWatcher(
        "example", SimpleNamespace(get_playback_access_token=lambda _u: _token()), "ua"
    )

    watcher.poll(now=1000)

    assert requested["media"] == "https://cdn.example/hls/example/audio_only.m3u8"


def test_poll_skips_forbidden_playback_token(monkeypatch):
    monkeypatch.setattr(
        requests, "get", lambda *a, **k: (_ for _ in ()).throw(AssertionError())
    )
    head_calls = _patch_head(monkeypatch)
    watcher = StreamSegmentWatcher(
        "example",
        SimpleNamespace(get_playback_access_token=lambda _u: _token(forbidden=True)),
        "ua",
    )

    assert watcher.poll(now=1000) is False
    assert head_calls == []
    assert watcher.media_playlist is None
    assert watcher.last_failure == "playback access forbidden"


def test_poll_records_master_playlist_failure_reason(monkeypatch):
    monkeypatch.setattr(requests, "get", lambda *a, **k: FakeResponse(503))
    _patch_head(monkeypatch)
    watcher = StreamSegmentWatcher(
        "example", SimpleNamespace(get_playback_access_token=lambda _u: _token()), "ua"
    )

    assert watcher.poll(now=1000) is False
    assert watcher.last_failure == "master playlist HTTP 503"


def test_poll_retries_failed_segment_on_next_cycle(monkeypatch):
    _route_get(
        monkeypatch,
        [FakeResponse(200, MEDIA_PLAYLIST), FakeResponse(200, MEDIA_PLAYLIST)],
    )
    statuses = iter([500, 200, 200])
    head_calls = []

    def fake_head(url, **kwargs):
        head_calls.append(url)
        return FakeResponse(next(statuses))

    monkeypatch.setattr(requests, "head", fake_head)
    watcher = StreamSegmentWatcher(
        "example", SimpleNamespace(get_playback_access_token=lambda _u: _token()), "ua"
    )

    # First cycle: segment 0 fails, segment 1 succeeds.
    assert watcher.poll(now=1000) is True
    # Second cycle: segment 0 is retried and succeeds.
    assert watcher.poll(now=1011) is True
    assert head_calls == [
        "https://cdn.example/hls/example/0.ts",
        "https://cdn.example/hls/example/1.ts",
        "https://cdn.example/hls/example/0.ts",
    ]


def test_expired_playlist_is_re_resolved(monkeypatch):
    media_responses = [FakeResponse(404), FakeResponse(200, MEDIA_PLAYLIST)]
    calls = _route_get(monkeypatch, media_responses)
    _patch_head(monkeypatch)
    watcher = StreamSegmentWatcher(
        "example", SimpleNamespace(get_playback_access_token=lambda _u: _token()), "ua"
    )

    assert watcher.poll(now=1000) is False
    assert watcher.media_playlist is None

    assert watcher.poll(now=1070) is True
    assert calls["master"] == 2


def test_seen_segment_cache_is_bounded(monkeypatch):
    watcher = StreamSegmentWatcher("example", SimpleNamespace(), "ua")
    from TwitchChannelPointsMiner.classes.StreamSegmentWatcher import (
        SEEN_SEGMENT_LIMIT,
    )

    for index in range(SEEN_SEGMENT_LIMIT + 10):
        watcher._remember(f"segment-{index}")

    assert len(watcher.seen_segments) == SEEN_SEGMENT_LIMIT
    assert "segment-0" not in watcher.seen_segments
    assert f"segment-{SEEN_SEGMENT_LIMIT + 9}" in watcher.seen_segments


def test_network_failure_stops_requesting_remaining_segments(monkeypatch):
    _route_get(monkeypatch, [FakeResponse(200, MEDIA_PLAYLIST)])
    head_calls = []

    def fake_head(url, **kwargs):
        head_calls.append(url)
        raise requests.exceptions.ConnectTimeout()

    monkeypatch.setattr(requests, "head", fake_head)
    watcher = StreamSegmentWatcher(
        "example", SimpleNamespace(get_playback_access_token=lambda _u: _token()), "ua"
    )

    assert watcher.poll(now=1000) is False
    # Only one timeout is paid; the second segment is not attempted.
    assert head_calls == ["https://cdn.example/hls/example/0.ts"]
    assert watcher.last_failure == "segment request failed"
    assert len(watcher.seen_segments) == 0


def test_expired_segment_url_stops_requests_and_forgets_playlist(monkeypatch):
    _route_get(monkeypatch, [FakeResponse(200, MEDIA_PLAYLIST)])
    head_calls = _patch_head(monkeypatch, status=403)
    watcher = StreamSegmentWatcher(
        "example", SimpleNamespace(get_playback_access_token=lambda _u: _token()), "ua"
    )

    assert watcher.poll(now=1000) is False
    assert head_calls == ["https://cdn.example/hls/example/0.ts"]
    assert watcher.media_playlist is None
    assert watcher.last_failure == "segment HTTP 403"


def _live_playlist(sequence, count=2):
    """A rolling media playlist whose window starts at ``sequence``."""
    lines = ["#EXTM3U", f"#EXT-X-MEDIA-SEQUENCE:{sequence}"]
    for number in range(sequence, sequence + count):
        lines += ["#EXTINF:2.000,", f"{number}.ts"]
    return "\n".join(lines) + "\n"


def _serve_live_stream(monkeypatch):
    """Serve the master playlist and a live window that advances 10s per poll."""
    state = {"sequence": 0}

    def fake_get(url, **kwargs):
        if MASTER_URL_FRAGMENT in url:
            return FakeResponse(200, MASTER_PLAYLIST)
        state["sequence"] += 10
        return FakeResponse(200, _live_playlist(state["sequence"]))

    monkeypatch.setattr(requests, "get", fake_get)
    return state


def test_first_failed_poll_waits_a_full_window_before_warning(monkeypatch, caplog):
    _serve_live_stream(monkeypatch)
    _patch_head(monkeypatch)
    watcher = StreamSegmentWatcher(
        "example",
        SimpleNamespace(get_playback_access_token=lambda _u: _token(forbidden=True)),
        "ua",
    )

    with caplog.at_level("INFO"):
        for now in range(1000, 1300, 20):
            watcher.poll(now=now)
        assert not [r for r in caplog.records if r.levelname == "WARNING"]

        watcher.poll(now=1300)

    assert len([r for r in caplog.records if r.levelname == "WARNING"]) == 1


def test_stall_and_recovery_logs_carry_emoji(monkeypatch, caplog):
    _serve_live_stream(monkeypatch)
    _patch_head(monkeypatch)
    watcher = StreamSegmentWatcher(
        "example",
        SimpleNamespace(get_playback_access_token=lambda _u: _token(forbidden=True)),
        "ua",
    )

    with caplog.at_level("INFO"):
        for now in range(1000, 1320, 20):
            watcher.poll(now=now)
        watcher.gql = SimpleNamespace(get_playback_access_token=lambda _u: _token())
        for now in range(1320, 1620, 20):
            watcher.poll(now=now)

    warnings = [r for r in caplog.records if r.levelname == "WARNING"]
    recoveries = [r for r in caplog.records if "working again" in r.getMessage()]
    assert len(warnings) == 1
    assert warnings[0].emoji == ":warning:"
    assert len(recoveries) == 1
    assert recoveries[0].emoji == ":white_check_mark:"


def test_rewatch_after_gap_does_not_report_immediately(monkeypatch, caplog):
    _serve_live_stream(monkeypatch)
    _patch_head(monkeypatch)
    watcher = StreamSegmentWatcher(
        "example",
        SimpleNamespace(get_playback_access_token=lambda _u: _token(forbidden=True)),
        "ua",
    )

    watcher.last_used = 1000

    with caplog.at_level("INFO"):
        # Watched for under a report window, rotated out, then watched again
        # well after the window: the re-watch gets its own full window.
        for now in range(1000, 1200, 20):
            watcher.start_poll(now=now).join(5)
        watcher.start_poll(now=2000).join(5)

    assert not [r for r in caplog.records if r.levelname == "WARNING"]
    assert watcher.last_report == 2000


def test_slow_polls_still_report_a_stall(monkeypatch, caplog):
    _serve_live_stream(monkeypatch)
    _patch_head(monkeypatch)
    watcher = StreamSegmentWatcher(
        "example",
        SimpleNamespace(get_playback_access_token=lambda _u: _token(forbidden=True)),
        "ua",
    )

    with caplog.at_level("INFO"):
        # Polls 90s apart (each one slow) while the stream stays watched must
        # not keep reopening the report window.
        for now in range(1000, 1400, 90):
            watcher.poll(now=now)

    assert len([r for r in caplog.records if r.levelname == "WARNING"]) == 1


def test_missed_segments_between_polls_are_measured(monkeypatch, caplog):
    _serve_live_stream(monkeypatch)
    _patch_head(monkeypatch)
    watcher = StreamSegmentWatcher(
        "example", SimpleNamespace(get_playback_access_token=lambda _u: _token()), "ua"
    )

    with caplog.at_level("DEBUG"):
        for now in range(1000, 1320, 20):
            watcher.poll(now=now)

    # Each poll lists 2 segments but the window advances 10, so 8 are missed
    # between every pair of consecutive polls.
    reports = [r for r in caplog.records if "missed between polls" in r.getMessage()]
    assert len(reports) == 1
    assert "playlist window 2-2 segments, 120 missed" in reports[0].getMessage()


def test_parse_media_sequence():
    assert StreamSegmentWatcher.parse_media_sequence(_live_playlist(42)) == 42
    assert StreamSegmentWatcher.parse_media_sequence(MEDIA_PLAYLIST) is None


def test_start_poll_skips_while_previous_poll_is_running(monkeypatch):
    import threading

    release = threading.Event()
    calls = []

    def slow_poll(self, now=None):
        calls.append(now)
        release.wait(5)

    monkeypatch.setattr(StreamSegmentWatcher, "poll", slow_poll)
    watcher = StreamSegmentWatcher("example", SimpleNamespace(), "ua")

    first = watcher.start_poll(now=1000)
    assert first is not None
    assert watcher.start_poll(now=1020) is None
    release.set()
    first.join(5)

    second = watcher.start_poll(now=1040)
    assert second is not None
    second.join(5)
    assert len(calls) == 2


def test_start_poll_logs_and_swallows_poll_errors(monkeypatch, caplog):
    def boom(self, now=None):
        raise ValueError("unexpected")

    monkeypatch.setattr(StreamSegmentWatcher, "poll", boom)
    watcher = StreamSegmentWatcher("example", SimpleNamespace(), "ua")

    with caplog.at_level("WARNING"):
        watcher.start_poll().join(5)

    records = [r for r in caplog.records if "Unable to request" in r.getMessage()]
    assert len(records) == 1
    assert records[0].exc_info is not None
    assert records[0].emoji == ":warning:"


def test_is_idle_after_expiry_without_polls():
    from TwitchChannelPointsMiner.classes.StreamSegmentWatcher import (
        IDLE_EXPIRY_SECONDS,
    )

    watcher = StreamSegmentWatcher("example", SimpleNamespace(), "ua")
    watcher.last_used = 1000

    assert watcher.is_idle(now=1000 + IDLE_EXPIRY_SECONDS) is False
    assert watcher.is_idle(now=1001 + IDLE_EXPIRY_SECONDS) is True


def test_evicted_segment_404_keeps_playlist_and_continues(monkeypatch):
    _route_get(monkeypatch, [FakeResponse(200, MEDIA_PLAYLIST)])
    statuses = iter([404, 200])
    head_calls = []

    def fake_head(url, **kwargs):
        head_calls.append(url)
        return FakeResponse(next(statuses))

    monkeypatch.setattr(requests, "head", fake_head)
    watcher = StreamSegmentWatcher(
        "example", SimpleNamespace(get_playback_access_token=lambda _u: _token()), "ua"
    )

    assert watcher.poll(now=1000) is True
    assert head_calls == [
        "https://cdn.example/hls/example/0.ts",
        "https://cdn.example/hls/example/1.ts",
    ]
    assert watcher.media_playlist == "https://cdn.example/hls/example/audio_only.m3u8"


def test_expired_cached_playlist_is_re_resolved_in_the_same_poll(monkeypatch):
    calls = _route_get(
        monkeypatch,
        [
            FakeResponse(200, MEDIA_PLAYLIST),
            FakeResponse(403),
            FakeResponse(200, MEDIA_PLAYLIST.replace("1.ts", "2.ts")),
        ],
    )
    head_calls = _patch_head(monkeypatch)
    watcher = StreamSegmentWatcher(
        "example", SimpleNamespace(get_playback_access_token=lambda _u: _token()), "ua"
    )

    assert watcher.poll(now=1000) is True
    # Well inside the old 60s resolve cooldown: the expiry is recovered from
    # immediately instead of losing this poll.
    assert watcher.poll(now=1030) is True
    assert calls["master"] == 2
    assert head_calls[-1] == "https://cdn.example/hls/example/2.ts"


def test_rejected_fresh_playlist_backs_off_before_resolving_again(monkeypatch):
    calls = _route_get(monkeypatch, [FakeResponse(403), FakeResponse(403)])
    _patch_head(monkeypatch)
    watcher = StreamSegmentWatcher(
        "example", SimpleNamespace(get_playback_access_token=lambda _u: _token()), "ua"
    )

    assert watcher.poll(now=1000) is False
    assert watcher.poll(now=1020) is False
    assert calls["master"] == 1
    assert watcher.poll(now=1061) is False
    assert calls["master"] == 2


def test_segment_head_follows_redirects_and_accepts_any_2xx(monkeypatch):
    _route_get(monkeypatch, [FakeResponse(200, MEDIA_PLAYLIST)])
    head_kwargs = []

    def fake_head(url, **kwargs):
        head_kwargs.append(kwargs)
        return FakeResponse(206)

    monkeypatch.setattr(requests, "head", fake_head)
    watcher = StreamSegmentWatcher(
        "example", SimpleNamespace(get_playback_access_token=lambda _u: _token()), "ua"
    )

    assert watcher.poll(now=1000) is True
    assert all(kwargs.get("allow_redirects") is True for kwargs in head_kwargs)
    assert len(watcher.seen_segments) == 2
