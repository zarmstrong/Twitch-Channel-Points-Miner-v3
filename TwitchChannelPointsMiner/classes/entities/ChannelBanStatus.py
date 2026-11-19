class ChannelBanStatus:
    """Whether the account is banned or timed out in one channel.

    Built from the `user.self.banStatus` field of the ChannelBanStatus query:
    `null` means not banned, an object means a permanent ban or a timeout.
    """

    __slots__ = ["banned", "permanent", "expires_at"]

    def __init__(self, banned, permanent=False, expires_at=None):
        self.banned: bool = banned
        self.permanent: bool = permanent
        # Epoch seconds when a timeout ends, or None when Twitch gave no end.
        self.expires_at: float | None = expires_at

    def active(self, now):
        """Return True while the ban or timeout still applies at `now`."""
        if not self.banned:
            return False
        if self.permanent or self.expires_at is None:
            return True
        return self.expires_at > now

    def same_ban(self, other):
        return (
            isinstance(other, ChannelBanStatus)
            and self.banned == other.banned
            and self.permanent == other.permanent
            and self.expires_at == other.expires_at
        )

    def __repr__(self):
        return (
            f"ChannelBanStatus(banned={self.banned}, permanent={self.permanent}, "
            f"expires_at={self.expires_at})"
        )
