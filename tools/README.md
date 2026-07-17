# Developer tools

These standalone utilities inspect Twitch Drops data and are not imported by
the miner at runtime:

- `drops_gql_probe.py` captures raw Drops-related Twitch GraphQL responses.
- `inspect_twitchdrops_har.py` extracts campaign and reward data from a HAR.
- `scrape_twitch_drop_campaigns.py` builds a cached badge campaign report.
- `twitchdrops_app_scraper.py` inspects a single twitchdrops.app game page.

Run them from the repository root with the project virtual environment, for
example:

```sh
.venv/bin/python tools/twitchdrops_app_scraper.py --help
```
