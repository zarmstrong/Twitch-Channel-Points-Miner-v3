import re
import socket
import time
from collections.abc import Callable
from copy import deepcopy
from datetime import datetime, timezone
from os import path
from random import randrange

import requests
from millify import millify as package_millify

from TwitchChannelPointsMiner.constants import GITHUB_LATEST_RELEASE_API, USER_AGENTS


def millify(input, precision=2):
    return package_millify(input, precision)


# Backward-compatible alias for callers not yet migrated to the public name.
def _millify(input, precision=2):
    return millify(input, precision)


def get_streamer_index(streamers: list, channel_id) -> int:
    try:
        return next(
            i for i, x in enumerate(streamers) if str(x.channel_id) == str(channel_id)
        )
    except StopIteration:
        return -1


def float_round(number, ndigits=2):
    return round(float(number), ndigits)


def server_time(message_data):
    return (
        datetime.fromtimestamp(message_data["server_time"], timezone.utc).isoformat()
        + "Z"
        if message_data is not None and "server_time" in message_data
        else datetime.fromtimestamp(time.time(), timezone.utc).isoformat() + "Z"
    )


# https://en.wikipedia.org/wiki/Cryptographic_nonce
def create_nonce(length=30) -> str:
    nonce = ""
    for i in range(length):
        char_index = randrange(0, 10 + 26 + 26)
        if char_index < 10:
            char = chr(ord("0") + char_index)
        elif char_index < 10 + 26:
            char = chr(ord("a") + char_index - 10)
        else:
            char = chr(ord("A") + char_index - 26 - 10)
        nonce += char
    return nonce


# for mobile-token


def get_user_agent(browser: str) -> str:
    """try:
        return USER_AGENTS[platform.system()][browser]
    except KeyError:
        # return USER_AGENTS["Linux"]["FIREFOX"]
        # return USER_AGENTS["Windows"]["CHROME"]"""
    return USER_AGENTS["Android"]["TV"]
    # return USER_AGENTS["Android"]["App"]


def remove_emoji(string: str) -> str:
    emoji_pattern = re.compile(
        "["
        "\U0001f600-\U0001f64f"  # emoticons
        "\U0001f300-\U0001f5ff"  # symbols & pictographs
        "\U0001f680-\U0001f6ff"  # transport & map symbols
        "\U0001f1e0-\U0001f1ff"  # flags (iOS)
        "\U00002500-\U00002587"  # chinese char
        "\U00002589-\U00002bef"  # I need Unicode Character “█” (U+2588)
        "\U00002702-\U000027b0"
        "\U00002702-\U000027b0"
        "\U000024c2-\U00002587"
        "\U00002589-\U0001f251"
        "\U0001f926-\U0001f937"
        "\U00010000-\U0010ffff"
        "\u2640-\u2642"
        "\u2600-\u2b55"
        "\u200d"
        "\u23cf"
        "\u23e9"
        "\u231a"
        "\ufe0f"  # dingbats
        "\u3030"
        "\u231b"
        "\u2328"
        "\u23cf"
        "\u23e9"
        "\u23ea"
        "\u23eb"
        "\u23ec"
        "\u23ed"
        "\u23ee"
        "\u23ef"
        "\u23f0"
        "\u23f1"
        "\u23f2"
        "\u23f3"
        "]+",
        flags=re.UNICODE,
    )
    return emoji_pattern.sub(r"", string)


def at_least_one_value_in_settings_is(items, attr, value=True):
    for item in items:
        if getattr(item.settings, attr) == value:
            return True
    return False


def copy_values_if_none(settings, defaults):
    values = list(
        filter(
            lambda x: x.startswith("__") is False
            and callable(getattr(settings, x)) is False,
            dir(settings),
        )
    )

    for value in values:
        if getattr(settings, value) is None:
            setattr(settings, value, getattr(defaults, value))
    return settings


def set_default_settings(settings, defaults):
    # If no settings was provided use the default settings ...
    # If settings was provided but maybe are only partial set
    # Get the default values from Settings.streamer_settings
    return (
        deepcopy(defaults)
        if settings is None
        else copy_values_if_none(settings, defaults)
    )


"""def char_decision_as_index(char):
    return 0 if char == "A" else 1"""


def internet_connection_available(host="8.8.8.8", port=53, timeout=3):
    try:
        socket.setdefaulttimeout(timeout)
        socket.socket(socket.AF_INET, socket.SOCK_STREAM).connect((host, port))
        return True
    except socket.error:
        return False


def percentage(a, b):
    return 0 if b == 0 else int((a / b) * 100)


def create_chunks(lst, n):
    return [lst[i : (i + n)] for i in range(0, len(lst), n)]  # noqa: E203


def _utf16_width(character: str) -> int:
    return 2 if ord(character) > 0xFFFF else 1


def utf16_length(text: str) -> int:
    """Return the length of ``text`` in UTF-16 code units.

    Discord counts message length this way, so characters outside the Basic
    Multilingual Plane (most emoji) count twice.
    """
    return sum(_utf16_width(character) for character in text)


def _wrap_line(
    line: str, limit: int, length_func: Callable[[str], int]
) -> list[tuple[str, int]]:
    """Hard-wrap ``line`` into ``(segment, length)`` pairs within ``limit``."""
    segments = []
    start = 0
    length = 0
    for index, character in enumerate(line):
        width = length_func(character)
        if length and length + width > limit:
            segments.append((line[start:index], length))
            start, length = index, 0
        length += width
    segments.append((line[start:], length))
    return segments


def split_message_lines(
    message: str,
    limit: int,
    prefix: str = "",
    *,
    length_func: Callable[[str], int] = utf16_length,
    max_pieces: int | None = None,
    notice_separator: str = "\n",
) -> list[str]:
    """Split a message into pieces of at most ``limit`` units.

    Length defaults to UTF-16 code units. Pass ``length_func=len`` for services
    such as Telegram and Pushover that count Unicode characters instead.

    A message that already fits is returned unchanged as the only piece.
    Otherwise pieces break on line boundaries whenever possible, and a single
    line that is longer than ``limit`` on its own is hard-wrapped. Blank lines
    are kept inside a piece but never start one. Whitespace-only segments from
    hard-wrapping are also skipped when they would start a piece, so a long
    whitespace run cannot produce a blank notification.

    When the message starts with ``prefix`` (such as the username the logger
    adds), every later piece starts with it too, within the limit.

    With ``max_pieces``, at most that many pieces are returned; the last one
    ends with a notice saying how many pieces were left out, trimmed to fit.
    A notice longer than ``limit`` on its own is left out.
    The notice follows ``notice_separator``: a new line by default, or a
    space where the pieces must stay on one line (Discord's inline code).
    """
    if limit < 1:
        raise ValueError("limit must be a positive integer")
    if max_pieces is not None and max_pieces < 1:
        raise ValueError("max_pieces must be a positive integer")
    if length_func(message) <= limit:
        return [message]
    if prefix and message.startswith(prefix):
        prefix_length = length_func(prefix)
        if prefix_length <= limit // 2:
            body = message[len(prefix) :]  # noqa: E203
            body_limit = limit - prefix_length
            pieces = _truncate_pieces(
                _split_lines(body, body_limit, length_func),
                body_limit,
                max_pieces,
                length_func,
                notice_separator,
            )
            return [prefix + piece for piece in pieces]
    return _truncate_pieces(
        _split_lines(message, limit, length_func),
        limit,
        max_pieces,
        length_func,
        notice_separator,
    )


def _truncate_pieces(
    pieces: list[str],
    limit: int,
    max_pieces: int | None,
    length_func: Callable[[str], int],
    separator: str = "\n",
) -> list[str]:
    """Keep at most ``max_pieces`` pieces, ending the last with a notice."""
    if max_pieces is None or len(pieces) <= max_pieces:
        return pieces
    omitted = len(pieces) - max_pieces
    notice = f"[{omitted} more part{'s' if omitted != 1 else ''} not sent]"
    kept = pieces[:max_pieces]
    if length_func(notice) > limit:
        return kept
    # Trailing whitespace (blank lines, or spaces a cut leaves behind) would
    # put the notice after an empty line or a visible gap.
    last = kept[-1].rstrip()
    notice_length = length_func(separator) + length_func(notice)
    # Make room by dropping trailing lines whole while they are short or fit
    # inside the overflow, so no short line is left cut off mid-way, and by
    # cutting characters only from a long last line, which keeps all it can.
    while last and length_func(last) + notice_length > limit:
        overflow = length_func(last) + notice_length - limit
        head, newline, tail = last.rpartition("\n")
        tail_length = length_func(tail)
        if newline and (tail_length <= overflow or tail_length <= notice_length):
            last = head.rstrip()
        else:
            # Free `overflow` units, counted per character: a character can
            # be wider than one unit (an emoji is two UTF-16 units).
            cut, freed = len(last), 0
            while cut > 0 and freed < overflow:
                cut -= 1
                freed += length_func(last[cut])
            last = last[:cut].rstrip()
    kept[-1] = f"{last}{separator}{notice}" if last else notice
    return kept


def _split_lines(
    message: str, limit: int, length_func: Callable[[str], int]
) -> list[str]:
    pieces = []
    current = []
    current_length = 0
    for line in message.split("\n"):
        for segment, segment_length in _wrap_line(line, limit, length_func):
            if current and current_length + 1 + segment_length > limit:
                pieces.append("\n".join(current))
                current, current_length = [], 0
            if not current and not segment.strip():
                continue
            current_length += segment_length + (1 if current else 0)
            current.append(segment)
    if current:
        pieces.append("\n".join(current))
    return pieces or [""]


def read(fname):
    package_root = path.dirname(path.dirname(__file__))
    with open(path.join(package_root, fname), encoding="utf-8") as file:
        return file.read()


def init2dict(content):
    return dict(re.findall(r"""__([a-z]+)__ = "([^"]+)""", content))


def is_newer_version(candidate, current):
    """Return whether a SemVer candidate is newer than the current version."""
    version_pattern = r"^(\d+)\.(\d+)\.(\d+)(?:[-+].*)?$"
    candidate_match = re.match(version_pattern, candidate)
    current_match = re.match(version_pattern, current)
    if candidate_match is None or current_match is None:
        return False

    candidate_parts = tuple(int(part) for part in candidate_match.groups())
    current_parts = tuple(int(part) for part in current_match.groups())
    return candidate_parts > current_parts


def check_versions():
    try:
        current_version = init2dict(read("__init__.py"))
        current_version = (
            current_version["version"] if "version" in current_version else "0.0.0"
        )
    except Exception:
        current_version = "0.0.0"
    try:
        response = requests.get(
            GITHUB_LATEST_RELEASE_API,
            headers={
                "Accept": "application/vnd.github+json",
                "User-Agent": f"Twitch-Channel-Points-Miner/{current_version}",
            },
            timeout=(5, 15),
        )
        response.raise_for_status()
        tag_name = response.json().get("tag_name")
        github_version = tag_name.removeprefix("v") if isinstance(tag_name, str) else ""
        if re.fullmatch(r"\d+\.\d+\.\d+(?:[-+].*)?", github_version) is None:
            github_version = "0.0.0"
    except Exception:
        github_version = "0.0.0"
    return current_version, github_version
