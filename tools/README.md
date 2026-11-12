# Developer tools

These standalone utilities are not imported by the miner at runtime:

- `drops_gql_probe.py` captures raw Drops-related Twitch GraphQL responses.
- `inspect_twitchdrops_har.py` extracts campaign and reward data from a HAR.
- `scrape_twitch_drop_campaigns.py` builds a cached badge campaign report.
- `twitchdrops_app_scraper.py` inspects a single twitchdrops.app game page.
- `view_cookie.py` pretty-prints a saved JSON cookie file, for example
  `python tools/view_cookie.py cookies/user.pkl`. The output contains your
  Twitch session; don't share it.

Run them from the repository root with the project virtual environment, for
example:

```sh
.venv/bin/python tools/twitchdrops_app_scraper.py --help
```
