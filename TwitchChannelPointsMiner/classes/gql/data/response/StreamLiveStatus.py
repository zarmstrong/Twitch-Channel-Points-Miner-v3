from datetime import datetime


class LiveStream:
    def __init__(self, _id: str, created_at: datetime):
        self.id = _id
        self.created_at = created_at

    def __repr__(self):
        return f"LiveStream({self.__dict__})"


class StreamLiveStatusResponse:
    def __init__(self, channel_id: str | None, stream: LiveStream | None):
        self.channel_id = channel_id
        self.stream = stream

    def __repr__(self):
        return f"StreamLiveStatusResponse({self.__dict__})"
