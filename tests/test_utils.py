from types import SimpleNamespace

import pytest

from TwitchChannelPointsMiner import utils


@pytest.mark.parametrize(
    ("value", "total", "expected"),
    [(0, 10, 0), (1, 4, 25), (2, 3, 66), (10, 10, 100)],
)
def test_percentage(value, total, expected):
    assert utils.percentage(value, total) == expected


def test_create_nonce_has_requested_length_and_alphanumeric_characters():
    nonce = utils.create_nonce(64)

    assert len(nonce) == 64
    assert nonce.isalnum()


def test_create_chunks_preserves_order_and_remainder():
    assert utils.create_chunks([1, 2, 3, 4, 5], 2) == [[1, 2], [3, 4], [5]]


def test_get_streamer_index_compares_channel_ids_as_strings():
    streamers = [SimpleNamespace(channel_id="10"), SimpleNamespace(channel_id=20)]

    assert utils.get_streamer_index(streamers, 10) == 0
    assert utils.get_streamer_index(streamers, "20") == 1
    assert utils.get_streamer_index(streamers, "missing") == -1


def test_set_default_settings_copies_defaults_without_sharing_them():
    defaults = SimpleNamespace(enabled=True, limit=10)

    first = utils.set_default_settings(None, defaults)
    first.limit = 20

    assert defaults.limit == 10


def test_copy_values_if_none_preserves_explicit_values():
    settings = SimpleNamespace(enabled=False, limit=None)
    defaults = SimpleNamespace(enabled=True, limit=10)

    result = utils.copy_values_if_none(settings, defaults)

    assert result.enabled is False
    assert result.limit == 10


def test_remove_emoji_leaves_plain_text():
    assert utils.remove_emoji("Live now! " + chr(0x1F389)) == "Live now! "
