#!/usr/bin/env python

"""Safely display a JSON cookie file created by the miner."""

import json
import sys

if __name__ == "__main__":
    argv = sys.argv
    if len(argv) <= 1:
        print("Specify a cookie file, e.g. cookies/user.pkl")
    else:
        try:
            with open(argv[1], encoding="utf-8") as cookie_file:
                cookies = json.load(cookie_file)
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
            raise SystemExit(
                "Unable to read this as a JSON cookie file. Start the miner once "
                f"to safely migrate a legacy cookie file: {error}"
            ) from error
        print(json.dumps(cookies, indent=2))
