from enum import Enum, auto
from threading import Lock

ANALYTICS_FILE_MUTEX = Lock()


class Priority(Enum):
    ORDER = auto()
    STREAK = auto()
    FAVORITE = auto()
    DROPS = auto()
    SUBSCRIBED = auto()
    POINTS_ASCENDING = auto()
    POINTS_DESCENDING = auto()


class StreamerSource(Enum):
    STREAMERS = auto()
    FOLLOWERS = auto()
    CATEGORIES = auto()
    BADGES = auto()


class FollowersOrder(Enum):
    ASC = auto()
    DESC = auto()

    def __str__(self):
        return self.name


class CategorySort(str, Enum):
    ORDER = "ORDER"
    VIEWERS_DESC = "VIEWERS_DESC"
    VIEWERS_ASC = "VIEWERS_ASC"
    STARTED_AT_DESC = "STARTED_AT_DESC"
    STARTED_AT_ASC = "STARTED_AT_ASC"
    RANDOM = "RANDOM"

    def __str__(self):
        return self.value


class CategoryCampaignOrder(str, Enum):
    ORDER = "ORDER"
    EXPIRATION = "EXPIRATION"

    def __str__(self):
        return self.value


# Empty object shared between class
class Settings(object):
    __slots__ = [
        "logger",
        "streamer_settings",
        "enable_analytics",
        "disable_ssl_cert_verification",
        "disable_at_in_nickname",
        "track_category_streamer_points",
    ]


class Events(Enum):
    UPDATE_AVAILABLE = auto()
    DAILY_REPORT = auto()
    STREAMER_ONLINE = auto()
    STREAMER_OFFLINE = auto()
    GAIN_FOR_RAID = auto()
    GAIN_FOR_CLAIM = auto()
    GAIN_FOR_WATCH = auto()
    GAIN_FOR_WATCH_STREAK = auto()
    BET_WIN = auto()
    BET_LOSE = auto()
    BET_REFUND = auto()
    BET_FILTERS = auto()
    BET_GENERAL = auto()
    BET_FAILED = auto()
    BET_START = auto()
    BONUS_CLAIM = auto()
    MOMENT_CLAIM = auto()
    JOIN_RAID = auto()
    DROP_CLAIM = auto()
    DROP_STATUS = auto()
    CHAT_MENTION = auto()
    CONFIGURATION = auto()

    def __str__(self):
        return self.name

    @classmethod
    def get(cls, key):
        return getattr(cls, str(key)) if str(key) in dir(cls) else None
