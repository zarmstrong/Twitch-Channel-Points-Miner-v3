from twitchdrops_app_scraper import parse_game_page


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
