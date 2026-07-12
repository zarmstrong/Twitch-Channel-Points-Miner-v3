# -*- coding: utf-8 -*-
__version__ = "2.0.5"

from .runner_migration import migrate_runner

migrate_runner()

from .TwitchChannelPointsMiner import TwitchChannelPointsMiner  # noqa: E402

__all__ = [
    "TwitchChannelPointsMiner",
]
