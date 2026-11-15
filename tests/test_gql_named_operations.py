import re

import pytest

from TwitchChannelPointsMiner.constants import GQLOperations

# Twitch answers a request whose operationName names no operation in its query
# text with "operation with name '...' not found" and never runs the query
# (zarmstrong/Twitch-Channel-Points-Miner-v3#296).
_NAMED_OPERATION = r"^\s*(?:query|mutation)\s+{name}\b"


def _hand_written_operations():
    for attribute, value in vars(GQLOperations).items():
        if isinstance(value, dict) and "query" in value:
            yield attribute, value["operationName"], value["query"]
    for name, query in GQLOperations.raw_queries.items():
        yield f"raw_queries[{name!r}]", name, query


@pytest.mark.parametrize(
    "source, operation_name, query",
    list(_hand_written_operations()),
    ids=lambda value: value if isinstance(value, str) and " " not in value else "",
)
def test_hand_written_query_names_its_operation(source, operation_name, query):
    assert re.match(
        _NAMED_OPERATION.format(name=re.escape(operation_name)), query
    ), f"{source}: query does not declare operation {operation_name!r}"


def test_hand_written_operations_are_found():
    sources = {source for source, _, _ in _hand_written_operations()}
    assert {"ChannelPointsContext", "GameSlug", "raw_queries['Inventory']"} <= sources
