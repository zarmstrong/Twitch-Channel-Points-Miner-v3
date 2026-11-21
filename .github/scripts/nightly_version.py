"""Version string of a nightly build.

A nightly build is the last release plus whatever has been merged since, so
its version is the release version with build metadata appended:

    3.22.0+nightly.20261119.795629a

The miner compares only MAJOR.MINOR.PATCH when it checks for updates, so a
nightly build reports an update as soon as the next release is published.

    nightly_version.py compute --date 20261119 --sha 795629a...
    nightly_version.py stamp 3.22.0+nightly.20261119.795629a

`compute` prints the version for the checked-out source. `stamp` writes it
into TwitchChannelPointsMiner/__init__.py. The nightly workflow stamps its
own checkout before building; the change is never committed.
"""

import argparse
import re
import sys
from pathlib import Path

INIT_FILE = (
    Path(__file__).resolve().parents[2] / "TwitchChannelPointsMiner" / "__init__.py"
)

VERSION_LINE = re.compile(r'^(__version__ = ")([^"]*)(")', re.MULTILINE)
RELEASE_VERSION = re.compile(r"\d+\.\d+\.\d+")
NIGHTLY_VERSION = re.compile(r"\d+\.\d+\.\d+\+nightly\.\d{8}\.[0-9a-f]{7}")
SHORT_SHA_LENGTH = 7


def read_version(text):
    """The value of the one __version__ line in the package's __init__.py."""
    matches = VERSION_LINE.findall(text)
    if len(matches) != 1:
        raise ValueError(f"expected one __version__ line, found {len(matches)}")
    return matches[0][1]


def nightly_version(base, date, sha):
    """The nightly version for release `base`, built on `date` from `sha`."""
    if RELEASE_VERSION.fullmatch(base) is None:
        raise ValueError(f"{base!r} is not a MAJOR.MINOR.PATCH release version")
    if re.fullmatch(r"\d{8}", date) is None:
        raise ValueError(f"{date!r} is not a YYYYMMDD date")
    if re.fullmatch(r"[0-9a-f]{7,40}", sha) is None:
        raise ValueError(f"{sha!r} is not a commit SHA")
    return f"{base}+nightly.{date}.{sha[:SHORT_SHA_LENGTH]}"


def stamp(text, version):
    """`text` with its __version__ replaced by the nightly `version`."""
    if NIGHTLY_VERSION.fullmatch(version) is None:
        raise ValueError(f"{version!r} is not a nightly version")
    read_version(text)
    return VERSION_LINE.sub(lambda match: f"{match[1]}{version}{match[3]}", text)


def main(argv=None, init_file=INIT_FILE):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    commands = parser.add_subparsers(dest="command", required=True)
    compute = commands.add_parser("compute", help="print the nightly version")
    compute.add_argument("--date", required=True, help="build date, YYYYMMDD")
    compute.add_argument("--sha", required=True, help="commit being built")
    stamper = commands.add_parser("stamp", help="write a version into the package")
    stamper.add_argument("version")
    args = parser.parse_args(argv)

    # newline="" keeps the file's own line endings on Windows.
    with open(init_file, encoding="utf-8", newline="") as file:
        text = file.read()
    try:
        if args.command == "compute":
            print(nightly_version(read_version(text), args.date, args.sha))
        else:
            stamped = stamp(text, args.version)
            with open(init_file, "w", encoding="utf-8", newline="") as file:
                file.write(stamped)
            print(f"Stamped {args.version}")
    except ValueError as error:
        print(f"error: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
