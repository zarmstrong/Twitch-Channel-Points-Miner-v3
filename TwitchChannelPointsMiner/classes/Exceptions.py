class StreamerDoesNotExistException(Exception):
    pass


class StreamerIsOfflineException(Exception):
    pass


class WrongCookiesException(Exception):
    pass


class BadCredentialsException(Exception):
    pass


class CategoryLookupFailedException(Exception):
    """A Twitch request needed to discover a category's drop channels failed.

    Distinct from an empty result: an empty discovery means the category has
    no live drop channel (or no open campaign) right now and its tracked
    streamers are stale, while this exception means the category was not
    evaluated at all this cycle and nothing should be retired for it.

    `game_slug` carries the Twitch game slug the category resolves to (or
    the catalog name it was about to be searched by); it can differ from the
    configured category slug for aliased categories. `usernames` are the live
    channels found before the failure, when it struck mid-way through a
    paginated lookup: still worth adding, although the category as a whole
    stays unevaluated.
    """

    def __init__(self, message, game_slug=None, usernames=()):
        super().__init__(message)
        self.game_slug = game_slug
        self.usernames = list(usernames)
