"""Host-local capability boundary joining development, execution and checkpoints.

All objects here stay outside worker processes. authorize() is a privileged host
operation, not an RPC. Object identity authenticates local handoffs, not remote
people or models. Synchronous methods block; use them off the serving event loop.
"""

import asyncio
import base64
import uuid
from dataclasses import asdict, dataclass

from .containment import FixtureSpec
from .contracts import File, Snapshot
from .development import Binding, CheckResults, Development, Review, Stage
from .executor import Deadline, FixtureExecutor
from .journal import IntegrityError, decode, encode, read_archive, sha256


@dataclass(frozen=True)
class DevelopmentSettings:
    image_id: str
    image_environment: tuple[str, ...]
    entrypoint: str


@dataclass(frozen=True, eq=False)
class Grant:
    grant_id: str
    action: str
    actor_id: str
    binding: Binding | None
    controller_instance: str
    cycle_id: str
    stage: str
    generation: int


def _archive(records) -> Snapshot:
    return Snapshot(tuple(
        file
        for record in records
        for file in (
            File(f"records/{record.anchor.sequence}.json", record.body),
            *(File(f"records/{record.anchor.sequence}/{f.path}", f.content)
              for f in record.files.files),
        )
    ))


async def _offload(function, *args):
    task = asyncio.create_task(asyncio.to_thread(function, *args))
    try:
        return await asyncio.shield(task)
    except asyncio.CancelledError:
        await FixtureExecutor._settle(task)
        raise


async def _finalize(function, *args):
    """Settle cleanup without hiding its exception behind repeated cancellation."""
    task = asyncio.create_task(asyncio.to_thread(function, *args))
    cancelled = False
    while not task.done():
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError:
            cancelled = True
        except BaseException:
            break
    try:
        task.result()
    except BaseException as exc:
        return exc, cancelled
    return None, cancelled


class _ReleaseFence:
    def __init__(self, owner, runtime):
        self.owner, self.runtime = owner, runtime

    def __getattr__(self, name):
        return getattr(self.runtime, name)

    def release(self, worker, record, deadline):
        with self.owner._controller._development_operation(self.owner):
            if not self.owner._busy:
                raise IntegrityError("Execution authority no longer active")
            self.runtime.release(worker, record, deadline)


class IntegratedDevelopment:
    def __init__(self, controller, baseline, policy, settings, cp1, started, deadline):
        self._controller = controller
        self._baseline, self._policy, self._settings = baseline, policy, settings
        self._started, self._deadline = started, deadline
        self._id = uuid.uuid4().hex
        self._actors = {role: uuid.uuid4().hex for role in (
            "author", "forward_reviewer", "checker", "code_reviewer", "submitter"
        )}
        self._development = Development(
            attempt_id=controller.config.attempt_id, instance_id=self._id,
            author_id=self._actors["author"],
            forward_reviewer_id=self._actors["forward_reviewer"],
            code_reviewer_id=self._actors["code_reviewer"],
            contract=controller.config.contract, policy=policy, baseline=baseline,
            verified_cp1_sha256=cp1,
            original_started_at=started.monotonic_ns / 1e9,
            deadline=None,
            clock=lambda: controller._now().monotonic_ns / 1e9,
        )
        self._grants = {}
        self._busy = False
        self._lease = None
        self._driver = None
        self._driver_used = False
        self._generation = 0
        self._event_cursor = 0
        self._executor = None
        self._receipt = None
        self._receipt_binding = None
        self._receipt_source_sha256 = None
        self._role_context = None
        self._role_model = None
        # The last applied execute reply, keyed by the plan it implemented.
        self._previous_edits = None
        self._pending_edits = None
        # Validate the frozen execution profile before issuing any capability.
        self._spec(uuid.uuid4().hex, self._initial_binding(controller))
        controller._emit("development_opened", {
            "cycle_id": self._id, "cp1_sha256": cp1,
            "actors": self._actors, "settings": asdict(settings),
            "policy_sha256": policy.sha256, "baseline_sha256": baseline.sha256,
            "original_started": asdict(started), "deadline_ns": deadline,
        })

    def _initial_binding(self, controller):
        return Binding(controller.config.attempt_id, self._id, 1,
                       controller.config.contract.sha256, self._baseline.sha256,
                       "0" * 64, None)

    def _spec(self, run_id, binding):
        return FixtureSpec(
            run_id, self._settings.image_id, self._settings.image_environment,
            self._baseline, self._policy, self._settings.entrypoint,
            None, binding,
        )

    @property
    def stage(self):
        return self._development.stage

    @property
    def binding(self):
        return self._development.binding

    def _role(self, action):
        if action == "plan":
            return "author"
        if action == "execute" and self.stage in {Stage.IMPLEMENT, Stage.CHECKS,
                                                  Stage.CODE_REVIEW, Stage.READY}:
            return "author"
        if action == "review" and self.stage == Stage.FORWARD_REVIEW:
            return "forward_reviewer"
        if action == "review" and self.stage == Stage.CODE_REVIEW:
            return "code_reviewer"
        if action == "checks" and self.stage == Stage.CHECKS:
            return "checker"
        if action == "submit" and self.stage == Stage.READY:
            return "submitter"
        raise IntegrityError("Action is not allowed at this development stage")

    def authorize(self, action: str, *, _driver=None) -> Grant:
        """Trusted host mints one handoff; workers must never call this method."""
        with self._controller._development_operation(self):
            if self._driver is not _driver:
                raise IntegrityError("Development driver owns the handoffs")
            if self._busy or self._grants:
                raise IntegrityError("A development handoff is already outstanding")
            role = self._role(action)
            binding = None if self.stage == Stage.PLAN else self.binding
            grant = Grant(
                uuid.uuid4().hex, action, self._actors[role], binding,
                self._controller._instance, self._id, self.stage, self._generation,
            )
            self._controller._emit("development_authorized", {
                "cycle_id": self._id, "grant": asdict(grant),
            })
            self._grants[grant.grant_id] = grant
            return grant

    def _claim_driver(self, owner, settings):
        with self._controller._development_operation(self):
            if (self._driver_used or self._busy or self._grants
                    or self.stage != Stage.PLAN):
                raise IntegrityError("Driver requires an unclaimed fresh development")
            with self._controller._development_lease_lock:
                self._driver = owner
                self._driver_used = True
                self._controller._development_pending = True
            self._driver_profile = settings.identity
            self._controller._emit("development_driver", {
                "cycle_id": self._id, "state": "claimed",
                "runner_sha256": self._driver_profile,
            })

    def _next_driver_grant(self, owner, settings):
        with self._controller._development_operation(self):
            if (self._driver is not owner or self._busy or self._grants
                    or settings.identity != self._driver_profile):
                raise IntegrityError("Driver ownership or profile changed")
            if self.stage == Stage.READY:
                return None
            event = self._development.events[-1]
            if self.stage == Stage.FORWARD_REVIEW:
                action = ("plan" if event.kind == "forward_review"
                          and event.detail == "rejected" else "review")
            elif self.stage == Stage.CODE_REVIEW:
                action = ("execute" if event.kind == "code_review"
                          and event.detail == "rejected" else "review")
            else:
                action = {Stage.PLAN: "plan", Stage.IMPLEMENT: "execute",
                          Stage.CHECKS: "checks"}.get(self.stage)
            if action is None:
                raise IntegrityError("No valid development continuation")
            grant = self.authorize(action, _driver=owner)
            self._controller._emit("development_driver", {
                "cycle_id": self._id, "state": "handoff", "action": action,
                "grant_id": grant.grant_id, "generation": self._generation,
                "after_event": event.sequence,
            })
            return grant

    def _finish_driver(self, owner, error):
        controller = self._controller
        with controller._lock:
            if self._driver is not owner:
                return
            if error is not None:
                controller._abort("development_driver_failed:" + type(error).__name__)
                controller.journal.append("development_driver", {
                    "cycle_id": self._id, "state": "failed",
                    "error_type": type(error).__name__, "error": str(error)[:2048],
                })
                return
            with controller._development_operation(self):
                artifact = self._development.candidate(self._id)
                if (self._busy or self._grants or self._receipt is None
                        or self._receipt.diagnostic_only
                        or self._receipt_binding != self.binding
                        or self._receipt_source_sha256 != artifact.sha256):
                    raise IntegrityError("Driver has no settled primary candidate")
                controller._emit("development_driver", {
                    "cycle_id": self._id, "state": "ready",
                    "binding": asdict(self.binding),
                })

    async def run_until_ready(self, settings, runtime_factory, *, model_factory=None,
                              native_executor=None):
        """Drive one fresh cycle to READY, never submit or retry terminal failures.

        Factories are trusted host constructors: they must not dispatch work.
        Every role still executes through the owned, independently checked runner.
        With native_executor(grant), execute grants use the owned native modifier
        instead; checks and reviews still run against its exact captured candidate.
        """
        from .roles import LocalRoleModel

        owner, error = object(), None
        try:
            await _offload(self._claim_driver, owner, settings)
            while True:
                grant = await _offload(self._next_driver_grant, owner, settings)
                if grant is None:
                    break
                if grant.action == "execute" and native_executor is not None:
                    await native_executor(grant)
                    continue
                runtime = await _offload(runtime_factory)
                model = (await _offload(model_factory or LocalRoleModel, settings)
                         if grant.action != "checks" else None)
                await self.run_role(grant, settings, runtime, model=model)
        except BaseException as exc:
            error = exc
        final_error, cancelled = await _finalize(self._finish_driver, owner, error)
        if final_error is not None or (cancelled and error is None):
            error = final_error or asyncio.CancelledError()
            accounting_error, _ = await _finalize(self._finish_driver, owner, error)
            if accounting_error is not None:
                error = IntegrityError("Driver failure accounting unconfirmed")
                error.__cause__ = accounting_error
        # No await between releasing ownership and delivering readiness/error.
        with self._controller._development_lease_lock:
            revoked = (self._driver is owner and error is None
                       and not self._controller._eligible)
            if self._driver is owner and not revoked:
                self._driver = None
                self._controller._development_pending = self._busy
        if revoked:
            error = IntegrityError("Driver eligibility revoked before delivery")
            accounting_error, _ = await _finalize(self._finish_driver, owner, error)
            if accounting_error is not None:
                error = IntegrityError("Driver failure accounting unconfirmed")
                error.__cause__ = accounting_error
            with self._controller._development_lease_lock:
                if self._driver is owner:
                    self._driver = None
                    self._controller._development_pending = self._busy
        if error is not None:
            raise error

    def _consume(self, grant, action):
        if (
            not isinstance(grant, Grant)
            or self._grants.get(grant.grant_id) is not grant
            or grant.action != action or self._busy
            or grant.actor_id != self._actors[self._role(action)]
            or grant.binding != (None if self.stage == Stage.PLAN else self.binding)
            or grant.controller_instance != self._controller._instance
            or grant.cycle_id != self._id or grant.stage != self.stage
            or grant.generation != self._generation
        ):
            raise IntegrityError("Foreign, replayed or stale development grant")
        del self._grants[grant.grant_id]
        self._controller._emit("development_consumed", {
            "cycle_id": self._id, "grant": asdict(grant),
        })
        if action in {"plan", "execute", "review"}:
            review = action == "review"
            field = "_development_reviews" if review else "_development_updates"
            count = getattr(self._controller, field) + 1
            setattr(self._controller, field, count)

    def _input(self, kind, report, evidence, *, role_report=None):
        if not evidence.files or report.evidence_sha256 != evidence.sha256:
            raise IntegrityError("Report evidence bytes do not match its digest")
        self._controller._emit("development_input", {
            "cycle_id": self._id, "kind": kind, "report": asdict(report),
            "evidence_sha256": evidence.sha256,
            **({"role_report": role_report} if role_report is not None else {}),
        }, evidence)

    def _transition(self):
        self._generation += 1
        events = self._development.events
        self._controller._emit("development_transition", {
            "cycle_id": self._id, "stage": self.stage,
            "events": [asdict(e) for e in events[self._event_cursor:]],
        })
        self._event_cursor = len(events)

    def propose(self, grant: Grant, plan):
        with self._controller._development_operation(self):
            self._consume(grant, "plan")
            self._controller._emit("development_input", {
                "cycle_id": self._id, "kind": "plan", "plan_sha256": plan.sha256,
            }, Snapshot((File("plan.json", encode(asdict(plan))),)))
            self._development.propose(self._id, plan)
            self._receipt = self._receipt_binding = None
            self._transition()

    def review(self, grant: Grant, report, evidence: Snapshot):
        with self._controller._development_operation(self):
            self._consume(grant, "review")
            if report.reviewer_id != grant.actor_id:
                raise IntegrityError("Report actor differs from authorized reviewer")
            self._input("review", report, evidence)
            self._development.review(self._id, report)
            self._transition()

    def checks(self, grant: Grant, report, evidence: Snapshot):
        with self._controller._development_operation(self):
            self._consume(grant, "checks")
            self._input("checks", report, evidence)
            self._development.checks(self._id, report)
            self._transition()

    def _prepare_execution(self, grant, lease):
        with self._controller._development_operation(self):
            self._consume(grant, "execute")
            with self._controller._development_lease_lock:
                self._lease = lease
                self._busy = self._controller._development_pending = True
            self._executor = None
            self._role_context = self._role_model = None
            spec = self._spec(uuid.uuid4().hex, self.binding)
            self._controller._emit("development_execution", {
                "cycle_id": self._id, "state": "claimed", "run_id": spec.run_id,
                "binding": asdict(spec.binding), "spec_sha256": spec.sha256,
            })
            self._executor = FixtureExecutor.create(
                self._controller.journal.root / ("executor-" + spec.run_id), spec,
                original_started=self._started, deadline_ns=self._deadline,
                max_refreshes=0, clock=self._controller._clock,
                fault=self._controller.journal.fault,
            )

    def _finish_execution(self, receipt):
        with self._controller._development_operation(self):
            executor = self._executor
            if executor.spec.binding != self.binding:
                raise IntegrityError("Executor belongs to a different revision")
            records = executor.verified_receipt(receipt)
            self._controller._emit("development_execution", {
                "cycle_id": self._id, "state": "verified",
                "spec_sha256": executor.spec.sha256,
                "receipt": {"run_id": receipt.run_id,
                            "anchor": asdict(receipt.archive_anchor),
                            "snapshot_sha256": receipt.snapshot.sha256},
            }, _archive(records))
            self._development.implementation(self._id, receipt.snapshot)
            self._receipt, self._receipt_binding = receipt, self.binding
            self._receipt_source_sha256 = receipt.snapshot.sha256
            self._transition()

    def _prepare_role(self, grant, settings, model, lease):
        from .roles import make_context, model_payload, render_message, role_spec

        with self._controller._development_operation(self):
            if grant.action not in {"plan", "review", "execute", "checks"}:
                raise IntegrityError("Submission is deterministic, not a model role")
            identity = settings.identity
            controller = self._controller
            if controller._role_settings_sha256 not in (None, identity):
                raise IntegrityError("Runner profile changed within the attempt")
            if tuple(f.path[:-3] for f in settings.checks) != (
                controller.config.contract.development_checks
            ):
                raise IntegrityError("Check executables differ from frozen inventory")
            self._consume(grant, grant.action)
            with controller._development_lease_lock:
                self._lease = lease
                self._busy = controller._development_pending = True
            self._executor = None
            self._role_model = model
            self._role_context = None
            self._role_settings = settings
            self._role_grant = grant
            controller._role_settings_sha256 = identity
            now = controller._now()
            self._role_deadline = Deadline(None, now.boot_id)
            context = make_context(self, grant, settings)
            self._role_context = encode(context)
            if len(self._role_context) > 128 * 1024:
                raise IntegrityError("Role context exceeds frozen byte bound")
            # Reject unsupported source/check envelopes before spending inference.
            preview = role_spec(self, context, b"{}\n", settings, None)
            if sum(len(f.content) for f in preview.baseline.files) + 128 * 1024 > (
                2 * 1024 * 1024
            ):
                raise IntegrityError("Role envelope cannot reserve its reply budget")
            if grant.action != "checks":
                if model.settings != settings:
                    raise IntegrityError("Model broker configuration mismatch")
                controller._role_model_calls += 1
            controller._emit("development_role", {
                "cycle_id": self._id, "state": "reserved", "grant": asdict(grant),
                "request_sha256": sha256(self._role_context),
                "runner_sha256": identity, "deadline": asdict(self._role_deadline),
                "model_calls_reserved": controller._role_model_calls,
            }, Snapshot((File("role-request.json", self._role_context),)))
            self._role_guard()
            message = render_message(self, context, settings) if model else None
            return (model_payload(context, settings, message) if model else None,
                    self._role_deadline)

    def _role_guard(self):
        now = self._controller._now()
        if (
            not self._busy or self._role_context is None
            or now.boot_id != self._role_deadline.boot_id
            or self._role_deadline.monotonic_ns is not None
            or self._role_settings.identity != (
                decode(self._role_context)["runner_sha256"]
            )
        ):
            raise IntegrityError("Role deadline, profile or ownership changed")
        return now

    def _start_role_executor(self, reply):
        from .roles import role_spec

        with self._controller._development_operation(self):
            self._role_guard()
            evidence = (self._role_model.evidence() if self._role_model
                        else Snapshot(()))
            self._controller._emit("development_role", {
                "cycle_id": self._id, "state": "inference_settled",
                "request_sha256": sha256(self._role_context),
                "model_response_complete": bool(self._role_model and
                                                self._role_model.response_complete),
                "local_model_closed": bool(self._role_model and
                                           self._role_model.local_closed),
                "upstream_termination_confirmed": False,
            }, evidence)
            context = decode(self._role_context)
            self._pending_edits = (decode(reply) if context["role"] == "execute"
                                   else None)
            self._role_spec = role_spec(
                self, context, reply, self._role_settings, None,
            )
            self._executor = FixtureExecutor.create(
                self._controller.journal.root / ("executor-" + self._role_spec.run_id),
                self._role_spec, original_started=self._started,
                deadline_ns=self._role_deadline.monotonic_ns, max_refreshes=0,
                clock=self._controller._clock, fault=self._controller.journal.fault,
            )

    def _finish_role(self, receipt):
        from .roles import (
            InvalidRoleReply,
            check_result,
            extract_source,
            plan_result,
            review_result,
        )

        with self._controller._development_operation(self):
            self._role_guard()
            context = decode(self._role_context)
            if self._executor.spec != self._role_spec:
                raise IntegrityError("Role executor binding changed")
            records = self._executor.verified_receipt(receipt)
            reports = [r for r in records if r.value["kind"] == "report_collected"]
            if len(reports) != 1:
                raise IntegrityError("Ambiguous role result channel")
            outer = decode(next(f.content for f in reports[0].files.files
                                if f.path == "report.json"))
            report = decode(base64.b64decode(outer["stdout"], validate=True))
            if (
                set(report) != {"request_id", "role", "result"}
                or report["request_id"] != context["request_id"]
                or report["role"] != context["role"]
            ):
                raise IntegrityError("Foreign or stale role report")
            evidence = _archive(records)
            source = extract_source(receipt.snapshot)
            self._controller._emit("development_role", {
                "cycle_id": self._id, "state": "verified",
                "request_sha256": sha256(self._role_context),
                "spec_sha256": self._role_spec.sha256,
                "development_binding": context["binding"],
                "receipt": asdict(receipt.archive_anchor),
                "envelope_snapshot_sha256": receipt.snapshot.sha256,
                "source_snapshot_sha256": source.sha256,
                "result": report["result"],
            }, evidence)
            value = report["result"]
            action = context["role"]
            try:
                if action == "plan":
                    plan = plan_result(value, self._controller.config.contract,
                                       self._policy)
                    self._controller._emit("development_input", {
                        "cycle_id": self._id, "kind": "plan",
                        "plan_sha256": plan.sha256,
                    }, Snapshot((File("plan.json", encode(asdict(plan))),)))
                    self._development.propose(self._id, plan)
                    self._receipt = self._receipt_binding = None
                    self._receipt_source_sha256 = None
                elif action == "execute":
                    if type(value) is dict and set(value) == {"invalid"}:
                        raise InvalidRoleReply(value["invalid"])
                    self._development.implementation(self._id, source)
                    # Preserve the raw envelope receipt. This separately recorded map
                    # identifies the candidate extracted and reconstructed by the host.
                    self._receipt, self._receipt_binding = receipt, self.binding
                    self._receipt_source_sha256 = source.sha256
                    self._previous_edits = (self._development._plan.sha256,
                                            self._pending_edits)
                elif action == "review":
                    blockers = review_result(value, context)
                    review = Review(self.binding, self._role_grant.actor_id,
                                    value["approved"], blockers,
                                    evidence.sha256)
                    self._input("review", review, evidence, role_report=value)
                    self._development.review(self._id, review)
                else:
                    checks = CheckResults(self.binding, check_result(value,
                                          self._role_settings), evidence.sha256)
                    self._input("checks", checks, evidence, role_report=value)
                    self._development.checks(self._id, checks)
            except InvalidRoleReply as error:
                # The same step retries with this as history.
                self._controller._emit("development_input", {
                    "cycle_id": self._id, "kind": "invalid",
                    "role": context["stage"] if action == "review" else action,
                    "error": str(error)[:2048],
                })
            self._transition()
            self._role_guard()

    async def run_role(self, grant, settings, runtime, *, model=None):
        """Host-owned one-shot actor; no worker RPC or raw receipt import."""
        from .roles import InvalidRoleReply, LocalRoleModel

        lease, error = object(), None
        try:
            if grant.action == "checks":
                if model is not None:
                    raise IntegrityError("Development checks cannot be model judgments")
            else:
                model = model or LocalRoleModel(settings)
            payload, deadline = await _offload(
                self._prepare_role, grant, settings, model, lease,
            )
            reply = b"{}\n"
            if model is not None:
                # Reserve and recheck before the outbound request, not afterward.
                await _offload(self._role_dispatch)
                inference = asyncio.create_task(model.complete(
                    payload, deadline, clock=self._controller._clock,
                ))
                try:
                    reply, _ = await asyncio.shield(inference)
                except InvalidRoleReply as invalid:
                    # A cut-off or unparsable reply goes back to the step.
                    reply = encode({"invalid": str(invalid)[:2048]})
                except asyncio.CancelledError:
                    inference.cancel()
                    # Exactly one cancellation reaches HTTP cleanup. Further
                    # caller cancellations cannot release the role's busy lease.
                    await FixtureExecutor._settle(inference)
                    raise
            await _offload(self._start_role_executor, reply)
            receipt = await self._executor.run_async(_ReleaseFence(self, runtime))
            await _offload(self._finish_role, receipt)
        except BaseException as exc:
            error = exc
        await self._settle_execution(error, lease)

    def _role_dispatch(self):
        with self._controller._development_operation(self):
            self._role_guard()
            self._controller._emit("development_role", {
                "cycle_id": self._id, "state": "inference_dispatched",
                "request_sha256": sha256(self._role_context),
                "upstream_termination_confirmed": False,
            })

    def _execution_failed(self, error, lease):
        controller = self._controller
        with controller._lock:
            if controller._phase == "closed":
                return
            controller._abort("development_execution_failed:" + type(error).__name__)
            files = Snapshot(())
            details = {"cycle_id": self._id, "error_type": type(error).__name__,
                       "error": str(error)[:2048], "capture_complete": False,
                       "termination_confirmed": False}
            if self._role_context is not None and self._lease is lease:
                details["role_request_sha256"] = sha256(self._role_context)
                details["upstream_termination_confirmed"] = False
                model_files = (self._role_model.evidence() if self._role_model
                               else Snapshot(()))
                controller.journal.append("development_role", {
                    "cycle_id": self._id, "state": "failed",
                    "request_sha256": sha256(self._role_context),
                    "upstream_termination_confirmed": False,
                    "local_model_closed": bool(self._role_model and
                                               self._role_model.local_closed),
                }, model_files)
            if self._executor is not None and self._lease is lease:
                details["run_id"] = self._executor.spec.run_id
                details["last_anchor"] = asdict(self._executor.journal.head)
                try:
                    records = read_archive(self._executor.journal.root,
                                           self._executor.journal.head)
                    files = _archive(records)
                    details["capture_complete"] = True
                    details["termination_confirmed"] = any(
                        r.value["kind"] == "termination_verified" for r in records
                    )
                except Exception as exc:
                    details["archive_error"] = str(exc)[:2048]
            # A damaged clock must not discard independently captured evidence.
            controller.journal.append("development_failure", details, files)

    async def execute(self, grant: Grant, runtime):
        """Run only through the owned executor; never accept an injected receipt."""
        lease = object()
        error = None
        try:
            await _offload(self._prepare_execution, grant, lease)
            receipt = await self._executor.run_async(_ReleaseFence(self, runtime))
            await _offload(self._finish_execution, receipt)
        except BaseException as exc:
            error = exc
        await self._settle_execution(error, lease)

    def admit_native(self, grant: Grant, *, model, context_limit, output_limit,
                     base_url, slot=None):
        """Reserve native authority, not a candidate or a checkpoint receipt.

        Candidate acceptance requires the owned adapter's handoff, containment
        stop, final capture and upstream settlement. ``slot`` pins the modifier
        lane on a three-slot model server.
        """
        from .native_admission import NativeAdmission

        return NativeAdmission(
            self, grant, model=model, context_limit=context_limit,
            output_limit=output_limit, base_url=base_url, slot=slot,
        )

    async def execute_native(self, grant: Grant, runtime_factory, *, prompt, model,
                             context_limit, output_limit, base_url, slot=None):
        """One owned native modifier turn: admit, prompt, capture handoff, settle.

        runtime_factory(admission) must inertly construct a trusted adapter such as
        NativeRuntime. Native prose is never a result; only the verified capture is
        handed to checks and review. Every path, including failure, awaits close().
        """
        admission = await _offload(lambda: self.admit_native(
            grant, model=model, context_limit=context_limit,
            output_limit=output_limit, base_url=base_url, slot=slot,
        ))
        error = None
        try:
            runtime = await _offload(admission.start, runtime_factory)
            await _offload(admission.release)
            await runtime.started()
            await runtime.prompt(prompt)
            await admission.handoff()
        except BaseException as exc:
            error = exc
        try:
            await admission.close()
        except BaseException as exc:
            if error is None:
                error = exc
            else:
                error.__context__ = exc
        if error is not None:
            raise error

    async def _settle_execution(self, error, lease):
        if error is not None:
            accounting_error, _ = await _finalize(self._execution_failed, error, lease)
            if accounting_error is not None:
                error = IntegrityError("Development failure accounting unconfirmed")
                error.__cause__ = accounting_error
        close_error, cancelled = await _finalize(self._close_execution, lease)
        if close_error is not None or (cancelled and error is None):
            cause = close_error or asyncio.CancelledError()
            accounting_error, _ = await _finalize(self._execution_failed, cause, lease)
            if accounting_error is not None:
                error = IntegrityError("Development failure accounting unconfirmed")
                error.__cause__ = accounting_error
            elif error is None:
                error = cause
        # No await or I/O between releasing the lease and delivering the result.
        # A cancellation observed at any preceding await has already been accounted.
        # This leaf lock covers assignments only, never journal I/O or awaits.
        # A different loop/thread must not claim between the two flag writes.
        with self._controller._development_lease_lock:
            if self._lease is lease:
                self._busy = False
                self._controller._development_pending = self._driver is not None
        if error is not None:
            raise error

    def _close_execution(self, lease):
        with self._controller._lock:
            if self._lease is lease and self._executor is not None:
                self._executor.close()
                if self._role_context is not None:
                    self._role_guard()

    def submit(self, grant: Grant):
        with self._controller._development_operation(self):
            self._consume(grant, "submit")
            artifact = self._development.candidate(self._id)
            if (
                self._receipt is None or self._receipt.diagnostic_only
                or self._receipt_binding != self.binding
                or self._receipt_source_sha256 != artifact.sha256
            ):
                raise IntegrityError("No primary executor receipt for this revision")
            records = self._controller.journal.verify()
            evidence = _archive(tuple(r for r in records if (
                r.value["kind"].startswith("development_")
                and r.value["data"].get("cycle_id") == self._id
            )))
            self._controller._emit("development_submission", {
                "cycle_id": self._id, "binding": asdict(self.binding),
                "evidence_sha256": evidence.sha256,
                "executor_anchor": asdict(self._receipt.archive_anchor),
            })
            authorization = object()
            self._controller._development_submission = (
                authorization, self, self._id, artifact.sha256, evidence.sha256,
            )
            return self._controller.submit(
                self._id, artifact, evidence, _development=self,
                _authorization=authorization,
            )
