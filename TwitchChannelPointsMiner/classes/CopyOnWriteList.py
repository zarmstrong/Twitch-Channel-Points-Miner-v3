from threading import Lock


class CopyOnWriteList:
    """A list that other threads can read while it changes (#170).

    The miner shares its streamer and websocket lists by reference with
    worker threads that read them without a lock, while retirement and
    config reloads shrink or reorder them. Each change here builds a new
    tuple under a lock and swaps it in, so a loop over the list keeps
    working on the version it started with instead of skipping items or
    failing with "changed size during iteration". A reader that takes len()
    and then indexes reads the live list each time; take snapshot() first.

    It supports the list operations this project uses. Sorting returns
    nothing and sorts in place, like list.sort().
    """

    __slots__ = ("_items", "_lock")

    def __init__(self, items=()):
        self._items = tuple(items)
        self._lock = Lock()

    # Reads use one snapshot of the current tuple.

    def snapshot(self):
        return self._items

    def __iter__(self):
        return iter(self._items)

    def __reversed__(self):
        return reversed(self._items)

    def __len__(self):
        return len(self._items)

    def __bool__(self):
        return bool(self._items)

    def __contains__(self, item):
        return item in self._items

    def __getitem__(self, index):
        if isinstance(index, slice):
            return list(self._items[index])
        return self._items[index]

    def index(self, *args):
        return self._items.index(*args)

    def count(self, item):
        return self._items.count(item)

    def copy(self):
        return list(self._items)

    def __eq__(self, other):
        if isinstance(other, CopyOnWriteList):
            other = other._items
        if isinstance(other, (list, tuple)):
            return list(self._items) == list(other)
        return NotImplemented

    __hash__ = None

    def __repr__(self):
        return f"CopyOnWriteList({list(self._items)!r})"

    # Writes replace the tuple under the lock.

    def _update(self, change):
        with self._lock:
            items = list(self._items)
            result = change(items)
            self._items = tuple(items)
        return result

    def __setitem__(self, index, value):
        if isinstance(index, slice):
            value = list(value)

        def change(items):
            items[index] = value

        self._update(change)

    def __delitem__(self, index):
        def change(items):
            del items[index]

        self._update(change)

    def __iadd__(self, other):
        self.extend(other)
        return self

    def append(self, item):
        self._update(lambda items: items.append(item))

    def extend(self, other):
        other = list(other)
        self._update(lambda items: items.extend(other))

    def insert(self, index, item):
        self._update(lambda items: items.insert(index, item))

    def remove(self, item):
        self._update(lambda items: items.remove(item))

    def pop(self, index=-1):
        return self._update(lambda items: items.pop(index))

    def clear(self):
        self._update(lambda items: items.clear())

    def sort(self, *, key=None, reverse=False):
        self._update(lambda items: items.sort(key=key, reverse=reverse))

    def reverse(self):
        self._update(lambda items: items.reverse())
