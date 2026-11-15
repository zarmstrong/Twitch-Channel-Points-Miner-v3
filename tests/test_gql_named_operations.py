import ast
import re
import textwrap
from pathlib import Path

import pytest

import TwitchChannelPointsMiner
from TwitchChannelPointsMiner.constants import GQLOperations

# Twitch answers a request whose operationName names no operation in its query
# text with "operation with name '...' not found" and never runs the query
# (zarmstrong/Twitch-Channel-Points-Miner-v3#296).
_NAMED_OPERATION = r"^\s*(?:query|mutation)\s+{name}\b"
_PACKAGE = Path(TwitchChannelPointsMiner.__file__).parent
# Stands in for query text the collector can't read, so that case fails.
UNRESOLVED = ""


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


def _enclosing_scope(node, parents):
    while node in parents:
        node = parents[node]
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            return node
    return node


def _variant_queries(query, scope):
    # Resolves `for variant in variants: {..., "query": variant["query"]}`
    # where `variants = [{"query": "..."}, ...]` in the same scope. Returns
    # one entry per variant (None when its query isn't a literal), or None
    # when the payload's query can't be traced to such a list.
    if not (
        isinstance(query, ast.Subscript)
        and isinstance(query.value, ast.Name)
        and _string(query.slice) == "query"
    ):
        return None
    loops = [
        node
        for node in ast.walk(scope)
        if isinstance(node, (ast.For, ast.AsyncFor))
        and isinstance(node.target, ast.Name)
        and node.target.id == query.value.id
        and isinstance(node.iter, ast.Name)
    ]
    if len(loops) != 1:
        return None
    lists = [
        node.value
        for node in ast.walk(scope)
        if isinstance(node, ast.Assign)
        and len(node.targets) == 1
        and isinstance(node.targets[0], ast.Name)
        and node.targets[0].id == loops[0].iter.id
    ]
    if len(lists) != 1 or not isinstance(lists[0], (ast.List, ast.Tuple)):
        return None
    return [
        (
            _string(_dict_entries(variant).get("query"))
            if isinstance(variant, ast.Dict)
            else None
        )
        for variant in lists[0].elts
    ]


def _operations_in_source(module, text):
    # Payloads built in the code rather than in GQLOperations: a dict with a
    # literal operationName and a "query", either a literal or a variant's
    # query from a list of variants. Anything else yields UNRESOLVED.
    tree = ast.parse(text)
    parents = {
        child: parent
        for parent in ast.walk(tree)
        for child in ast.iter_child_nodes(parent)
    }
    for node in ast.walk(tree):
        if not isinstance(node, ast.Dict):
            continue
        entries = _dict_entries(node)
        operation_name = _string(entries.get("operationName"))
        if operation_name is None or "query" not in entries:
            continue
        source = f"{module}:{operation_name}"
        query = _string(entries["query"])
        if query is not None:
            yield source, operation_name, query
            continue
        variants = _variant_queries(entries["query"], _enclosing_scope(node, parents))
        if not variants:
            yield source, operation_name, UNRESOLVED
            continue
        for variant in variants:
            yield source, operation_name, UNRESOLVED if variant is None else variant


def _inline_operations():
    for path in sorted(_PACKAGE.rglob("*.py")):
        if path.name == "constants.py":
            continue  # GQLOperations is read directly below.
        module = path.relative_to(_PACKAGE).as_posix()
        yield from _operations_in_source(module, path.read_text(encoding="utf-8"))


def _hand_written_operations():
    for attribute, value in vars(GQLOperations).items():
        if isinstance(value, dict) and "query" in value:
            yield attribute, value["operationName"], value["query"]
    for name, query in GQLOperations.raw_queries.items():
        yield f"raw_queries[{name!r}]", name, query
    yield from _inline_operations()


def _declares(operation_name, query):
    return bool(
        re.match(_NAMED_OPERATION.format(name=re.escape(operation_name)), query)
    )


@pytest.mark.parametrize(
    "source, operation_name, query",
    [
        pytest.param(source, operation_name, query, id=source)
        for source, operation_name, query in _hand_written_operations()
    ],
)
def test_hand_written_query_names_its_operation(source, operation_name, query):
    assert _declares(
        operation_name, query
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


def test_collector_checks_each_variant_and_fails_unreadable_ones():
    text = textwrap.dedent("""
        RENAMED = "query Other { a }"

        def fetch(gql):
            variants = [
                {"query": "query Op { a }"},
                {"query": "query { b }"},
                {"query": RENAMED},
            ]
            for variant in variants:
                gql.post({"operationName": "Op", "query": variant["query"]})
        """)

    found = list(_operations_in_source("sample.py", text))

    assert found == [
        ("sample.py:Op", "Op", "query Op { a }"),
        ("sample.py:Op", "Op", "query { b }"),
        ("sample.py:Op", "Op", UNRESOLVED),
    ]
    assert [_declares("Op", query) for _, _, query in found] == [True, False, False]


@pytest.mark.parametrize(
    "text",
    [
        # The query comes from somewhere the collector doesn't trace.
        'def f(q):\n    return {"operationName": "Op", "query": q}\n',
        # Payloads outside a function are found too.
        'PAYLOAD = {"operationName": "Op", "query": QUERY}\n',
        'class C:\n    PAYLOAD = {"operationName": "Op", "query": "{ a }"}\n',
    ],
    ids=["untraced", "module-level", "class-level"],
)
def test_collector_fails_queries_it_cannot_check(text):
    found = list(_operations_in_source("sample.py", text))

    assert len(found) == 1
    assert not _declares("Op", found[0][2])
