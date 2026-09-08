"""Sessions, stores, and the turn lifecycle.

A session is a directory: one append-only episode store, one index of turn
summaries, and one JSON trace per turn. Nothing here is a database of its
own - traces are files because they are records, not state, and a record
you can open in a text editor three months later is worth more than one
that needs the application running.

**Ordering matters and is easy to get wrong.** Retrieval for turn N runs
against the store *before* turn N is written to it, so the exchange
currently being generated is never in its own context. The episode is
formed only once the assistant has replied, because an episode is the
user/assistant pair - a half-written turn is not a memory. This mirrors the
library's own contract, where ``append("user", ...)`` merely sets a message
aside and ``append("assistant", ...)`` is what makes the episode durable.

**Stores are opened per turn rather than held open.** SQLite connections
are bound to the thread that made them, and the server runs blocking work
in a pool where the thread is not stable. Opening per turn sidesteps that
entirely, and it is cheap here: the two costs at open are an integrity
check and a sentinel embedding, and the sentinel is a constant string that
the embedder's memo cache answers without touching the model.
"""

from __future__ import annotations

import json
import sqlite3
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from episodic import EpisodeStore, EpisodicConfig, EpisodicError
from episodic._embedding import embed_solo
from pydantic import BaseModel

from .config import RecollectConfig
from .engine._internals import read_episodes, store_meta
from .engine.embedder import HarnessEmbedder
from .engine.shadow import RetrievalResult, retrieve_with_trace
from .trace import (
    QueryTrace,
    StoreTrace,
    TurnSummary,
    TurnTrace,
)


class SessionInfo(BaseModel):
    session_id: str
    created_at: datetime
    title: str
    turn_count: int = 0


@dataclass
class PreparedTurn:
    """Retrieval is done; the model has not been called yet.

    Splitting the turn here is what lets the inspector fill in the moment
    retrieval finishes rather than after the model has finished talking -
    which, for a several-second generation, is the difference between
    watching the mechanism work and reading about it afterwards.
    """

    trace: TurnTrace
    retrieval: RetrievalResult
    user_message: str


class SessionManager:
    """Owns the data directory, the embedder, and every session in it."""

    def __init__(self, config: RecollectConfig, embedder: HarnessEmbedder) -> None:
        self.config = config
        self.embedder = embedder
        self.config.sessions_dir.mkdir(parents=True, exist_ok=True)

    # -- session lifecycle -------------------------------------------------

    def create_session(self, title: str | None = None) -> SessionInfo:
        session_id = uuid.uuid4().hex[:12]
        created = datetime.now(UTC)
        info = SessionInfo(
            session_id=session_id,
            created_at=created,
            title=title or f"Session {created:%Y-%m-%d %H:%M}",
        )
        directory = self.config.session_dir(session_id)
        directory.mkdir(parents=True, exist_ok=True)
        self.config.traces_dir(session_id).mkdir(parents=True, exist_ok=True)
        self._write_info(info)
        return info

    def list_sessions(self) -> list[SessionInfo]:
        sessions = []
        for path in sorted(self.config.sessions_dir.glob("*/session.json")):
            try:
                sessions.append(self._read_info(path))
            except (json.JSONDecodeError, OSError):
                continue
        return sorted(sessions, key=lambda s: s.created_at, reverse=True)

    def get_session(self, session_id: str) -> SessionInfo:
        path = self.config.session_dir(session_id) / "session.json"
        if not path.is_file():
            raise KeyError(f"No such session: {session_id}")
        return self._read_info(path)

    def _write_info(self, info: SessionInfo) -> None:
        path = self.config.session_dir(info.session_id) / "session.json"
        path.write_text(info.model_dump_json(indent=2), encoding="utf-8")

    def _read_info(self, path: Path) -> SessionInfo:
        return SessionInfo.model_validate_json(path.read_text(encoding="utf-8"))

    # -- stores -------------------------------------------------------------

    def open_store(self, session_id: str) -> EpisodeStore:
        path = self.config.store_path(session_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        return EpisodeStore(
            path, self._offer_config(session_id), embedder=self.embedder
        )

    def _offer_config(self, session_id: str) -> EpisodicConfig:
        """The config to offer this session's store at open.

        A store's numbers are only meaningful under the config that produced
        them, so an existing store is opened under the config it was created
        with - not the deployment's current one, which a later deployment
        change (say, switching ASPECT on) would otherwise fail it with on
        every open. New stores pin the deployment config at first open. No
        ``override_config`` anywhere: a store that cannot be parsed back into
        the current config schema is stopped, not silently rebound.
        """
        path = self.config.store_path(session_id)
        if not path.is_file():
            return self.config.episodic
        stored = self._stored_config_json(path)
        if stored is None or stored == self.config.episodic.to_json():
            return self.config.episodic
        try:
            offered = EpisodicConfig.from_json(stored)
        except EpisodicError as error:
            raise ValueError(
                f"Session {session_id}'s store carries a config the current "
                f"library cannot parse ({error}); it predates or postdates "
                "this build. Delete the session directory to start clean."
            ) from error
        if offered.to_json() != stored:
            raise ValueError(
                f"Session {session_id}'s store was created under a config "
                "schema that no longer round-trips (it predates the CC-007 "
                "adoption). Its numbers came from the retired pipeline, so "
                "opening it here would mix two mechanisms. Delete the "
                "session directory to start clean."
            )
        return offered

    @staticmethod
    def _stored_config_json(path: Path) -> str | None:
        """Read the config pin out of an existing store without opening it.

        A direct SQLite read with an explicit close: EpisodeStore would open
        (and therefore already need to know) the very config we are trying
        to discover.
        """
        connection = sqlite3.connect(str(path))
        try:
            row = connection.execute(
                "SELECT value FROM episodic_meta WHERE key = 'config'"
            ).fetchone()
        except sqlite3.Error:
            return None
        finally:
            connection.close()
        return None if row is None else str(row[0])

    def episode(self, session_id: str, episode_id: str) -> dict | None:
        """Full body of one episode. Trace rows carry only previews."""
        store = self.open_store(session_id)
        try:
            for episode in read_episodes(store):
                if str(episode["id"]) == episode_id:
                    return {
                        "id": str(episode["id"]),
                        "turn_number": int(episode["turn_number"]),
                        "user_message": episode["user_message"],
                        "assistant_message": episode["assistant_message"],
                    }
        finally:
            store.close()
        return None

    # -- the turn -----------------------------------------------------------

    def prepare_turn(self, session_id: str, user_message: str) -> PreparedTurn:
        """Everything up to the model call: embed, retrieve, verify, trace.

        Blocking and CPU-bound. Call it off the event loop.
        """
        info = self.get_session(session_id)
        store = self.open_store(session_id)
        # The store's own pinned config governs the turn: retrieval must run
        # under the config the episodes were created with, which for an
        # existing store may differ from the deployment's current one.
        store_config = store.config
        try:
            episodes = read_episodes(store)

            self.embedder.last_cache_hit = False
            query_embedding = embed_solo(self.embedder, user_message)
            query_trace = QueryTrace(
                text=user_message,
                chars=len(user_message),
                embedding_sha256=_vector_sha256(query_embedding),
                embedding_norm=float(
                    (query_embedding.astype("float64") ** 2).sum() ** 0.5
                ),
                embed_latency_ms=self.embedder.last_latency_ms,
                embed_cache_hit=self.embedder.last_cache_hit,
            )

            store_trace = StoreTrace(
                path=str(self.config.store_path(session_id)),
                episode_count=len(episodes),
                config_json=store_config.to_json(),
                sentinel_sha256=store_meta(store, "sentinel_sha256") or "",
                embedder_model_sha256=store_config.embedder_sha256,
            )

            retrieval = retrieve_with_trace(
                episodes=episodes,
                query_text=user_message,
                query_embedding=query_embedding,
                budget=self.config.budget_chars,
                config=store_config,
                strict=True,
            )
        finally:
            store.close()

        trace = TurnTrace(
            turn_id=uuid.uuid4().hex[:16],
            session_id=session_id,
            turn_index=info.turn_count,
            started_at=datetime.now(UTC),
            query=query_trace,
            store=store_trace,
            candidates=retrieval.candidates,
            tiers=retrieval.tiers,
            cc80_detail=retrieval.cc80_detail,
            aspect_detail=retrieval.aspect_detail,
            packing=retrieval.packing,
            context_block=retrieval.context_block,
            report=retrieval.report,
            verification=retrieval.verification,
            generation=None,
        )
        return PreparedTurn(
            trace=trace, retrieval=retrieval, user_message=user_message
        )

    def commit_turn(self, prepared: PreparedTurn, assistant_message: str) -> None:
        """Write the episode, persist the trace, bump the session count.

        The episode is written first: once ``append("assistant", ...)``
        returns, the exchange is fsynced and cannot be lost. The trace is a
        record of how the reply was produced and is written after, because
        losing a trace costs an explanation while losing an episode costs a
        memory.
        """
        session_id = prepared.trace.session_id
        store = self.open_store(session_id)
        try:
            store.append("user", prepared.user_message)
            store.append("assistant", assistant_message)
        finally:
            store.close()

        self.save_trace(prepared.trace)

        info = self.get_session(session_id)
        info.turn_count += 1
        self._write_info(info)

    # -- traces -------------------------------------------------------------

    def save_trace(self, trace: TurnTrace) -> None:
        directory = self.config.traces_dir(trace.session_id)
        directory.mkdir(parents=True, exist_ok=True)
        (directory / f"{trace.turn_id}.json").write_text(
            trace.model_dump_json(indent=2), encoding="utf-8"
        )
        with (self.config.session_dir(trace.session_id) / "turns.jsonl").open(
            "a", encoding="utf-8"
        ) as handle:
            handle.write(summarize(trace).model_dump_json() + "\n")

    def get_trace(self, session_id: str, turn_id: str) -> TurnTrace | None:
        path = self.config.traces_dir(session_id) / f"{turn_id}.json"
        if not path.is_file():
            return None
        try:
            return TurnTrace.model_validate_json(path.read_text(encoding="utf-8"))
        except ValueError:
            # A trace written before schema v2 does not validate against it.
            # Report it as absent rather than as an error: it is a record of
            # a different mechanism, not a failed record of this one.
            return None

    def find_trace(self, turn_id: str) -> TurnTrace | None:
        """Locate a turn without knowing its session."""
        for candidate in self.config.sessions_dir.glob(
            f"*/traces/{turn_id}.json"
        ):
            try:
                return TurnTrace.model_validate_json(
                    candidate.read_text(encoding="utf-8")
                )
            except ValueError:
                return None
        return None

    def list_turns(self, session_id: str) -> list[TurnSummary]:
        path = self.config.session_dir(session_id) / "turns.jsonl"
        if not path.is_file():
            return []
        summaries = []
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                summaries.append(TurnSummary.model_validate_json(line))
            except ValueError:
                continue
        return summaries


def summarize(trace: TurnTrace) -> TurnSummary:
    response = trace.generation.response_text if trace.generation else ""
    return TurnSummary(
        turn_id=trace.turn_id,
        session_id=trace.session_id,
        turn_index=trace.turn_index,
        started_at=trace.started_at,
        query_preview=_clip(trace.query.text),
        response_preview=_clip(response),
        episodes_delivered=trace.report.episodes_delivered,
        episodes_dropped=trace.report.episodes_dropped,
        chars_delivered=trace.report.chars_delivered,
        budget_chars=trace.report.budget_chars,
        stm_count=trace.report.stm_count,
        k_count=trace.report.k_count,
        coverage_count=trace.report.coverage_count,
        recency_count=trace.report.recency_count,
        semantic_count=trace.report.semantic_count,
        aspect_count=trace.report.aspect_count,
        starved_tiers=list(trace.starved_tiers),
        trace_trustworthy=trace.verification.trustworthy,
    )


def _clip(text: str, limit: int = 160) -> str:
    flat = " ".join(str(text).split())
    return flat if len(flat) <= limit else flat[: limit - 1] + "…"


def _vector_sha256(vector) -> str:
    import hashlib

    import numpy as np

    return hashlib.sha256(
        np.asarray(vector, dtype=np.float32).tobytes()
    ).hexdigest()
