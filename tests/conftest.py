"""Shared fixtures.

The tests here deliberately do **not** load the real GGUF embedder. What
they exercise is the instrumentation's faithfulness to the library, which
is a property of the retrieval pipeline given *some* vectors, not of any
particular vectors. A deterministic fake makes that property testable in
milliseconds instead of minutes, and lets the suite run on a machine with
no model file at all.

The real embedder's identity is a separate concern with its own test, and
it is checked at runtime on every store open by the library's own sentinel
gate.
"""

from __future__ import annotations

import hashlib

import numpy as np
import pytest
from episodic import EpisodeStore, EpisodicConfig
from episodic._embedding import EMBEDDING_DIMENSION


class FakeEmbedder:
    """Deterministic pseudo-embeddings derived from the text itself.

    Same text always yields the same vector, different texts yield
    different ones, and the vectors carry enough structure that clustering
    and cosine ranking produce non-degenerate results. No model, no I/O.
    """

    def __init__(self, *, topics: int = 5) -> None:
        self.topics = topics
        self.calls = 0

    def __call__(self, text: str) -> np.ndarray:
        self.calls += 1
        digest = hashlib.sha256(text.encode("utf-8")).digest()
        seed = int.from_bytes(digest[:8], "big") % (2**32)
        rng = np.random.default_rng(seed)

        # A topic centroid plus noise. The topic is whichever keyword the
        # text mentions, found anywhere in it - not just at the start -
        # because the store embeds "User: ...\nAssistant: ..." while a query
        # is bare. Keying on position would make every episode share one
        # centroid and leave all cosines near zero, which is exactly the
        # degenerate geometry that makes a similarity threshold untestable.
        centroid = np.random.default_rng(_topic_seed(text)).normal(
            size=EMBEDDING_DIMENSION
        )
        noise = rng.normal(size=EMBEDDING_DIMENSION)
        vector = centroid * 1.0 + noise * 0.35
        return np.asarray(vector, dtype=np.float32).reshape(EMBEDDING_DIMENSION)


TOPICS = ["venice", "budget", "painting", "rainfall", "contracts"]


def _topic_seed(text: str) -> int:
    lowered = text.lower()
    for topic in TOPICS:
        if topic in lowered:
            key = topic
            break
    else:
        key = "untopiced"
    return int.from_bytes(
        hashlib.sha256(key.encode("utf-8")).digest()[:4], "big"
    ) % (2**32)


def make_episodes(count: int) -> list[tuple[str, str]]:
    """Conversation pairs spread across a handful of recurring topics."""
    pairs = []
    for index in range(count):
        topic = TOPICS[index % len(TOPICS)]
        pairs.append(
            (
                f"{topic} question {index}: what did we decide about "
                f"{topic} on item {index}?",
                f"{topic} answer {index}: we decided to record {topic} "
                f"item {index} and revisit it later. " + ("detail " * (index % 7)),
            )
        )
    return pairs


@pytest.fixture
def fake_embedder() -> FakeEmbedder:
    return FakeEmbedder()


@pytest.fixture
def config() -> EpisodicConfig:
    return EpisodicConfig()


def build_store(path, embedder, config, pairs) -> EpisodeStore:
    store = EpisodeStore(path, config, embedder=embedder)
    for user, assistant in pairs:
        store.append("user", user)
        store.append("assistant", assistant)
    return store


@pytest.fixture
def store(tmp_path, fake_embedder, config):
    """A 40-episode store: past the 32-episode recency window."""
    handle = build_store(
        tmp_path / "episodes.sqlite", fake_embedder, config, make_episodes(40)
    )
    yield handle
    handle.close()
