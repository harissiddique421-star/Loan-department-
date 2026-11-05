"""Small server-side read cache for expensive, read-heavy results (dashboard counts,
monthly report totals, the user-name list, disbursement sheet totals).

Correctness first:
  * Every entry is tied to a "data version" read from the database once per request
    (public.data_version(): newest audit id, newest application change, newest sheet /
    cheque change). Almost every change in the app writes an audit row, so any create,
    edit, approval, rejection, deletion or sheet change produces a new version and the
    old entries are simply never used again — on every server instance, not only the
    one that made the change. A short TTL is a second safety net.
  * Keys always include the user's role and id where the result depends on who is
    looking (Row Level Security decides what each user can see), so one user's numbers
    are never shown to another user.
  * Only plain values (numbers, strings, dicts, lists) are stored — never database
    objects, sessions, passwords or permission decisions.
"""
import threading
import time
from collections import OrderedDict


class VersionedCache:
    def __init__(self, ttl=300, maxsize=512):
        self.ttl, self.maxsize = ttl, maxsize
        self._data = OrderedDict()
        self._lock = threading.Lock()
        self.hits = self.misses = 0

    def get(self, key, version, compute):
        """Return compute() for (key, version), reusing a fresh earlier result."""
        if version is None:                       # no version (e.g. SQLite / tests): no caching
            return compute()
        now = time.monotonic()
        with self._lock:
            hit = self._data.get(key)
            if hit and hit[0] == version and now - hit[1] < self.ttl:
                self._data.move_to_end(key)
                self.hits += 1
                return hit[2]
        value = compute()
        with self._lock:
            self.misses += 1
            self._data[key] = (version, now, value)
            self._data.move_to_end(key)
            while len(self._data) > self.maxsize:
                self._data.popitem(last=False)
        return value

    def clear(self):
        with self._lock:
            self._data.clear()
