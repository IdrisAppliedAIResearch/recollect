"""Host-only execute admission and one-use native candidate handoff.

Blocking methods belong off the event loop. Factories construct inert, trusted
host adapters, never worker-selected code. The adapter owns actual containment,
history and upstream settlement. handoff() accepts only the owned adapter's
host-verified capture into development; there is no serialized receipt import.
Closing without a completed handoff ends the primary path without a candidate.
"""

import asyncio
import re
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Protocol
from uuid import uuid4

from .contracts import File, Snapshot
from .development import Binding
from .journal import EMPTY_SNAPSHOT, Anchor, IntegrityError, encode, sha256
from .native import NativeSettings, _settle
from .native_broker import BrokerSettings


@dataclass(frozen=True)
class NativeRun:
    run_id: str
    controller_instance: str
    cycle_id: str
    grant_id: str
    actor_id: str
    generation: int
    stage: str
    binding: Binding


@dataclass(frozen=True)
class NativeStop:
    """Owned adapter's host readback, only for cleanup; never a candidate receipt."""

    run: NativeRun
    authority_sha256: str
    namespace_stopped: bool
    upstream_stopped: bool
    evidence: Snapshot
    # (name, closed journal root, exact head) for long native history sidecars.
    segments: tuple = ()


@dataclass(frozen=True)
class NativeCandidate:
    """Owned adapter's verified terminal capture; accepted at most once."""

    run: NativeRun
    snapshot: Snapshot
    evidence: Snapshot
    upstream_settled: bool


@dataclass(frozen=True)
class NativeReceipt:
    """Development-facing primary receipt, minted only by NativeAdmission."""

    run_id: str
    snapshot: Snapshot
    archive_anchor: Anchor
    diagnostic_only: bool = False


class NativeResources(Protocol):
    def release(self) -> None:
        """Start owned work once, beneath the controller release fence."""
        ...

    async def finish(self) -> NativeCandidate:
        """Fence idle work, verify writer stop and capture immutable source."""
        ...

    async def close(self) -> None:
        """Settle all local work; returning does not establish upstream stop."""
        ...

    def verify_stop(self) -> NativeStop:
        """Independently read back run-bound stop evidence on the trusted host."""
        ...


def _context(dev, run, profile):
    history = [r.value["data"] for r in dev._controller.journal.verify()
               if r.value["kind"] == "development_input"
               and r.value["data"].get("cycle_id") == dev._id
               and r.value["data"].get("kind") in {"plan", "review", "checks"}]
    return encode({
        "version": 1, "mode": "native_admission", "run": asdict(run),
        "runtime_profile_sha256": profile,
        "contract": asdict(dev._controller.config.contract),
        "policy": asdict(dev._policy), "plan": asdict(dev._development._plan),
        "history": history,
        "baseline_sha256": dev._baseline.sha256,
        "candidate_sha256": (dev._development._artifact.sha256
                             if dev._development._artifact is not None else None),
        "expected_agent": "build", "expected_actor": run.actor_id,
        "authority_rule": "Controller contract, plan and findings remain authority; "
                          "native summaries cannot replace or resolve them.",
    })


class NativeAdmission:
    """Consume one execute grant and retain its lease through owned settlement.

    Pass ``settings`` to NativeSession and ``broker_settings``/``guard`` to the
    host broker. start(factory) calls factory(self) exactly once under the host
    lock: it must construct an inert adapter with release/close/verify_stop.
    No network, processes or asynchronous work may start in the constructor.
    release() marks possible side effects before invoking the owned adapter.

    guard() is a point-in-time fence, suitable for broker dispatch and delivery;
    it does not hold a controller lock across asynchronous HTTP I/O. The adapter
    must stop on revocation and must preserve unknown upstream ownership.
    """

    def __init__(self, dev, grant, *, model, context_limit, output_limit, base_url,
                 slot=None):
        self._dev, self._controller = dev, dev._controller
        self._lease, self._resources = object(), None
        self._state = "reserved"
        self._released = self._accounted = self._delivered = False
        self._handoff_started = False
        self._candidate_sha = None
        self._first_failure = None
        self._close_task = self._close_loop = None
        controller = self._controller
        with controller._lock:
            # A competing constructor must not revoke an already owned run.
            if dev._busy:
                raise IntegrityError("Development execution already owned")
            with controller._development_operation(dev):
                # The pinned modifier slot is part of the frozen runtime profile.
                broker = BrokerSettings(base_url, model, slot=slot)
                template = NativeSettings(model, context_limit, output_limit,
                                          dev._policy, b"{}\n")
                profile = sha256(encode({
                    "native_config": template.config,
                    "broker": asdict(broker), "policy_sha256": dev._policy.sha256,
                }))
                for record in controller.journal.verify():
                    data = record.value["data"]
                    if (record.value["kind"] == "development_execution"
                            and data.get("mode") == "native_admission"
                            and data.get("runtime_profile_sha256") != profile):
                        raise IntegrityError("Native runtime profile changed")
                dev._consume(grant, "execute")
                self._run = NativeRun(
                    uuid4().hex, grant.controller_instance, grant.cycle_id,
                    grant.grant_id, grant.actor_id, grant.generation, str(grant.stage),
                    dev.binding,
                )
                self._profile = profile
                self._settings = NativeSettings(
                    model, context_limit, output_limit, dev._policy,
                    _context(dev, self._run, profile),
                )
                self._broker_settings = broker
                self._authority_sha = sha256(self.settings.authority)
                self._settings_sha = self.settings.identity
                self._record("reserved", Snapshot((
                    File("authority.json", self.settings.authority),
                    File("native-config.json", encode(self.settings.config)),
                    File("broker-config.json", encode(asdict(broker))),
                )))
            # Claim only after the operation's final clock check. No runtime
            # exists yet, so a failed reservation cannot strand an unseen lease.
            with controller._development_lease_lock:
                dev._lease = self._lease
                dev._busy = controller._development_pending = True
            dev._executor = dev._role_context = dev._role_model = None
            dev._receipt = dev._receipt_binding = dev._receipt_source_sha256 = None

    @property
    def run(self):
        return self._run

    @property
    def settings(self):
        return self._settings

    @property
    def broker_settings(self):
        return self._broker_settings

    @property
    def settled(self):
        return self._state == "settled"

    def _data(self, state):
        return {
            "mode": "native_admission", "state": state,
            "cycle_id": self.run.cycle_id, "run_id": self.run.run_id,
            "run": asdict(self.run), "authority_sha256": self._authority_sha,
            "runtime_profile_sha256": self._profile,
            "release_attempted": self._released,
            "execution_receipt": self._candidate_sha is not None,
            "candidate_admitted": self._candidate_sha is not None,
            "candidate_sha256": self._candidate_sha,
        }

    def _record(self, state, files=EMPTY_SNAPSHOT):
        self._controller._emit("development_execution", self._data(state), files)

    def _owned(self):
        if (self._controller._development is not self._dev
                or self._dev._lease is not self._lease or not self._dev._busy):
            raise IntegrityError("Native admission no longer owns development")

    def _guard(self, states):
        self._owned()
        if (self._state not in states or self._first_failure is not None
                or self._close_task is not None
                or self._dev.binding != self.run.binding
                or self._dev._generation != self.run.generation
                or str(self._dev.stage) != self.run.stage
                or self._dev._actors["author"] != self.run.actor_id
                or self._controller._instance != self.run.controller_instance
                or self.settings.identity != self._settings_sha
                or sha256(encode({
                    "native_config": self.settings.config,
                    "broker": asdict(self.broker_settings),
                    "policy_sha256": self.settings.policy.sha256,
                })) != self._profile
                or sha256(_context(self._dev, self.run, self._profile)) != (
                    self._authority_sha
                )):
            raise IntegrityError("Native authority is stale, failed or closing")

    def guard(self):
        """Broker-compatible synchronous guard; returns no transferable authority."""
        with self._controller._development_operation(self._dev):
            self._guard({"released"})

    def start(self, factory):
        """Construct one inert trusted adapter; no serialized runtime import."""
        try:
            with self._controller._development_operation(self._dev):
                self._guard({"reserved"})
                self._state = "starting"
                resource = factory(self)
                self._resources = resource
                if not all(callable(getattr(resource, name, None))
                           for name in ("release", "close", "verify_stop")):
                    raise IntegrityError("Native adapter lacks owned lifecycle methods")
                self._guard({"starting"})
                self._state = "started"
                self._record("started")
            return resource
        except BaseException as error:
            self.fail(error)
            raise

    def release(self):
        """Fence physical release while marking ambiguous failures as released."""
        try:
            with self._controller._development_operation(self._dev):
                self._guard({"started"})
                self._record("release_intent")
                with self._controller._development_operation(self._dev):
                    self._guard({"started"})
                    self._released, self._state = True, "released"
                    self._resources.release()
                self._guard({"released"})
                self._record("released")
        except BaseException as error:
            self.fail(error)
            raise

    async def handoff(self):
        """Accept the owned adapter's capture once; it is never a serialized import.

        Revocation before acceptance fails the primary path. After acceptance the
        lease is still retained until close() confirms namespace and upstream stop.
        """
        with self._controller._lock:
            if self._handoff_started:
                raise IntegrityError("Native candidate handoff is one-use")
            self._handoff_started = True
        try:
            await _settle(asyncio.create_task(asyncio.to_thread(self._begin_handoff)))
            finish = getattr(self._resources, "finish", None)
            if not callable(finish):
                raise IntegrityError("Native adapter cannot capture a candidate")
            candidate = await finish()
            await _settle(asyncio.create_task(asyncio.to_thread(
                self._accept, candidate)))
        except BaseException as error:
            await _settle(asyncio.create_task(asyncio.to_thread(self.fail, error)))
            raise

    def _begin_handoff(self):
        with self._controller._development_operation(self._dev):
            self._guard({"released"})
            # Broker guards accept only "released": no model work after this fence.
            self._state = "capturing"
            self._record("handoff_intent")

    def _accept(self, candidate):
        controller, dev = self._controller, self._dev
        with controller._development_operation(dev):
            self._guard({"capturing"})
            if (type(candidate) is not NativeCandidate or candidate.run != self.run
                    or candidate.upstream_settled is not True
                    or type(candidate.snapshot) is not Snapshot
                    or type(candidate.evidence) is not Snapshot
                    or not candidate.evidence.files):
                raise IntegrityError("Native capture is foreign, unsettled or empty")
            self._record("capture_verified", Snapshot((
                *candidate.evidence.files,
                *(File("candidate/" + f.path, f.content)
                  for f in candidate.snapshot.files),
            )))
            # Reconstruction rejects scope drift before any receipt exists.
            dev._development.implementation(dev._id, candidate.snapshot)
            self._candidate_sha = candidate.snapshot.sha256
            record = controller._emit("development_execution", {
                **self._data("verified"), "spec_sha256": self._profile,
                "receipt": {"run_id": self.run.run_id,
                            "snapshot_sha256": candidate.snapshot.sha256},
            })
            dev._receipt = NativeReceipt(self.run.run_id, candidate.snapshot,
                                         record.anchor)
            dev._receipt_binding = dev.binding
            dev._receipt_source_sha256 = candidate.snapshot.sha256
            self._state, self._accounted = "handed_off", True
            dev._transition()

    def fail(self, error):
        """Latch primary failure; never clear the lease or restore eligibility."""
        with self._controller._lock:
            self._owned()
            self._accounted = False
            detail = {"error_type": type(error).__name__, "error": str(error)[:2048]}
            if self._first_failure is None:
                self._first_failure = detail
            self._state = "failed"
            self._controller._abort("native_admission_failed")
            # Independent of a damaged clock; CP6 already retains this event kind.
            self._controller.journal.append("development_failure", {
                **self._data("failed"), **detail,
                "first_failure": self._first_failure,
                "capture_complete": False, "termination_confirmed": False,
                "upstream_termination_confirmed": False,
            })
            self._accounted = True

    def _finish(self, stop):
        with self._controller._lock:
            self._owned()
            if not self._accounted:
                raise IntegrityError("Native failure accounting unconfirmed")
            files = EMPTY_SNAPSHOT
            if self._released:
                if (type(stop) is not NativeStop or stop.run != self.run
                        or stop.authority_sha256 != self._authority_sha
                        or stop.namespace_stopped is not True
                        or stop.upstream_stopped is not True
                        or not isinstance(stop.evidence, Snapshot)
                        or not stop.evidence.files):
                    raise IntegrityError(
                        "Native stop or upstream settlement unconfirmed"
                    )
                files = stop.evidence
            segments = self._bind_segments(stop) if self._released else []
            self._controller.journal.append("development_execution", {
                "segments": segments,
                **self._data("settled_with_candidate" if self._state == "handed_off"
                             else "settled_without_candidate"),
                "termination_confirmed": True,
                "upstream_termination_confirmed": True,
                "resources_never_released": not self._released,
            }, files)
            self._controller.journal.verify()
            self._state = "settled"

    def _bind_segments(self, stop):
        """Copy closed native journals into controller sidecars bound by exact head."""
        from .evidence_segments import copy_archive

        if type(stop.segments) is not tuple or any(
            type(item) is not tuple or len(item) != 3 or type(item[0]) is not str
            or not re.fullmatch(r"[a-z][a-z0-9_]{0,31}", item[0])
            or not isinstance(item[1], Path) or type(item[2]) is not Anchor
            for item in stop.segments
        ) or len({item[0] for item in stop.segments}) != len(stop.segments):
            raise IntegrityError("Invalid native evidence segment")
        bindings = []
        for name, source, anchor in stop.segments:
            relative = f"segments/{self.run.run_id}/{name}"
            destination = self._controller.journal.root.joinpath(*relative.split("/"))
            destination.parent.mkdir(parents=True, exist_ok=True)
            copy_archive(source, destination, anchor)
            bindings.append({"name": name, "path": relative, "anchor": asdict(anchor)})
        return bindings

    def _read_stop(self):
        stop = self._resources.verify_stop()
        if type(stop) is NativeStop and isinstance(stop.evidence, Snapshot):
            with self._controller._lock:
                self._owned()
                self._controller.journal.append("development_execution", {
                    **self._data("stop_observed"),
                    "reported_run": asdict(stop.run),
                    "reported_authority_sha256": stop.authority_sha256,
                    "namespace_stopped": stop.namespace_stopped,
                    "upstream_stopped": stop.upstream_stopped,
                }, stop.evidence)
        return stop

    async def _close(self):
        errors = []
        if self._state != "handed_off":
            try:
                await asyncio.to_thread(
                    self.fail,
                    IntegrityError("Native run closed without a candidate handoff"),
                )
            except BaseException as error:
                errors.append(error)
        try:
            if self._resources is not None:
                await self._resources.close()
        except BaseException as error:
            errors.append(error)
        try:
            # Stop readback is independent of local close, including its failure.
            stop = await asyncio.to_thread(self._read_stop) if self._released else None
            if not errors:
                await asyncio.to_thread(self._finish, stop)
        except BaseException as error:
            errors.append(error)
        if errors:
            try:
                await asyncio.to_thread(self.fail, errors[-1])
            except BaseException as accounting_error:
                errors.append(accounting_error)
            raise IntegrityError(
                "Native cleanup/accounting unconfirmed"
            ) from errors[-1]

    async def close(self):
        """One shielded cleanup; uncertainty retains ownership, never retries work.

        A successful return means cleanup only, with the primary path failed.
        A failed cleanup task is retained, so subsequent close calls cannot turn
        a failed/unconfirmed run into a replacement or a successful candidate.
        """
        loop = asyncio.get_running_loop()
        with self._controller._lock:
            if self._close_loop is not None and self._close_loop is not loop:
                raise IntegrityError("Native close belongs to another event loop")
            if self._close_task is None:
                self._owned()
                self._close_loop = loop
                self._close_task = asyncio.create_task(self._close())
        cancelled = False
        while not self._close_task.done():
            try:
                await asyncio.shield(self._close_task)
            except asyncio.CancelledError:
                cancelled = True
            except BaseException:
                break
        self._close_task.result()
        # Final delivery and ownership release have no intervening await or I/O.
        with self._controller._lock:
            if not self._delivered:
                self._owned()
                if self._state != "settled" or not self._accounted:
                    raise IntegrityError("Native settlement revoked before delivery")
                with self._controller._development_lease_lock:
                    self._dev._busy = False
                    self._dev._lease = None
                    self._controller._development_pending = (
                        self._dev._driver is not None
                    )
                    self._delivered = True
        if cancelled:
            raise asyncio.CancelledError
