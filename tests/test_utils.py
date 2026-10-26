from types import SimpleNamespace

import pytest

from TwitchChannelPointsMiner import __version__, utils


@pytest.mark.parametrize(
    ("value", "total", "expected"),
    [(0, 10, 0), (1, 0, 0), (1, 4, 25), (2, 3, 66), (10, 10, 100)],
)
def test_percentage(value, total, expected):
    assert utils.percentage(value, total) == expected


def test_create_nonce_has_requested_length_and_alphanumeric_characters():
    nonce = utils.create_nonce(64)

    assert len(nonce) == 64
    assert nonce.isalnum()


def test_create_chunks_preserves_order_and_remainder():
    assert utils.create_chunks([1, 2, 3, 4, 5], 2) == [[1, 2], [3, 4], [5]]


def test_split_message_lines_keeps_short_message_whole():
    assert utils.split_message_lines("one\ntwo", 10) == ["one\ntwo"]
    assert utils.split_message_lines("", 10) == [""]


def test_split_message_lines_breaks_on_line_boundaries():
    assert utils.split_message_lines("aaa\nbbb\nccc\ndd", 7) == [
        "aaa\nbbb",
        "ccc\ndd",
    ]


def test_split_message_lines_hard_wraps_overlong_lines():
    assert utils.split_message_lines("abcdefgh\nij", 3) == [
        "abc",
        "def",
        "gh",
        "ij",
    ]
    assert utils.split_message_lines("abcdef\nx", 3) == ["abc", "def", "x"]


def test_split_message_lines_drops_empty_trailing_pieces():
    assert utils.split_message_lines("a" * 10 + "\n", 10) == ["a" * 10]
    assert utils.split_message_lines("a" * 10 + "\n\n", 10) == ["a" * 10]
    assert utils.split_message_lines("a\n\n\nb", 1) == ["a", "b"]
    assert utils.split_message_lines("a\nb\n\n\n", 1) == ["a", "b"]
    assert utils.split_message_lines("\n\n", 10) == ["\n\n"]
    assert utils.split_message_lines("\nabc", 10) == ["\nabc"]
    assert utils.split_message_lines("\n" * 12, 10) == [""]


def test_split_message_lines_drops_leading_blank_lines_of_long_message():
    pieces = utils.split_message_lines("\n" + "x" * 5000, 4096)

    assert [len(piece) for piece in pieces] == [4096, 904]


def test_split_message_lines_never_emits_blank_part_mid_message():
    # The blank lines after "aaaa" cannot join it, and must not form a part
    # of their own when the next line is too long to join them either.
    pieces = utils.split_message_lines("aaaa\n\n\n\nbbbbbbbb\ncc", 8)

    assert pieces == ["aaaa\n\n\n", "bbbbbbbb", "cc"]
    assert all(piece.strip() for piece in pieces)


def test_split_message_lines_keeps_blank_lines_inside_a_part():
    assert utils.split_message_lines("a\n\nb\n" + "c" * 6, 6) == [
        "a\n\nb",
        "c" * 6,
    ]


def test_split_message_lines_repeats_prefix_on_later_pieces():
    pieces = utils.split_message_lines("[u] aaaa\nbbbb\ncccc", 9, prefix="[u] ")

    assert pieces == ["[u] aaaa", "[u] bbbb", "[u] cccc"]
    assert all(len(piece) <= 9 for piece in pieces)
    assert utils.split_message_lines("[u] short", 20, prefix="[u] ") == ["[u] short"]
    assert utils.split_message_lines("aaaa\nbbbb", 5, prefix="[u] ") == [
        "aaaa",
        "bbbb",
    ]


def test_split_message_lines_skips_blank_segments_of_wrapped_lines():
    padded = "a" + " " * 10 + "b"
    pieces = utils.split_message_lines(padded, 5)

    assert pieces == ["a    ", " b"]
    assert all(piece.strip() for piece in pieces)
    assert utils.split_message_lines("[u] " + padded, 9, prefix="[u] ") == [
        "[u] a    ",
        "[u]  b",
    ]
    assert utils.split_message_lines("x\n     \ny", 5) == ["x", "y"]


def test_split_message_lines_measures_utf16_code_units():
    trophy = "\U0001f3c6"
    assert utils.utf16_length(trophy * 3 + "ab") == 8
    assert utils.split_message_lines(trophy * 5, 4) == [trophy * 2, trophy * 2, trophy]
    assert utils.split_message_lines(f"{trophy}\n{trophy}\nab", 5) == [
        f"{trophy}\n{trophy}",
        "ab",
    ]


def test_split_message_lines_rejects_non_positive_limit():
    with pytest.raises(ValueError):
        utils.split_message_lines("abc", 0)


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


def test_package_utils_read_version_from_package_root():
    version_data = utils.init2dict(utils.read("__init__.py"))

    assert version_data["version"] == __version__


def test_read_closes_file_handle(monkeypatch):
    closed = []

    class FakeFile:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            closed.append(True)

        def read(self):
            return "contents"

    monkeypatch.setattr("builtins.open", lambda *args, **kwargs: FakeFile())

    assert utils.read("anything") == "contents"
    assert closed == [True]
