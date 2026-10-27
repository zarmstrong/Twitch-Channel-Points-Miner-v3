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


def test_split_message_lines_can_count_unicode_characters():
    trophy = "\U0001f3c6"
    assert utils.split_message_lines(trophy * 5, 4, length_func=len) == [
        trophy * 4,
        trophy,
    ]
    assert utils.split_message_lines(f"{trophy}\n{trophy}\nab", 5, length_func=len) == [
        f"{trophy}\n{trophy}",
        "ab",
    ]
    assert utils.split_message_lines(trophy * 2 + "\nab", 5, length_func=len) == [
        trophy * 2 + "\nab"
    ]
    prefix = trophy + " "
    assert utils.split_message_lines(prefix + "abcd", 5, prefix, length_func=len) == [
        prefix + "abc",
        prefix + "d",
    ]


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


def test_split_message_lines_caps_the_number_of_pieces():
    message = "\n".join(f"line {index:03}" for index in range(100))

    pieces = utils.split_message_lines(message, 40, max_pieces=3)

    assert len(pieces) == 3
    assert all(len(piece) <= 40 for piece in pieces)
    assert pieces[:2] == [
        "line 000\nline 001\nline 002\nline 003",
        "line 004\nline 005\nline 006\nline 007",
    ]
    # The last kept piece gives up whole lines so the notice fits the limit.
    assert pieces[-1] == "line 008\n[22 more parts not sent]"
    # A notice that cannot fit at all is left out rather than overflowing.
    assert len(utils.split_message_lines(message, 20, max_pieces=3)) == 3
    # Under the cap nothing changes.
    assert utils.split_message_lines(message, 40) == utils.split_message_lines(
        message, 40, max_pieces=100
    )


def test_split_message_lines_cap_keeps_the_prefix_on_every_piece():
    message = "[u] " + "\n".join(f"line {index:03}" for index in range(100))

    pieces = utils.split_message_lines(message, 30, "[u] ", max_pieces=2)

    assert len(pieces) == 2
    assert all(piece.startswith("[u] ") and len(piece) <= 30 for piece in pieces)
    assert "more parts not sent]" in pieces[-1]


def test_split_message_lines_cap_trims_a_long_last_line_instead_of_dropping_it():
    message = "\n".join(["a" * 50, "s", "x" * 45, "b" * 50, "c" * 50])

    pieces = utils.split_message_lines(message, 50, max_pieces=2)

    # Only the characters the notice needs are cut from the long last line.
    assert pieces == ["a" * 50, "s\n" + "x" * 24 + "\n[2 more parts not sent]"]


def test_split_message_lines_cap_puts_the_notice_where_the_caller_says():
    # By default the notice goes on its own line, so it never reads like the
    # end of a cut line. A caller whose pieces must stay on one line (Discord's
    # inline code) asks for a space instead.
    assert utils.split_message_lines("z" * 200, 50, max_pieces=2) == [
        "z" * 50,
        "z" * 26 + "\n[2 more parts not sent]",
    ]
    assert utils.split_message_lines(
        "z" * 200, 50, max_pieces=2, notice_separator=" "
    ) == ["z" * 50, "z" * 26 + " [2 more parts not sent]"]


def test_split_message_lines_cap_trims_wide_characters_by_width():
    # 50 UTF-16 units hold 25 emoji. The notice and its line break take 24,
    # leaving room for exactly 13 emoji, no fewer.
    pieces = utils.split_message_lines("\U0001f600" * 60, 50, max_pieces=1)

    assert pieces == ["\U0001f600" * 13 + "\n[2 more parts not sent]"]
    assert utils.utf16_length(pieces[0]) == 50


def test_split_message_lines_cap_drops_blank_lines_before_the_notice():
    message = "a" * 50 + "\nb\n\n" + "c" * 60 + "\n" + "d" * 50

    pieces = utils.split_message_lines(message, 50, max_pieces=2)

    assert pieces[-1] == "b\n[3 more parts not sent]"


def test_split_message_lines_rejects_an_invalid_cap_for_any_message():
    with pytest.raises(ValueError):
        utils.split_message_lines("short", 10, max_pieces=0)


def test_split_message_lines_cap_drops_a_line_cut_down_to_spaces():
    # Found by fuzzing: cutting characters from "   bbb..." to fit the notice
    # left only its leading spaces, so the notice came after a blank line.
    message = "\n".join(["p" * 50, "a" * 24, "   " + "b" * 21, "q" * 50])

    pieces = utils.split_message_lines(message, 50, max_pieces=2)

    assert pieces == ["p" * 50, "a" * 24 + "\n[1 more part not sent]"]


@pytest.mark.parametrize("length_func", [len, utils.utf16_length])
def test_split_message_lines_cap_drops_a_one_line_piece_cut_down_to_spaces(
    length_func,
):
    # Found in review: a single-line piece that starts with a long run of
    # spaces could be cut down to only spaces, leaving the notice after them.
    message = "\n".join(["x" * 1024, " " * 1010 + "y" * 14, "z" * 1024])

    pieces = utils.split_message_lines(
        message, 1024, max_pieces=2, length_func=length_func
    )

    assert pieces == ["x" * 1024, "[1 more part not sent]"]


@pytest.mark.parametrize("separator", ["\n", " "])
def test_split_message_lines_cap_leaves_no_trailing_spaces_before_the_notice(
    separator,
):
    message = "\n".join(["p" * 40, "abc   ", "q" * 40])

    pieces = utils.split_message_lines(
        message, 40, max_pieces=2, notice_separator=separator
    )

    assert pieces[-1] == "abc" + separator + "[1 more part not sent]"
