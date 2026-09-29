"""Fixed-size ring buffer for telemetry history.

Telemetry lives in RAM and nowhere else. Flash endures about 10^5 write cycles,
so a 1 Hz log would exhaust it inside a day and the failure is unrecoverable.
The phone persists anything that needs to outlive a reboot.

Pre-allocating the whole buffer also keeps the heap from fragmenting, which
matters on a board with 165 KB free and an HTTP server that allocates in bursts.
"""


class RingBuffer:
    """Keeps the most recent `capacity` items, discarding the oldest."""

    def __init__(self, capacity):
        if capacity < 1:
            raise ValueError("capacity must be at least 1")
        self._items = [None] * capacity
        self._capacity = capacity
        self._next = 0
        self._count = 0

    def append(self, item):
        self._items[self._next] = item
        self._next = (self._next + 1) % self._capacity
        if self._count < self._capacity:
            self._count += 1

    def __len__(self):
        return self._count

    def __iter__(self):
        """Oldest first."""
        start = (self._next - self._count) % self._capacity
        for offset in range(self._count):
            yield self._items[(start + offset) % self._capacity]

    def latest(self):
        if self._count == 0:
            return None
        return self._items[(self._next - 1) % self._capacity]

    def to_list(self):
        return list(self)

    def clear(self):
        self._next = 0
        self._count = 0

    @property
    def capacity(self):
        return self._capacity

    @property
    def full(self):
        return self._count == self._capacity
