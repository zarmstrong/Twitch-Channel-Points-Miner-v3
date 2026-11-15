import ast
import re
from pathlib import Path

import pytest

import TwitchChannelPointsMiner
from TwitchChannelPointsMiner.constants import GQLOperations

# Twitch answers a request whose operationName names no operation in its query
# text with "operation with name '...' not found" and never runs the query
# (zarmstrong/Twitch-Channel-Points-Miner-v3#296).
_NAMED_OPERATION = r"^\s*(?:query|mutation)\s+{name}\b"
_PACKAGE = Path(TwitchChannelPointsMiner.__file__).parent


def _dict_entries(node):
    return {
        key.value: value
        for key, value in zip(node.keys, node.values)
        if isinstance(key, ast.Constant) and isinstance(key.value, str)
    }


def _string(node):
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    return None


def _inline_operations():
    # Payloads built in the code rather than in GQLOperations: a dict with a
    # literal operationName and a "query". When the query isn't a literal
    # (it comes from a list of variants), every literal "query" in the same
    # function is checked against that operation name.
    for path in sorted(_PACKAGE.rglob("*.py")):
        if path.name == "constants.py":
            continue  # GQLOperations is read directly below.
        module = path.relative_to(_PACKAGE).as_posix()
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for function in ast.walk(tree):
            if not isinstance(function, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            dicts = [node for node in ast.walk(function) if isinstance(node, ast.Dict)]
            for node in dicts:
                entries = _dict_entries(node)
                operation_name = _string(entries.get("operationName"))
                if operation_name is None or "query" not in entries:
                    continue
                source = f"{module}:{operation_name}"
                query = _string(entries["query"])
                if query is not None:
                    yield source, operation_name, query
                    continue
                queries = [
                    _string(_dict_entries(other).get("query"))
                    for other in dicts
                    if other is not node
                ]
                queries = [query for query in queries if query is not None]
                if not queries:
                    # Fails below: a query this test can't see isn't checked.
                    yield source, operation_name, ""
                for query in queries:
                    yield source, operation_name, query


def _hand_written_operations():
    for attribute, value in vars(GQLOperations).items():
        if isinstance(value, dict) and "query" in value:
            yield attribute, value["operationName"], value["query"]
    for name, query in GQLOperations.raw_queries.items():
        yield f"raw_queries[{name!r}]", name, query
    yield from _inline_operations()


@pytest.mark.parametrize(
    "source, operation_name, query",
    [
        pytest.param(source, operation_name, query, id=source)
        for source, operation_name, query in _hand_written_operations()
    ],
)
def test_hand_written_query_names_its_operation(source, operation_name, query):
    assert re.match(
        _NAMED_OPERATION.format(name=re.escape(operation_name)), query
    ), f"{source}: query does not declare operation {operation_name!r}"


def test_hand_written_operations_are_found():
    sources = {source for source, _, _ in _hand_written_operations()}
    assert {
        "ChannelPointsContext",
        "GameSlug",
        "raw_queries['Inventory']",
        "classes/Twitch.py:AvailableBadges",
        "classes/Twitch.py:MinerRewardCampaignsRaw",
    } <= sources
