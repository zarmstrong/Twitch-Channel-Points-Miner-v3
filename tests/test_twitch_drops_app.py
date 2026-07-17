from TwitchChannelPointsMiner.classes.TwitchDropsApp import (
    parse_front_page,
    parse_game_page,
)


def test_single_campaign_accepts_watch_drop_without_campaign_label():
    source = """
    <main><h1>Example Game</h1></main>
    <div class="tab-content tab-viewer active">
      <div class="drop-card">
        <img src="reward.png">
        <div class="drop-name">Example Watch Reward</div>
        <div class="drop-time">Watch 2h</div>
      </div>
      <h2>Active Campaigns</h2>
      <div class="campaign-banner">
        <span class="cb-name">Single Active Campaign</span>
        <span class="cb-owner">Example Publisher</span>
        <span class="cb-dates">Jul 1 — Jul 31</span>
        <span class="cb-timer" data-end-ts="1784087999999"></span>
        <div class="cb-channels">
          <a href="https://twitch.tv/ExampleStreamer">ExampleStreamer</a>
        </div>
      </div>
      <h2>How to get these drops</h2>
    </div>
    """

    report = parse_game_page(source, "https://twitchdrops.app/game/example-game")

    assert report["campaign_count"] == 1
    assert report["campaigns"][0]["name"] == "Single Active Campaign"
    assert report["campaigns"][0]["channels"] == ["examplestreamer"]
    assert [drop["name"] for drop in report["campaigns"][0]["drops"]] == [
        "Example Watch Reward"
    ]


def test_multiple_campaigns_do_not_guess_unlabeled_drop_ownership():
    source = """
    <main><h1>Example Game</h1></main>
    <div class="drop-card">
      <div class="drop-name">Unlabeled Reward</div>
      <div class="drop-time">Watch 1h</div>
    </div>
    <h2>Active Campaigns</h2>
    <div class="campaign-banner">
      <span class="cb-name">Campaign One</span>
      <span class="cb-timer" data-end-ts="1784087999999"></span>
    </div>
    <div class="campaign-banner">
      <span class="cb-name">Campaign Two</span>
      <span class="cb-timer" data-end-ts="1784087999999"></span>
    </div>
    <h2>Past Drops</h2>
    """

    report = parse_game_page(source, "https://twitchdrops.app/game/example")

    assert report["campaign_count"] == 0
    assert report["non_watch_campaign_count"] == 2


def test_subscriber_only_campaign_is_not_treated_as_watch_drop():
    source = """
    <main><h1>Example Game</h1></main>
    <div class="drop-card">
      <div class="drop-name">Subscriber Reward</div>
      <div class="drop-time">Subscribe to a participating channel</div>
      <div class="drop-campaign">Subscriber Campaign</div>
    </div>
    <h2>Active Campaigns</h2>
    <div class="campaign-banner">
      <span class="cb-name">Subscriber Campaign</span>
      <span class="cb-timer" data-end-ts="1784087999999"></span>
    </div>
    <h2>Past Drops</h2>
    """

    report = parse_game_page(source, "https://twitchdrops.app/game/example")

    assert report["campaign_count"] == 0
    assert report["non_watch_campaign_count"] == 1
    assert report["non_watch_campaigns"][0]["drops"][0]["name"] == (
        "Subscriber Reward"
    )


def test_upcoming_campaign_includes_page_start_and_watch_drop():
    source = """
    <aside>
      <a href="/game/example-game" data-end="2026-07-17T20:00:00Z"
         data-start="2026-07-16T20:00:00Z">Example Game</a>
    </aside>
    <main><h1>Example Game</h1></main>
    <div class="drop-card">
      <div class="drop-name">Upcoming Reward</div>
      <div class="drop-time">Watch 45m</div>
    </div>
    <h2>How to get these drops</h2>
    <h2>Upcoming Campaigns</h2>
    <div class="campaign-banner upcoming">
      <span class="cb-name">Reveal Stream</span>
      <span class="cb-timer" data-end-ts="1784318400000"></span>
      <div class="cb-channels">All Channels</div>
    </div>
    <h2>Past Drops</h2>
    """

    report = parse_game_page(
        source, "https://twitchdrops.app/game/example-game"
    )

    assert report["campaign_count"] == 0
    assert report["upcoming_campaigns"][0]["starts_at"] == "2026-07-16T20:00:00Z"
    assert [drop["name"] for drop in report["upcoming_campaigns"][0]["drops"]] == [
        "Upcoming Reward"
    ]


def test_front_page_lists_only_active_and_upcoming_games():
    source = """
    <a href="/game/active" class="game-card" data-game="active game"
       data-slug="active" data-start="2026-07-01T00:00:00Z"
       data-end="2026-07-20T00:00:00Z"></a>
    <a href="/game/upcoming" class="game-card upcoming" data-game="upcoming game"
       data-slug="upcoming" data-start="2026-07-18T00:00:00Z"
       data-end="2026-07-20T00:00:00Z"></a>
    <a href="/game/expired" class="game-card game-card--expired"></a>
    """

    games = parse_front_page(source)

    assert [game["slug"] for game in games] == ["active", "upcoming"]
    assert games[1]["upcoming"] is True
