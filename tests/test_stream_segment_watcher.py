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
    assert len(watcher.seen_order) == SEEN_SEGMENT_LIMIT
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
    assert watcher.seen_segments == set()


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


def test_stall_and_recovery_logs_carry_emoji(monkeypatch, caplog):
    _route_get(monkeypatch, [FakeResponse(200, MEDIA_PLAYLIST)])
    _patch_head(monkeypatch)
    watcher = StreamSegmentWatcher(
        "example",
        SimpleNamespace(get_playback_access_token=lambda _u: _token(forbidden=True)),
        "ua",
    )

    with caplog.at_level("INFO"):
        watcher.poll(now=1000)
        watcher.gql = SimpleNamespace(get_playback_access_token=lambda _u: _token())
        watcher.poll(now=1300)

    warnings = [r for r in caplog.records if r.levelname == "WARNING"]
    recoveries = [r for r in caplog.records if "working again" in r.getMessage()]
    assert len(warnings) == 1
    assert warnings[0].emoji == ":warning:"
    assert len(recoveries) == 1
    assert recoveries[0].emoji == ":white_check_mark:"
