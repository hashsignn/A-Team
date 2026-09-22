"""The event spine: publish on arrival, never on a schedule.

The previous pipeline was a batch. Sources were polled, everything that came
back went through the gate together, and a board was built at the end. That is
fine for a report and wrong for a disruption: an item that arrives at 07:41 sits
until the batch runs, and the whole argument of this product is that the minutes
between the event and the decision are the expensive part.

So arrival is the trigger. ``publish`` is synchronous and returns once every
subscriber has been handed the message; a subscriber that wants to do slow work
queues it rather than blocking the publisher. Nothing here sleeps, polls or
waits for a window.

WHY IN-PROCESS AND NOT A BROKER
-------------------------------
A broker would be the right answer for a fleet of services and the wrong one
here: it adds an operational dependency, a second place for messages to be
lost, and a reason the tool cannot be run offline on a laptop. The interface
below is deliberately the subset a broker also offers — topic, publish,
subscribe — so moving to one later is a change of implementation rather than
a change of call sites.

ORDERING AND BACKPRESSURE
-------------------------
Messages are delivered in publication order to each subscriber. Async
subscribers get a bounded queue: when it fills, the OLDEST message is dropped
and the drop is counted, because a live dashboard that is behind wants the
newest state, not a faithful replay of a backlog. Synchronous subscribers are
called inline and cannot fall behind by construction.
"""

from __future__ import annotations

import asyncio
import threading
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

# Topics. Strings would work; an explicit list means a typo is a failed import
# rather than a subscriber that silently never fires.
TOPIC_SIGNAL = "signal"        # something arrived from a source
TOPIC_DISRUPTION = "disruption"  # it survived the gate and touches freight
TOPIC_ACTION = "action"        # a planner executed something
TOPIC_FIELD = "field"          # a report came in from the road
TOPIC_UNDO = "undo"            # an executed action was pulled back

TOPICS = (TOPIC_SIGNAL, TOPIC_DISRUPTION, TOPIC_ACTION, TOPIC_FIELD, TOPIC_UNDO)

# How many messages an async subscriber may fall behind before the oldest are
# dropped. One screenful of updates; past that the client is not reading.
QUEUE_LIMIT = 256

# How much history a late-joining subscriber can replay. Small on purpose: this
# is a live spine, not a log. The durable record is the report store.
REPLAY_LIMIT = 100


@dataclass(frozen=True)
class Message:
    """One thing that happened, with the instant it happened at.

    ``at`` is passed in rather than read from the clock here. Everything under
    ``engine/`` is reproducible from a pinned as-of date, and a module that
    stamped its own wall-clock time would be the one place a replay could not
    reproduce.
    """

    topic: str
    payload: dict[str, Any]
    at: str
    seq: int = 0

    def as_dict(self) -> dict:
        return {"topic": self.topic, "seq": self.seq, "at": self.at,
                **self.payload}


@dataclass
class _AsyncSubscriber:
    topics: frozenset[str]
    queue: asyncio.Queue
    loop: asyncio.AbstractEventLoop
    dropped: int = 0


class Bus:
    """A topic bus with synchronous and async subscribers."""

    def __init__(self, replay_limit: int = REPLAY_LIMIT) -> None:
        self._lock = threading.Lock()
        self._sync: list[tuple[frozenset[str], Callable[[Message], None]]] = []
        self._async: list[_AsyncSubscriber] = []
        self._history: deque[Message] = deque(maxlen=replay_limit)
        self._seq = 0

    # -------------------------------------------------------------- publish
    def publish(self, topic: str, payload: dict, at: str) -> Message:
        """Hand a message to everyone listening, now.

        Returns once delivery has been attempted for every subscriber. A
        subscriber that raises does not stop the others: one broken listener
        must not be able to silence the spine.
        """
        if topic not in TOPICS:
            raise ValueError(f"unknown topic {topic!r}; expected one of {TOPICS}")

        with self._lock:
            self._seq += 1
            message = Message(topic=topic, payload=dict(payload), at=at,
                              seq=self._seq)
            self._history.append(message)
            sync = [fn for topics, fn in self._sync if topic in topics]
            targets = [s for s in self._async if topic in s.topics]

        for fn in sync:
            try:
                fn(message)
            except Exception:  # noqa: BLE001 - a listener must not break the bus
                continue

        for sub in targets:
            self._offer(sub, message)

        return message

    def _offer(self, sub: _AsyncSubscriber, message: Message) -> None:
        """Put a message on a subscriber's queue, dropping the oldest if full.

        ``call_soon_threadsafe`` because publish is synchronous and may be
        called from a worker thread, while the queue belongs to an event loop.
        """
        def deliver() -> None:
            if sub.queue.full():
                try:
                    sub.queue.get_nowait()
                    sub.dropped += 1
                except asyncio.QueueEmpty:
                    pass
            try:
                sub.queue.put_nowait(message)
            except asyncio.QueueFull:
                sub.dropped += 1

        try:
            sub.loop.call_soon_threadsafe(deliver)
        except RuntimeError:
            # The loop is closed; the subscriber is gone. Reap it.
            with self._lock:
                if sub in self._async:
                    self._async.remove(sub)

    # ------------------------------------------------------------ subscribe
    def on(self, topics: str | list[str], fn: Callable[[Message], None]) -> Callable:
        """Subscribe synchronously. Returns the unsubscribe callable."""
        wanted = frozenset([topics] if isinstance(topics, str) else topics)
        entry = (wanted, fn)
        with self._lock:
            self._sync.append(entry)

        def off() -> None:
            with self._lock:
                if entry in self._sync:
                    self._sync.remove(entry)

        return off

    def stream(
        self,
        topics: str | list[str],
        loop: asyncio.AbstractEventLoop | None = None,
        limit: int = QUEUE_LIMIT,
    ) -> _AsyncSubscriber:
        """Subscribe asynchronously; read with ``drain``."""
        wanted = frozenset([topics] if isinstance(topics, str) else topics)
        sub = _AsyncSubscriber(
            topics=wanted,
            queue=asyncio.Queue(maxsize=limit),
            loop=loop or asyncio.get_event_loop(),
        )
        with self._lock:
            self._async.append(sub)
        return sub

    def release(self, sub: _AsyncSubscriber) -> None:
        with self._lock:
            if sub in self._async:
                self._async.remove(sub)

    async def drain(self, sub: _AsyncSubscriber, timeout: float = 1.0):
        """Next message, or None if nothing arrived inside the timeout.

        None is a normal outcome, not an error: it is what lets an SSE handler
        send a keep-alive and go round again.
        """
        try:
            return await asyncio.wait_for(sub.queue.get(), timeout=timeout)
        except TimeoutError:
            return None

    # -------------------------------------------------------------- history
    def replay(self, topics: str | list[str] | None = None,
               since_seq: int = 0) -> list[Message]:
        wanted = (
            None if topics is None
            else frozenset([topics] if isinstance(topics, str) else topics)
        )
        with self._lock:
            return [
                m for m in self._history
                if m.seq > since_seq and (wanted is None or m.topic in wanted)
            ]

    @property
    def sequence(self) -> int:
        with self._lock:
            return self._seq

    def subscriber_count(self) -> dict[str, int]:
        with self._lock:
            return {"sync": len(self._sync), "async": len(self._async)}


# The process-wide spine. A single instance because the point of the bus is
# that every part of the app is looking at the same stream of events.
BUS = Bus()
