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
        return str(self) == str(other)

    def __hash__(self):
        return hash(str(self))
