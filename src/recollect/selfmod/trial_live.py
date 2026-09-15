"""Live trusted collectors and actions for one unattended primary attempt.

``LiveTrialEnvironment`` implements ``TrialEnvironment`` over real services: the
experiment app state (three lanes, continuous OpenCode, amendment 02 profile),
the live Google provider broker and read-only verifier, the provider relay, the
A/B deployment router, the native modifier and the frozen candidate evaluator.
Waiting uses a fixed observation cadence, never a deadline. Credentials stay in
their user-run store; no token value is logged or written to evidence.
"""

import asyncio
import json
import time
import uuid
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from pathlib import Path

import httpx

from ..engine.embedder import HarnessEmbedder
from ..engine.generator import Generator, GeneratorSettings
from ..engine.model_admission import ModelAdmission, ModelIngress
from ..engine.sandbox.manager import SandboxDeployment, SandboxManager
from ..session import SessionManager
from ..task_chat import stream_task_turn
from ..task_store import TaskStore
from ..tasks import TaskCoordinator
from . import acceptance, subagent_tree, trial_manifest
from .calendar_evaluator import CalendarVerifier, ExpectedEvent, attribute, cleanup
from .candidate_evaluator import CandidateEvaluator
from .concurrency import (
    REQUIRED_OVERLAP_NS,
    LaneProbe,
    generating_intervals,
    longest_overlap,
    run_concurrency_check,
)
from .contracts import File, Snapshot
from .controller import PREFLIGHT_CHECKS
from .deployment import (
    BundleImages,
    DeploymentRouter,
    DeploymentSandboxes,
    SubagentBundle,
    TaskDeployments,
    materialize_skills,
)
from .docker_runtime import DockerFixtureRuntime
from .gap_trigger import parse_gap_report
from .google_auth import SCOPES, RefreshingCredential
from .integration import DevelopmentSettings
from .journal import IntegrityError, Journal, encode
from .model_settlement import SlotObserver, loopback_root
from .native_runtime import NativeRuntime
from .provider_broker import ProviderBroker, ProviderPolicy
from .provider_relay import ProviderRelay
from .roles import LocalRoleModel, RoleSettings
from .tool_invocations import ToolInvocations
from .trial import PROBE_POINTS, Activation, TargetOutcome

#: Observation cadence for durable task state; spacing, not a deadline.
OBSERVE_SECONDS = 1.0
#: /slots sampling cadence for the A/modifier overlap evidence.
SLOT_POLL_SECONDS = 0.05
WORKER_SLOT, MODIFIER_SLOT = 1, 2
ACTION_ID = "primary-calendar-action"
TERMINAL_TASK_STATES = frozenset({"completed", "canceled", "interrupted"})


class ExperimentState:
    """The app surface ``stream_task_turn`` and the served UI read."""

    def __init__(self, config, embedder, sessions, generator, store, tasks,
                 sandboxes):
        self.config, self.embedder, self.sessions = config, embedder, sessions
        self.generator, self.task_store, self.tasks = generator, store, tasks
        self.sandboxes = sandboxes
        self.voice = None
        self.web_client = httpx.AsyncClient(trust_env=False)
        self.embedder_health = {}
        self._locks = {}

    def lock(self, session_id):
        return self._locks.setdefault(session_id, asyncio.Lock())


@dataclass(frozen=True)
class LiveSettings:
    repository: Path
    manifest: trial_manifest.RuntimeManifest
    attempt_root: Path
    docker: object          # NativeDocker pinned CLI
    credential_store: Path
    base_config: object     # RecollectConfig from the frozen environment


async def consume_turn(state, session_id, message, request_id):
    """Drive one in-process main-chat turn and keep its complete event stream."""
    events, text, error = [], [], None
    started = time.monotonic_ns()
    async for chunk in stream_task_turn(state, session_id, message,
                                        request_id=request_id):
        events.append(chunk)
        kind, _, data = chunk.partition("\n")
        payload = json.loads(data.removeprefix("data: ").strip() or "null")
        if kind == "event: token":
            text.append(payload["text"])
        elif kind == "event: error":
            error = payload["message"]
    return {"request_id": request_id, "text": "".join(text), "error": error,
            "submitted_ns": started, "completed_ns": time.monotonic_ns(),
            "events": events}


class LiveTrialEnvironment:
    def __init__(self, settings: LiveSettings):
        self.settings = settings
        manifest = settings.manifest.value
        self.frozen = manifest
        self.config = replace(
            settings.base_config, subagent_backend="opencode",
            subagent_continuous_enabled=True, subagent_enabled=True,
            generator_parallel_slots=3, experiment_unbounded=True,
            data_dir=settings.attempt_root / "app")
        calendar, experiment = manifest["calendar"], manifest["experiment"]
        self.expected = ExpectedEvent(experiment["id"], datetime.fromisoformat(
            experiment["event_date"]).date(), calendar["time_zone"])
        self.request = experiment["request"]
        self._receipts = {}
        self._target = None      # (session_id, task_id, request_id)
        self.started = False
        self.manager_a = self.manager_b = None
        self._slot_samples, self._sampler, self._sampler_stop = [], None, None
        self._evaluator = self._evaluation = self._event_id = None
        self._cp1_closed_ns = self._sealed_after_ns = None

    # -- service lifecycle ---------------------------------------------------

    async def start(self):
        settings, config = self.settings, self.config
        root = settings.attempt_root
        self.embedder = HarnessEmbedder(config.embedding_model_path,
                                        n_threads=config.embedding_threads)
        self.embedder_health = await asyncio.to_thread(self.embedder.warm_up)
        self.sessions = SessionManager(config, self.embedder)
        self.admission = ModelAdmission(slots=3)
        self.ingress = ModelIngress(config, self.admission)
        self.generator = Generator(GeneratorSettings(
            base_url=config.generator_base_url, model=config.generator_model,
            api_key=config.generator_api_key, thinking=config.generator_thinking,
            max_tokens=config.generator_max_tokens,
            temperature=config.generator_temperature,
            context_tokens=config.generator_context_tokens, require_tools=True,
            unbounded=True), model_slot=self.admission)
        store = settings.credential_store
        self.provider_journal = Journal.create(root / "provider")
        self.broker = ProviderBroker(
            ProviderPolicy(self.frozen["calendar"]["calendar_id"]),
            self.provider_journal,
            credentials={role: RefreshingCredential(store, role)
                         for role in ("worker", "verifier")},
            transport=httpx.AsyncHTTPTransport(retries=0))
        self.verifier = CalendarVerifier(self.broker, self.broker.issue("verifier"),
                                         self.expected)
        self.tracker = ToolInvocations()
        self.relay = ProviderRelay(self.broker, alias=self.frozen["calendar"]["alias"],
                                   invocation_for=self.tracker.running)
        self.routing_journal = Journal.create(root / "routing")
        self.router = DeploymentRouter(self.routing_journal)
        self.selector = DeploymentSandboxes(self.router)
        self.images = BundleImages(settings.docker)
        self.tree = subagent_tree.baseline(settings.repository)
        self.bundle_a = SubagentBundle(self.tree,
                                       self.frozen["subagent"]["base_image_id"],
                                       subagent_tree.LAUNCH)
        await self.ingress.start()
        await self.relay.start()
        image = await self.images.build(self.bundle_a)
        self.verified_a = await self.images.verify(self.bundle_a, image)
        self.router.register_a(self.verified_a)
        self.manager_a = self._manager("A", self.bundle_a, self.verified_a)
        self.selector.register("A", self.manager_a, self.verified_a)
        self.task_store = TaskStore(config)
        self.tasks = TaskCoordinator(
            config, self.sessions, self.task_store, self.generator, self.manager_a,
            deployments=TaskDeployments(self.router, self.selector),
            tool_observer=self.tracker.observe)
        self.state = ExperimentState(config, self.embedder, self.sessions,
                                     self.generator, self.task_store, self.tasks,
                                     self.manager_a)
        self.state.embedder_health = self.embedder_health
        await self.tasks.start()
        self.started = True

    def _manager(self, role, bundle, verified):
        root = self.config.sandbox_root / ("selfmod-" + role.lower() + "-"
                                           + uuid.uuid4().hex)
        skills = materialize_skills(bundle, root.with_name(root.name + "-skills"))
        manager = SandboxManager(self.config, model_slot=self.admission,
                                 deployment=SandboxDeployment(
                                     verified.image_id, skills, root,
                                     subagent_tree.BUNDLE_PYTHONPATH))
        manager.configure_model(self.ingress.base_url, self.ingress.token)
        manager.configure_tools(self.relay.base_url, self.relay.token)
        return manager

    # -- CP0 -------------------------------------------------------------------

    async def current_identities(self):
        frozen = self.frozen
        return await trial_manifest.collect_identities(
            self.settings.repository, base_url=self.config.generator_base_url,
            model=self.config.generator_model,
            weight_path=frozen["model"]["weight_path"],
            base_image_id=self.bundle_a.base_image_id,
            calendar=dict(calendar_id=self.broker.policy.calendar_id,
                          alias=self.relay.alias, time_zone=self.expected.time_zone),
            credential_store=self.settings.credential_store)

    async def preflight(self):
        frozen = self.frozen
        files, checks = {}, dict.fromkeys(PREFLIGHT_CHECKS, False)
        current = await self.current_identities()
        leaves = {key: frozen[key] for key in ("registrations", "documents",
                                               "source", "subagent", "calendar")}
        # Lane probes are frozen inputs used below, not recomputed identities.
        leaves["model"] = {k: v for k, v in frozen["model"].items()
                           if k != "lane_probes"}
        mismatches = trial_manifest.compare(leaves, current)
        start_date = trial_manifest.registered_event_date(datetime.now(UTC))
        if start_date != frozen["experiment"]["event_date"]:
            mismatches.append("/experiment/event_date")
        if current["source"]["clean"] is not True:
            mismatches.append("/source/clean")
        checks["runtime_frozen"] = not mismatches
        checks["evaluator_frozen"] = (
            current["evaluator"]["sha256"] == frozen["evaluator"]["sha256"])
        authorization = {}
        for role in ("worker", "verifier"):
            try:
                await asyncio.to_thread(self.broker._credentials[role])
                scope = current["calendar"]["scopes"][role]
                authorization[role] = scope == SCOPES[role]
            except Exception as error:  # noqa: BLE001 - recorded, never a pass
                authorization[role] = False
                files[f"preflight/authorization-{role}-error.json"] = encode(
                    {"error_type": type(error).__name__})
        checks["same_authorization"] = all(authorization.values())
        empty, search = await self.verifier.baseline_empty()
        checks["baseline_empty"] = empty is True
        probes = tuple(LaneProbe(**probe) for probe in frozen["model"]["lane_probes"])
        concurrency = await run_concurrency_check(
            self.config.generator_base_url, self.config.generator_model, probes)
        checks["three_way_generation"] = concurrency.get("passed") is True
        files.update({
            "preflight/identities.json": encode(current),
            "preflight/mismatches.json": encode({"paths": mismatches,
                                                 "registered_event_date": start_date}),
            "preflight/authorization.json": encode(authorization),
            "preflight/baseline-search.json": encode(search),
            "preflight/concurrency.json": encode(concurrency),
            "preflight/manifest.json": self.settings.manifest.raw,
        })
        return checks, _snapshot(files)

    # -- baseline --------------------------------------------------------------

    def _bind(self, task_id, digest, epoch, *, phase="original", invocation_id=None):
        capability = self.broker.issue(
            "worker", task_id=task_id, action_id=ACTION_ID,
            dedup_id=self.frozen["experiment"]["dedup_id"],
            routing_epoch=epoch, serving_digest=digest)
        self.relay.bind(capability, phase=phase, invocation_id=invocation_id)
        return capability

    async def run_baseline(self):
        session = await asyncio.to_thread(self.sessions.create_session,
                                          "Self-modification primary attempt")
        request_id = "primary-request-" + uuid.uuid4().hex
        # A holds the same pre-authorized access B will hold when serving.
        self._bind(None, self.bundle_a.digest, self.router.epoch)
        self.broker.open_gate(ACTION_ID, "baseline target request submitted")
        turn = await consume_turn(self.state, session.session_id, self.request,
                                  request_id)
        files = {"baseline/main-turn.json": encode(turn)}
        task = await asyncio.to_thread(self.task_store.request, session.session_id,
                                       request_id)
        if task is None:
            return TargetOutcome(None, None, None, False, False, True,
                                 _snapshot(files))
        key = (session.session_id, task["task_id"])
        self._target = (*key, request_id)
        self._bind(task["task_id"], self.bundle_a.digest, self.router.epoch)
        report, claimed = None, False
        while True:
            task = await asyncio.to_thread(self.task_store.get, *key)
            messages = await asyncio.to_thread(self.task_store.messages, *key, 0,
                                               "subagent")
            for message in messages:
                if message["kind"] == "blocked" and report is None:
                    report = parse_gap_report(message)
                claimed = claimed or message["kind"] == "result"
            if (report is not None or claimed
                    or task["state"] in TERMINAL_TASK_STATES
                    or (task["state"] == "blocked"
                        and not self.tasks._has_owner(task))):
                break
            await asyncio.sleep(OBSERVE_SECONDS)
        operations = [r.value["data"] for r in self.provider_journal.verify()
                      if r.value["kind"] == "provider_operation"
                      and r.value["data"]["action_id"] == ACTION_ID]
        unknown = any(o["kind"] == "mutation" and (o["failure"] or o["transient"])
                      for o in operations)
        files.update({"baseline/task.json": encode(task),
                      "baseline/messages.json": encode({"items": messages}),
                      "baseline/provider-operations.json": encode(
                          {"items": operations})})
        same = (self.router.serving.bundle_digest == self.bundle_a.digest
                and (await self.current_identities())["calendar"]["scopes"]
                == self.frozen["calendar"]["scopes"])
        return TargetOutcome(task["task_id"], "start:" + request_id, report,
                             claimed or task["state"] == "completed", unknown, same,
                             _snapshot(files))

    async def stop_target(self, task_id):
        session_id, _, request_id = self._target
        await self.tasks.command(session_id, task_id, "cancel-" + request_id,
                                 "cancel")
        while True:
            task = await asyncio.to_thread(self.task_store.get, session_id, task_id)
            if (not self.tasks._has_owner(task) and self.manager_a._active is None
                    and task["state"] not in {"running", "queued",
                                              "cancel-requested"}):
                return True
            await asyncio.sleep(OBSERVE_SECONDS)

    async def calendar_empty(self):
        empty, search = await self.verifier.baseline_empty()
        known = search["complete"]
        return (empty if known else None), _snapshot(
            {"baseline/independent-search.json": encode(search)})

    async def close_target_gate(self, reason):
        self.broker.close_gate(ACTION_ID, reason)
        self.relay.unbind(reason)
        self._cp1_closed_ns = time.monotonic_ns()

    async def start_workload(self):
        session = await asyncio.to_thread(self.sessions.create_session,
                                          "Unrelated research workload")
        workload = self.frozen["experiment"]["workload_request"]
        task = await self.tasks.submit(session.session_id,
                                       "workload-" + uuid.uuid4().hex, workload,
                                       workload)
        self._receipts["workload"] = {"session_id": session.session_id,
                                      "task_id": task["task_id"]}

    async def probe(self, point):
        probe = self.frozen["experiment"]["probe"]
        session = await asyncio.to_thread(self.sessions.create_session,
                                          "Responsiveness probe " + point)
        turn = await consume_turn(self.state, session.session_id, probe["prompt"],
                                  "probe-" + point + "-" + uuid.uuid4().hex)
        correct = turn["error"] is None and probe["answer"] in turn["text"]
        return {"point": point, "prompt": probe["prompt"], "answer": turn["text"],
                "error": turn["error"], "correct": correct,
                "submitted_ns": turn["submitted_ns"],
                "completed_ns": turn["completed_ns"],
                "duration_ns": turn["completed_ns"] - turn["submitted_ns"],
                "events": turn["events"]}


    # -- modification and evaluation -------------------------------------------

    async def _sample_slots(self, stop):
        observer = SlotObserver(loopback_root(self.config.generator_base_url))
        try:
            while not stop.is_set():
                self._slot_samples.append(await observer.slots())
                await asyncio.sleep(SLOT_POLL_SECONDS)
        finally:
            await observer.aclose()

    def _issue_requirements(self):
        return (self.settings.repository / trial_manifest.FROZEN_DOCUMENTS[0]
                ).read_text(encoding="utf-8")

    async def develop(self, controller, outcome, feedback, on_first_generation):
        native = self.frozen["native"]
        policy = subagent_tree.change_policy(self.tree)
        settings = DevelopmentSettings(native["image_id"],
                                       tuple(native["image_environment"]),
                                       subagent_tree.PROTECTED[0])
        dev = await asyncio.to_thread(lambda: controller.open_development(
            baseline=self.tree, policy=policy, settings=settings))
        profile = RoleSettings(self.config.generator_base_url,
                               self.config.generator_model, acceptance.check_files(),
                               slot=MODIFIER_SLOT)
        observed = False

        async def first():
            nonlocal observed
            if observed:
                return
            observed = True
            if self._sampler is None:
                self._sampler_stop = asyncio.Event()
                self._sampler = asyncio.create_task(
                    self._sample_slots(self._sampler_stop))
            await on_first_generation()

        def model_factory(role_settings):
            model = LocalRoleModel(role_settings)
            complete = model.complete

            async def complete_after_observation(payload, deadline, **kwargs):
                await first()
                return await complete(payload, deadline, **kwargs)

            model.complete = complete_after_observation
            return model

        argv = self.settings.docker.argv
        executable, endpoint = Path(argv[0]), argv[argv.index("--host") + 1]
        roles_root = self.config.sandbox_root / ("selfmod-roles-" + uuid.uuid4().hex)
        roles_root.mkdir(parents=True)
        archive = self.settings.attempt_root / "native"
        archive.mkdir(exist_ok=True)
        prompt = acceptance.modifier_prompt(self.request, outcome.report,
                                            self._issue_requirements(),
                                            _feedback(feedback))
        loop = asyncio.get_running_loop()

        def native_factory(owner):
            return NativeRuntime(
                owner, docker=self.settings.docker,
                sandbox_root=self.config.sandbox_root, archive_root=archive,
                image_id=native["image_id"],
                image_environment=tuple(native["image_environment"]),
                native_binary_sha256=native["binary_sha256"], baseline=self.tree,
                loop=loop)

        async def native_executor(grant):
            await first()
            await dev.execute_native(
                grant, native_factory, prompt=prompt,
                model=self.config.generator_model,
                context_limit=native["context_limit"],
                output_limit=native["output_limit"],
                base_url=self.config.generator_base_url, slot=MODIFIER_SLOT)

        await dev.run_until_ready(
            profile, lambda: DockerFixtureRuntime(executable, roles_root, endpoint),
            model_factory=model_factory, native_executor=native_executor)
        await asyncio.to_thread(lambda: dev.submit(dev.authorize("submit")))
        return dev._development._artifact

    async def evaluate(self, candidate):
        if self._evaluator is None:
            self._evaluator = CandidateEvaluator(
                self.config, docker=self.settings.docker,
                base_image_id=self.bundle_a.base_image_id, admission=self.admission,
                sandbox_root=self.config.sandbox_root)
        return await self._evaluator.evaluate(candidate)

    # -- activation --------------------------------------------------------------

    def _operations(self):
        return [r.value["data"] for r in self.provider_journal.verify()
                if r.value["kind"] == "provider_operation"
                and r.value["data"]["action_id"] == ACTION_ID]

    async def activate(self, controller, evaluation, outcome):
        verified = evaluation.verified
        self._evaluation = evaluation
        self.router.stage_b(verified)
        self.manager_b = self._manager("B", verified.bundle, verified)
        self.selector.register("B", self.manager_b, verified)
        self.router.begin_activation()
        session_id, parent, request_id = self._target
        original = await asyncio.to_thread(self.task_store.get, session_id, parent)
        continuation = await self.tasks.submit(
            session_id, "continue-" + request_id, original["objective"],
            original["original_message"], original["effort"], parent,
            continuation=True)
        target = await asyncio.to_thread(self.task_store.get, session_id, parent)
        empty, _ = await self.calendar_empty()
        writes_since_cp1 = [o for o in self._operations() if o["kind"] == "mutation"
                            and o["dispatched_ns"] > self._cp1_closed_ns]
        checks = self.router.activation_checks(
            b_healthy=await _healthy(self.manager_b),
            a_available=await _healthy(self.manager_a),
            target_quiescent=(not self.tasks._has_owner(target)
                              and not writes_since_cp1),
            baseline_empty=empty is True)
        sealed = await asyncio.to_thread(self.router.seal, controller, checks)
        self._sealed_after_ns = time.monotonic_ns()
        if not sealed:
            return Activation(None)
        self.router.release_continuation(continuation["task_id"])
        self._bind(continuation["task_id"], verified.bundle.digest, self.router.epoch)
        self.broker.open_gate(ACTION_ID, "CP4 sealed; continuation released to B")
        await self.tasks.release_held(session_id, continuation["task_id"])
        return Activation(continuation["task_id"])

    # -- CP5 ---------------------------------------------------------------------

    async def _replay(self, task_id, invocation, tool, arguments):
        """Re-execute the recorded generated tool call inside B's serving container."""
        from mcp import ClientSession
        from mcp.client.stdio import StdioServerParameters, stdio_client

        digest = self._evaluation.verified.bundle.digest
        replay_id = "replay:" + invocation
        self._bind(task_id, digest, self.router.epoch, phase="replay",
                   invocation_id=replay_id)
        handle = self.manager_b._handle
        if handle is None or handle.container is None:
            raise IntegrityError("B's serving container is not running")
        docker = self.settings.docker
        # Relay settings pass by name from the CLI environment, never as arguments.
        parameters = StdioServerParameters(
            command=docker.argv[0],
            args=[*docker.argv[1:], "exec", "-i", "--user", "65532:65532",
                  "--env", "PYTHONPATH=" + subagent_tree.BUNDLE_PYTHONPATH,
                  "--env", "RECOLLECT_TASK_REPORTING=1",
                  "--env", "RECOLLECT_PROVIDER_URL",
                  "--env", "RECOLLECT_PROVIDER_TOKEN",
                  handle.container.name, "/usr/local/bin/python", "-m",
                  subagent_tree.TOOL_HOST_MODULE],
            env={**dict(docker.environment), **self.manager_b._tool_environment})
        async with stdio_client(parameters) as (read, write), ClientSession(
                read, write) as session:
            await session.initialize()
            result = await session.call_tool(tool, arguments)
        self._receipts["replay_tool_result"] = {"is_error": result.isError}
        replayed = [o for o in self._operations() if o["invocation_id"] == replay_id]
        read_ids = [(o.get("response_fields") or {}).get("id") for o in replayed
                    if o["status"] == 200]
        if read_ids:
            return read_ids[-1]
        if any(o["kind"] == "mutation" and o["status"] == 409 for o in replayed):
            # Google refuses a duplicate client event ID: that identity exists.
            return self.frozen["experiment"]["dedup_id"]
        return None

    async def verify_outcome(self, activation, probes):
        session_id = self._target[0]
        task_id = activation.continuation_task_id
        key = (session_id, task_id)
        while True:
            task = await asyncio.to_thread(self.task_store.get, *key)
            owned = self.tasks._has_owner(task)
            if (task["state"] in TERMINAL_TASK_STATES
                    or (task["state"] == "blocked" and not owned)):
                break
            await asyncio.sleep(OBSERVE_SECONDS)
        while key in self.tasks._notifications:
            await asyncio.sleep(OBSERVE_SECONDS)
        notes = [n for n in await asyncio.to_thread(self.task_store.notifications,
                                                    session_id)
                 if n["task_id"] == task_id and n["kind"] in {"result", "blocked"}]
        messages = await asyncio.to_thread(self.task_store.messages, *key, 0,
                                           "subagent")
        results = [m for m in messages if m["kind"] == "result"]
        event_id = self.frozen["experiment"]["dedup_id"]
        verification = await self.verifier.verify(
            event_id, claimed=results[-1]["payload"]["text"] if results else None)
        originals = [o for o in self._operations() if o["kind"] == "mutation"
                     and o["phase"] == "original" and o["task_id"] == task_id]
        created = [o for o in originals if o["status"] == 200] or originals
        invocation = created[0]["invocation_id"] if created else None
        tool = self.tracker.tool(invocation) if invocation else None
        arguments = self.tracker.arguments(invocation) if invocation else None
        replay_event_id = replay_error = None
        if tool and arguments is not None and verification["action_result"] == "pass":
            try:
                replay_event_id = await self._replay(task_id, invocation, tool,
                                                     arguments)
            except Exception as error:  # noqa: BLE001 - recorded replay failure
                replay_error = type(error).__name__ + ": " + str(error)[:512]
        replay = await self.verifier.verify_replay(verification, replay_event_id)
        self.broker.close_gate(ACTION_ID, "outcome verification finished")
        self.relay.unbind("outcome verification finished")
        bundle = self._evaluation.verified.bundle
        module = (self._evaluation.tool_modules.get(tool) or "").removeprefix(
            "/opt/recollect-bundle/")
        baseline = {f.path: f.content for f in self.tree.files}
        changed = {f.path for f in bundle.candidate.files
                   if baseline.get(f.path) != f.content}
        attribution = attribute(
            self._operations(), verified_event_id=event_id, action_id=ACTION_ID,
            task_id=task_id, serving_digest=bundle.digest,
            routing_epoch=self.router.epoch,
            invocation_modules=({invocation: module, "replay:" + invocation: module}
                                if invocation else {}),
            candidate_modules=changed, sealed_after_ns=self._sealed_after_ns)
        overlap, span, intervals = await self._concurrency()
        identities = await self.current_identities()
        link = verification.get("html_link")
        checks = {
            "original_task_completed": task["state"] == "completed" and bool(results),
            "event_fields_verified": (verification["observed"] == "exactly_one_correct"
                                      and verification.get("mismatches") == []),
            "exactly_one_event": (verification["observed"] == "exactly_one_correct"
                                  and replay["after_replay"]["observed"]
                                  == "exactly_one_correct"),
            "same_identity_replay": replay["result"] == "pass",
            "generated_code_attribution": attribution["attributed"],
            "unchanged_authorization": (identities["calendar"]
                                        == self.frozen["calendar"]),
            "main_reply_captured": bool(notes and link and link in notes[-1]["text"]),
            "concurrency_passed": overlap >= REQUIRED_OVERLAP_NS,
            "responsiveness_recorded": (set(probes) == set(PROBE_POINTS) and all(
                p is not None and p["correct"] for p in probes.values())),
        }
        self._event_id = event_id
        workload = self._receipts.get("workload")
        files = {
            "outcome/continuation-task.json": encode(task),
            "outcome/messages.json": encode({"items": messages}),
            "outcome/main-reply.json": encode({"items": notes}),
            "outcome/verification.json": encode(verification),
            "outcome/replay.json": encode({**replay, "error": replay_error,
                                           "tool": tool, "arguments": arguments}),
            "outcome/attribution.json": encode(attribution),
            "outcome/provider-operations.json": encode({"items": self._operations()}),
            "outcome/probes.json": encode(probes),
            "outcome/concurrency.json": encode({
                "overlap_ns": overlap, "span": span, "intervals": intervals,
                "samples": [{"monotonic_ns": t, "slots": v}
                            for t, v in self._slot_samples]}),
            "outcome/identities.json": encode(identities),
        }
        if workload is not None:
            files["outcome/workload-task.json"] = encode(await asyncio.to_thread(
                self.task_store.get, workload["session_id"], workload["task_id"]))
        return checks, _snapshot(files)

    async def _concurrency(self):
        if self._sampler is not None:
            self._sampler_stop.set()
            await self._sampler
        samples = self._slot_samples
        if not samples:
            return 0, None, {}
        window = (samples[0][0], samples[-1][0])
        intervals = {lane: generating_intervals(samples, slot, window)
                     for lane, slot in (("worker", WORKER_SLOT),
                                        ("modifier", MODIFIER_SLOT))}
        overlap, span = longest_overlap(list(intervals.values()))
        return overlap, span, intervals

    # -- CP6 ---------------------------------------------------------------------

    async def finish(self, failure):
        files, errors = {}, []

        async def attempt(name, operation):
            try:
                result = operation()
                if asyncio.iscoroutine(result):
                    result = await result
                return result
            except Exception as error:  # noqa: BLE001 - every cleanup error is evidence
                errors.append({"step": name, "error_type": type(error).__name__,
                               "error": str(error)[:1024]})
                return None

        if self.started:
            await attempt("close_gate", lambda: self.broker.close_gate(
                ACTION_ID, "trial finished"))
            await attempt("unbind_relay", lambda: self.relay.unbind("trial finished"))
            if self.router._activation is not None and not self.router._committed:
                await attempt("rollback", lambda: self.router.rollback(
                    "trial_stopped_before_cp4_seal"))
            if self._event_id is not None and self._event_id in self.verifier.passed:
                receipt = await attempt("cleanup", lambda: cleanup(
                    self.broker, self.broker.issue("cleanup"), self.verifier,
                    self._event_id))
                files["cleanup/receipt.json"] = encode({"receipt": receipt})
            if self._sampler is not None and not self._sampler.done():
                self._sampler_stop.set()
                await attempt("sampler", lambda: self._sampler)
            await attempt("tasks", self.tasks.close)
            for manager in (self.manager_a, self.manager_b):
                if manager is not None:
                    await attempt("sandbox", manager.close_all)
            await attempt("relay", self.relay.close)
            await attempt("ingress", self.ingress.close)
            await attempt("generator", self.generator.aclose)
            await attempt("broker", self.broker.aclose)
            await attempt("web_client", self.state.web_client.aclose)
            files["provider/journal.jsonl"] = b"".join(
                r.body for r in self.provider_journal.verify())
            files["routing/journal.jsonl"] = b"".join(
                r.body for r in self.routing_journal.verify())
            await attempt("provider_journal", self.provider_journal.close)
            await attempt("routing_journal", self.routing_journal.close)
        files["finish.json"] = encode({
            "failure_type": type(failure).__name__ if failure else None,
            "receipts": self._receipts, "errors": errors})
        return _snapshot(files)


async def _healthy(manager):
    try:
        await manager.ensure()
        return True
    except Exception:  # noqa: BLE001 - health is an observation, recorded by CP4
        return False


def _feedback(evaluation):
    """Rejected-candidate feedback: results only, never evaluator internals."""
    if evaluation is None:
        return None
    files = {f.path: f.content for f in evaluation.evidence.files}
    return {"checks": evaluation.checks, "scenarios": {
        path.split("/")[1]: json.loads(content)
        for path, content in files.items()
        if path.startswith("scenarios/") and path.endswith("/result.json")}}


def _snapshot(files):
    return Snapshot(tuple(File(path, content)
                          for path, content in sorted(files.items())))


__all__ = ["ExperimentState", "LiveSettings", "LiveTrialEnvironment"]
