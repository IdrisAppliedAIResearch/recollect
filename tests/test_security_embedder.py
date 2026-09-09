"""A shared native embedding context is entered only once at a time."""

from concurrent.futures import ThreadPoolExecutor
from threading import Event, Lock

import numpy as np

from recollect.engine.embedder import EMBEDDING_DIMENSION, HarnessEmbedder


def test_native_calls_are_serialized_and_identical_misses_are_coalesced(tmp_path):
    path = tmp_path / "fake.gguf"
    path.touch()
    embedder = HarnessEmbedder(path)
    entered, release, second_requested = Event(), Event(), Event()
    guard = Lock()
    active = peak = calls = 0

    class Probe:
        def embed(self, text):
            nonlocal active, peak, calls
            with guard:
                active += 1
                calls += 1
                peak = max(peak, active)
            entered.set()
            assert release.wait(3)
            with guard:
                active -= 1
            return np.arange(EMBEDDING_DIMENSION, dtype=np.float32)

    embedder._model = Probe()

    def second():
        second_requested.set()
        return embedder("same text"), embedder.last_cache_hit

    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(embedder, "same text")
        assert entered.wait(3)
        following = pool.submit(second)
        try:
            assert second_requested.wait(3)
            assert not following.done()
        finally:
            release.set()
        vector = first.result()
        cached, hit = following.result()
    assert peak == 1 and calls == 1 and hit
    assert np.array_equal(vector, cached)
    vector[0] = -123
    assert embedder("same text")[0] == 0


def test_last_call_metrics_belong_to_the_calling_worker(tmp_path):
    path = tmp_path / "fake.gguf"
    path.touch()
    embedder = HarnessEmbedder(path)
    embedder.last_cache_hit = True
    embedder.last_latency_ms = 5.0

    def other_worker():
        embedder.last_cache_hit = False
        embedder.last_latency_ms = 10.0
        return embedder.last_latency_ms

    with ThreadPoolExecutor(max_workers=1) as pool:
        assert pool.submit(other_worker).result() == 10.0
    assert embedder.last_cache_hit
    assert embedder.last_latency_ms == 5.0
