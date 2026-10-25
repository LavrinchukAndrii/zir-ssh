import asyncio
import json

from zir.collector import DemoCollector, HostReading, demo_specs
from zir.config import HostSpec, Settings
from zir.poller import HostWorker
from zir.store import HostStore, Session, backoff_delay


def test_backoff_steps():
    assert [backoff_delay(n) for n in range(1, 7)] == [5, 10, 30, 60, 60, 60]


def _reading(spec, error=None):
    collector = DemoCollector(spec)
    reading = asyncio.run(collector.collect())
    if error:
        reading.error = error
    return reading


def test_became_crit_only_on_transition():
    spec = HostSpec(name="web-1", target="web-1")
    store = HostStore([spec])
    assert store.update("web-1", _reading(spec)) is False
    assert store["web-1"].level in ("ok", "warn")
    assert store.update("web-1", HostReading(spec=spec, error="timeout")) is True
    assert store.update("web-1", HostReading(spec=spec, error="timeout")) is False
    assert store["web-1"].failures == 2
    store.update("web-1", _reading(spec))
    assert store["web-1"].failures == 0


def test_stale_after_three_intervals():
    spec = HostSpec(name="web-1", target="web-1")
    store = HostStore([spec])
    store.update("web-1", _reading(spec))
    state = store["web-1"]
    at = state.last_ok_at
    assert not state.is_stale(at + 5, interval=2)
    assert state.is_stale(at + 7, interval=2)


def test_session_round_trip_and_garbage(tmp_path):
    path = tmp_path / "session.json"
    Session(open=["a", "b"], active="b", sections={"b": "disks"}).save(path)
    loaded = Session.load(path)
    assert (loaded.open, loaded.active, loaded.sections) == (["a", "b"], "b", {"b": "disks"})

    path.write_text("{not json")
    assert Session.load(path) == Session()
    path.write_text(json.dumps({"open": "x", "active": 3, "sections": []}))
    assert Session.load(path) == Session()


def test_slow_host_does_not_block_others():
    specs = demo_specs(3)  # web-1, web-2, db-1 + edge-slow
    store = HostStore(specs)
    settings = Settings(interval=0.5)
    semaphore = asyncio.Semaphore(8)

    async def scenario():
        workers = [
            HostWorker(s.name, DemoCollector(s), store, semaphore, settings) for s in specs
        ]
        for worker in workers:
            worker.visible = True
        tasks = [asyncio.create_task(w.run()) for w in workers]
        await asyncio.sleep(2.5)
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)

    asyncio.run(scenario())
    assert store["edge-slow"].version == 0  # still waiting for its first answer
    for name in ("web-1", "web-2", "db-1"):
        assert store[name].version >= 3


def test_findings_are_kept_with_the_reading():
    spec = HostSpec(name="worker-1", target="worker-1")  # the demo profile with a failed unit
    store = HostStore([spec])
    store.update("worker-1", _reading(spec))
    state = store["worker-1"]
    assert any("laravel-worker" in f.message for f in state.findings)
    assert state.level == "crit"
    store.update("worker-1", HostReading(spec=spec, error="timeout"))
    assert state.findings == [] and state.level == "error"
