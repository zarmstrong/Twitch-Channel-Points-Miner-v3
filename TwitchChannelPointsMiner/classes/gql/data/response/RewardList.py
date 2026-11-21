from datetime import datetime


class WatchStreakMilestone:
    def __init__(
        self,
        value: str | None,
        achievement_timestamp: datetime,
        expires_at: datetime | None,
    ):
        self.value = value
        self.achievement_timestamp = achievement_timestamp
        self.expires_at = expires_at

    def __repr__(self):
        return f"WatchStreakMilestone({self.__dict__})"


class RewardListResponse:
    def __init__(self, watch_streak_milestone: WatchStreakMilestone | None):
        self.watch_streak_milestone = watch_streak_milestone

    def __repr__(self):
        return f"RewardListResponse({self.__dict__})"
