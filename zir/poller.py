"""One polling loop per host, so a slow or dead host never delays the others."""

from __future__ import annotations

import asyncio
import random
from typing import Callable

from .collector import HostReading
from .config import Settings
from .store import HostStore, backoff_delay


class HostWorker:
    def __init__(
        self,
        name: str,
        collector,
        store: HostStore,
        semaphore: asyncio.Semaphore,
        settings: Settings,
        on_bad: Callable[[str], None] | None = None,
    ) -> None:
        self.name = name
        self.collector = collector
        self.store = store
        self.semaphore = semaphore
        self.settings = settings
        self.on_bad = on_bad
        self.visible = False
        self.wake = asyncio.Event()

    @property
    def interval(self) -> float:
        return self.store[self.name].spec.interval or self.settings.interval

    def next_delay(self) -> float:
        state = self.store[self.name]
        if state.reading is not None and not state.reading.ok:
            # ±10%: hosts that went down together do not all retry at the same moment.
            return max(backoff_delay(state.failures), self.interval) * random.uniform(0.9, 1.1)
        delay = self.interval
        if not self.visible:
            delay *= self.settings.hidden_interval_factor
        return delay

    def poke(self) -> None:
        """Poll right now (Ctrl+R, tab became visible, resumed)."""
        self.wake.set()

    async def _sleep(self, delay: float) -> None:
        try:
            await asyncio.wait_for(self.wake.wait(), timeout=delay)
        except asyncio.TimeoutError:
            pass
        self.wake.clear()

    async def poll_once(self) -> None:
        async with self.semaphore:
            try:
                reading = await self.collector.collect()
            except Exception as exc:  # noqa: BLE001 - keep the loop alive
                reading = HostReading(
                    spec=self.store[self.name].spec,
                    error=f"{type(exc).__name__}: {exc}",
                )
        if self.store.update(self.name, reading) and self.on_bad:
            self.on_bad(self.name)

    async def run(self) -> None:
        # Spread the first polls so a big fleet does not hit SSH all at once.
        await self._sleep(random.uniform(0, min(self.interval, 1.0)))
        while True:
            state = self.store[self.name]
            if state.paused or state.disabled:
                await self._sleep(3600)
                continue
            await self.poll_once()
            await self._sleep(self.next_delay())
