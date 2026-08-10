class PubsubTopic(object):
    __slots__ = ["topic", "user_id", "streamer"]

    def __init__(self, topic, user_id=None, streamer=None):
        self.topic = topic
        self.user_id = user_id
        self.streamer = streamer

    def is_user_topic(self):
        return self.streamer is None

    def __str__(self):
        if self.is_user_topic():
            return f"{self.topic}.{self.user_id}"
        else:
            return f"{self.topic}.{self.streamer.channel_id}"

    def __eq__(self, other):
        if not isinstance(other, PubsubTopic):
            return NotImplemented
        return (
            self.topic == other.topic
            and self.user_id == other.user_id
            and self.__streamer_channel_id() == other.__streamer_channel_id()
        )

    def __streamer_channel_id(self):
        return None if self.streamer is None else self.streamer.channel_id
