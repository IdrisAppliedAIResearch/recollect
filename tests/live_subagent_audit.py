"""Opt-in real-model conversation audit; never writes to the user's chat store.

Run with ``uv run --no-sync python tests/live_subagent_audit.py --case replay``.
Outputs are deliberately retained for review; remove the printed audit directory
after recording the findings. The owned Docker sandbox is removed on exit.
"""

from __future__ import annotations

import argparse
import asyncio
import csv
import json
import shutil
import time
import uuid
from contextlib import asynccontextmanager
from dataclasses import replace
from pathlib import Path

from recollect.api import AppState, create_app
from recollect.config import RecollectConfig
from recollect.task_chat import stream_task_turn

REPLAY = [
    (0, "Do you mind introducing yourself?"),
    (36, "I actually want to introduce you to my team at TRIA Federal. Do you "
     "want to do some quick research to see what our company is and what we do?"),
    (22, "You didn't actually start looking."),
    (27, "I guess while that's working, do you want to tell me a joke?"),
    (49, "actually find out like the competitors of trio federal"),
    (38, "What can you see of the work that the research agent's doing?"),
    (20, "No, it's okay. You don't have to."),
    (91, "Why did you not say anything when you got a partial answer?"),
]


class Audit:
    def __init__(self, state, output: Path):
        self.state = state
        self.output = output
        self.records = []
        self.seen = set()
        self.started = time.monotonic()
        self.sessions = []
        self.failures = []
        generate = state.generator.stream

        async def observed(messages, *, trace, tools=None, **kwargs):
            async for chunk in generate(messages, trace=trace, tools=tools, **kwargs):
                yield chunk
            self.record(
                "model_call", tools=[t["function"]["name"] for t in tools or []],
                calls=[call.model_dump() for call in trace.tool_calls],
            )

        state.generator.stream = observed

    def check(self, name, passed):
        self.record("check", name=name, passed=bool(passed))
        if not passed:
            self.failures.append(name)

    def record(self, kind, **data):
        item = {"kind": kind, "seconds": round(time.monotonic() - self.started, 2),
                **data}
        self.records.append(item)
        display = item
        if kind == "worker_messages":
            display = {"kind": kind, "task_id": data["task_id"],
                       "count": len(data["messages"])}
        elif kind == "final":
            display = {"kind": kind, "task": {key: data["task"].get(key) for key in
                       ("task_id", "state", "result", "artifacts", "error")}}
        print(json.dumps(display, ensure_ascii=True), flush=True)
        self.output.write_text(json.dumps(self.records, indent=2), encoding="utf-8")

    async def new_session(self, title):
        session = await asyncio.to_thread(self.state.sessions.create_session, title)
        self.sessions.append(session.session_id)
        self.record("session", session_id=session.session_id, title=title)
        return session.session_id

    async def turn(self, session, text):
        self.record("user", session_id=session, text=text)
        async for raw in stream_task_turn(
            self.state, session, text, input_mode="voice",
        ):
            event, data = raw.strip().split("\n", 1)
            payload = json.loads(data.removeprefix("data: "))
            if event == "event: error":
                self.record("error", session_id=session, **payload)
            elif event == "event: done":
                generation = payload.get("generation") or {}
                self.record(
                    "assistant", session_id=session,
                    text=generation.get("response_text"),
                    committed=payload["committed"],
                    calls=generation.get("tool_calls"),
                    memory_text=generation.get("memory_response_text"),
                )
        await self.observe(session)

    async def observe(self, session):
        snapshot = await self.state.tasks.snapshot(session)
        for notice in snapshot["notifications"]:
            signature = (session, notice["notification_id"])
            if signature not in self.seen:
                self.seen.add(signature)
                self.record("notification", session_id=session, notice=notice)
        for task in snapshot["tasks"]:
            signature = (task["task_id"], task["state"], task["accepted_revision"])
            if signature not in self.seen:
                self.seen.add(signature)
                self.record("task", task={key: task.get(key) for key in (
                    "task_id", "objective", "state", "accepted_revision", "error",
                )})
        return snapshot

    async def pause(self, session, seconds):
        end = time.monotonic() + seconds
        while time.monotonic() < end:
            await asyncio.sleep(min(2, end - time.monotonic()))
            await self.observe(session)

    async def settle(self, session):
        async with asyncio.timeout(900):
            while True:
                snapshot = await self.observe(session)
                active = any(
                    t["state"] in {"queued", "running", "cancel-requested"}
                    or t["state"] == "blocked" and self.state.tasks._has_owner(t)
                    and t["revision"] > (t["accepted_revision"] or 1)
                    for t in snapshot["tasks"]
                )
                pending = any(key[0] == session
                              for key in self.state.tasks._notifications)
                if not active and not pending:
                    break
                await asyncio.sleep(2)
        # Save bounded task/tool evidence for a human behavior audit, including
        # which skill bodies were actually loaded and any native edits.
        for task in snapshot["tasks"]:
            messages = await asyncio.to_thread(
                self.state.task_store.messages, session, task["task_id"],
            )
            self.record("final", session_id=session, task=task)
            self.record("worker_messages", task_id=task["task_id"], messages=messages)
        return snapshot

    async def replay(self):
        session = await self.new_session("TRIA exact transcript replay")
        for index, (delay, text) in enumerate(REPLAY):
            await self.pause(session, delay)
            await self.turn(session, text)
            if index == 1:
                snapshot = await self.state.tasks.snapshot(session)
                self.check("initial_research_request_started_work", snapshot["tasks"])
        await self.settle(session)
        self.check("replay_created_no_files", not list(
            self.state.config.downloads_dir.glob("*"),
        ))
        await self.turn(session, "Tell me the actual competitor names and how they "
                        "overlap. Please answer here, without making any files.")
        await self.settle(session)

    async def cases(self):
        session = await self.new_session("Research summary without files")
        await self.turn(session, "Research the difference between NASA's Artemis I "
                        "and Artemis II. Give me a quick summary and a comparison "
                        "list here. Do not make or save any files.")
        initial = await self.state.tasks.snapshot(session)
        self.check("research_summary_started_work", bool(initial["tasks"]))
        await self.turn(session, "What have you found so far?")
        await self.settle(session)
        self.check("research_summary_created_no_files", not list(
            self.state.config.downloads_dir.glob("*"),
        ))

        await self.turn(session, "Now save that comparison as one Markdown file "
                        "called artemis.md. Keep the explanation here too.")
        await self.settle(session)
        await self.turn(session, "Revise that file to add a short sources section. "
                        "Keep just the same Markdown format.")
        await self.settle(session)
        await self.turn(session, "Where can I find my document?")
        markdown = list(self.state.config.downloads_dir.glob("*.md"))
        self.check("requested_markdown_delivered_with_sources", any(
            "sources" in p.read_text(encoding="utf-8").lower() for p in markdown
        ))
        before_writing = set(self.state.config.downloads_dir.glob("*"))

        session = await self.new_session("Conversational writing without files")
        await self.turn(session, "Write a two-sentence thank-you note for a teammate "
                        "who helped fix a bug. Put it right here, no download.")
        await self.turn(session, "Summarize this in one sentence: the team fixed "
                        "three bugs and postponed one release. "
                        "Don't create a document.")
        ordinary = await self.settle(session)
        self.check("inline_writing_created_no_tasks_or_files",
                   not ordinary["tasks"] and before_writing == set(
                       self.state.config.downloads_dir.glob("*"),
                   ))

        session = await self.new_session("Explicit CSV research")
        await self.turn(session, "Look up the launch dates of Apollo 11 and Apollo 12 "
                        "on NASA's website. Save exactly one CSV named apollo.csv "
                        "with columns mission,launch_date,source_url, and tell me "
                        "the dates here too.")
        await self.settle(session)
        csv_files = list(self.state.config.downloads_dir.glob("*.csv"))
        self.check("exactly_one_requested_csv_delivered", len(csv_files) == 1)
        if csv_files:
            with csv_files[0].open(encoding="utf-8-sig", newline="") as stream:
                rows = list(csv.DictReader(stream))
            self.check("csv_dates_and_sources_verified", len(rows) == 2 and {
                (r.get("mission"), r.get("launch_date")) for r in rows
            } == {("Apollo 11", "1969-07-16"), ("Apollo 12", "1969-11-14")}
                and all("nasa.gov/" in r.get("source_url", "") for r in rows))

    async def evidence(self):
        for index in range(3):
            session = await self.new_session(f"Blocked evidence audit {index + 1}")
            task = await asyncio.to_thread(
                self.state.task_store.start, session, uuid.uuid4().hex,
                "Identify TRIA Federal's competitors", "Research competitors",
                "focused",
            )
            await asyncio.to_thread(
                self.state.task_store.accept_revision, session, task["task_id"], 1,
            )
            await asyncio.to_thread(
                self.state.task_store.update, session, task["task_id"],
                state="blocked",
            )
            text = ("Competitor websites could not be retrieved. No competitor names "
                    "or contract details have been verified or reported yet.")
            self.state.tasks._queue_update(
                (session, task["task_id"]), "controlled-finding", "finding", text, 1,
            )
            snapshot = await self.settle(session)
            notice = snapshot["notifications"][-1]["text"]
            invented = [name for name in (
                "Leidos", "Booz", "Accenture", "CSM", "CSDH", "EPAM", "Deloitte",
            ) if name.lower() in notice.lower()]
            self.record("check", name="no_names_invented_from_missing_evidence",
                        passed=not invented, invented=invented)

    async def files(self):
        session = await self.new_session("Explicit files from supplied facts")
        await self.turn(session, "Create exactly one Markdown file named brief.md "
                        "using only these supplied facts: Project Maple starts "
                        "Monday; its owner is Casey. Explain the contents here too. "
                        "No research or additional formats are needed.")
        await self.settle(session)
        await self.turn(session, "Revise brief.md to say the start is Tuesday. "
                        "Keep Casey as owner and keep only the Markdown format.")
        await self.settle(session)
        files = list(self.state.config.downloads_dir.glob("*.md"))
        self.check("revision_delivered_Tuesday_and_Casey", any(
            "Tuesday" in path.read_text(encoding="utf-8")
            and "Casey" in path.read_text(encoding="utf-8") for path in files
        ))
        await self.turn(session, "Where can I find my document?")
        session = await self.new_session("Ordinary writing without a file")
        await self.turn(session, "Write a two-sentence thank-you note for Casey "
                        "right here. Don't save or create any file.")
        await self.settle(session)

    async def routing(self):
        for prompt in (
            REPLAY[1][1],
            "Could you look up NASA's Apollo launch dates? Don't create files.",
            "I teach biology; research enzymes.",
        ):
            session = await self.new_session("Foreground research routing")
            await self.turn(session, prompt)
            snapshot = await self.state.tasks.snapshot(session)
            self.check("research_request_has_task", len(snapshot["tasks"]) == 1)
        session = await self.new_session("Foreground revision transfer")
        await self.turn(session, "Create brief.md using these facts: Project Maple "
                        "starts Monday and Casey is the owner.")
        snapshot = await self.state.tasks.snapshot(session)
        self.check("file_request_has_task", len(snapshot["tasks"]) == 1)
        if snapshot["tasks"]:
            parent = snapshot["tasks"][0]
            await asyncio.to_thread(self.state.task_store.update, session,
                                    parent["task_id"], state="completed")
            await self.turn(session, "Revise brief.md to start Tuesday, keep Casey.")
            snapshot = await self.state.tasks.snapshot(session)
            self.check("revision_instruction_preserved", any(
                t["parent_task_id"] == parent["task_id"] and "Tuesday" in t["objective"]
                for t in snapshot["tasks"]
            ))
        for prompt in ("What are enzymes?", "Don't research anything. Tell me a joke."):
            session = await self.new_session("Foreground ordinary conversation")
            await self.turn(session, prompt)
            snapshot = await self.state.tasks.snapshot(session)
            self.check("ordinary_reply_starts_no_work", not snapshot["tasks"])

    async def supervision(self):
        session = await self.new_session("Active worker supervision")
        await self.turn(session, "Research NASA's Apollo 11, Apollo 12, Artemis I "
                        "and Artemis II. Compare launch dates, duration and "
                        "objectives using official sources. Report useful findings "
                        "as you go. Answer here without creating any files.")
        await self.pause(session, 16)
        snapshot = await self.observe(session)
        active = [t for t in snapshot["tasks"] if self.state.tasks._has_owner(t)]
        self.check("worker_still_active_for_supervision", bool(active))
        if not active:
            return
        task = active[0]
        before = time.monotonic()
        await self.turn(session, "What is the subagent doing?")
        self.check("active_status_under_two_seconds", time.monotonic() - before < 2)
        before = time.monotonic()
        await self.turn(session, "Tell the subagent to compare ONLY Apollo 11 and "
                        "Apollo 12. Drop Artemis I and Artemis II from the final "
                        "comparison. Keep using official NASA sources and do "
                        "not create files.")
        self.check("steering_saved_under_two_seconds", time.monotonic() - before < 2)
        current = await asyncio.to_thread(self.state.task_store.get,
                                          session, task["task_id"])
        revision = current["revision"]
        self.check("same_worker_received_revision", revision > task["revision"])
        final = await self.settle(session)
        current = next(t for t in final["tasks"] if t["task_id"] == task["task_id"])
        self.check("worker_acknowledged_revision",
                   current["accepted_revision"] == revision)
        self.check("validated_result_completed_task", current["state"] == "completed")
        self.check("updates_come_from_reports", all(
            not n["notification_id"].startswith("heartbeat-")
            for n in final["notifications"]
        ))
        self.check("supervision_created_no_files", not list(
            self.state.config.downloads_dir.glob("*"),
        ))


@asynccontextmanager
async def audit_state(config, *, foreground_only):
    if not foreground_only:
        app = create_app(config, serve_ui=False)
        async with app.router.lifespan_context(app):
            yield app.state.recollect
        return
    # Real main model, memory and durable routing; no worker dispatcher is started.
    state = AppState(config)
    try:
        state.embedder_health = await asyncio.to_thread(state.embedder.warm_up)
        yield state
    finally:
        state.model_slot.close()
        await state.tasks.close()
        await state.sandboxes.close_all()
        await state.model_ingress.close()
        await state.generator.aclose()
        await state.web_client.aclose()

async def main(case):
    config = RecollectConfig.from_env()
    identifier = "behavior-audit-" + uuid.uuid4().hex[:12]
    root = (Path(".agent") / identifier).resolve()
    root.mkdir(parents=True)
    sandbox = config.sandbox_root.resolve() / identifier
    config = replace(config, data_dir=root / "data", downloads_dir=root / "Downloads",
                     sandbox_root=sandbox, subagent_enabled=True,
                     subagent_backend="opencode", subagent_continuous_enabled=True)
    print(f"AUDIT_DIRECTORY={root}", flush=True)
    try:
        async with audit_state(
            config, foreground_only=case == "routing",
        ) as state:
            audit = Audit(state, root / "results.json")
            audit.record("environment", model=config.generator_model,
                         context_tokens=config.generator_context_tokens,
                         temperature=config.generator_temperature,
                         embedder=state.embedder_health)
            if case == "replay":
                await audit.replay()
            elif case == "cases":
                await audit.cases()
            elif case == "files":
                await audit.files()
            elif case == "routing":
                await audit.routing()
            elif case == "supervision":
                await audit.supervision()
            else:
                await audit.evidence()
            audit.record("downloads", files=[p.name for p in
                         config.downloads_dir.glob("*")])
            if audit.failures:
                raise SystemExit("Behavior checks failed: " + ", ".join(audit.failures))
    finally:
        if sandbox.exists():
            assert sandbox.parent == RecollectConfig.from_env().sandbox_root.resolve()
            assert sandbox.name == identifier
            shutil.rmtree(sandbox)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--case", choices=["replay", "cases", "evidence", "files", "routing",
                           "supervision"],
        required=True,
    )
    asyncio.run(main(parser.parse_args().case))
