class ModViewChannelResponse:
    def __init__(self, is_moderator: bool | None):
        self.is_moderator = is_moderator

    def __repr__(self):
        return f"ModViewChannelResponse({self.__dict__})"
