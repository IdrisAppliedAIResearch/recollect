"""Explicitly simulated observations, never a target-capability rehearsal."""

from recollect.selfmod.contracts import File, Snapshot
from recollect.selfmod.controller import (
    ACTIVATION_CHECKS,
    BASELINE_CHECKS,
    OUTCOME_CHECKS,
    PREFLIGHT_CHECKS,
    Controller,
    ControllerConfig,
    Stamp,
)
from tests.test_selfmod_contracts import make_scope

EVIDENCE = Snapshot((File("fixture.txt", b"simulated host observation"),))
ARTIFACT = Snapshot((File("extension.py", b"nonexecutable fixture bytes"),))


class FakeClock:
    def __init__(self):
        self.ns = 1_000_000_000
        self.boot = "simulated-process"

    def __call__(self):
        return Stamp(self.ns, "2026-09-12T00:00:00+00:00", self.boot)


class Fault:
    def __init__(self):
        self.at = None
        self.seen = []
        self.callback = None

    def __call__(self, point):
        self.seen.append(point)
        if self.callback:
            self.callback(point)
        if point == self.at:
            self.at = None
            raise OSError("injected: " + point)


def config():
    baseline, _, contract, _, _ = make_scope()
    return ControllerConfig(
        "simulated-attempt",
        contract,
        tuple(
            (name, str(i) * 64)
            for i, name in enumerate(
                ("protocol", "checkpoints", "amendment", "runtime",
                 "timing_amendment"), 1
            )
        ),
        baseline.sha256,
        "5" * 64,
        ("fixture", "regression"),
    )


def good(names):
    return dict.fromkeys(names, True)


def create(root, clock=None, fault=None):
    return Controller.create(
        root, config(), clock=clock or FakeClock(), fault=fault or Fault()
    )


def through_baseline(controller):
    controller.preflight(good(PREFLIGHT_CHECKS), EVIDENCE)
    controller.receive_request()
    controller.dispatch("baseline")
    controller.baseline("unsupported_and_reported", good(BASELINE_CHECKS), EVIDENCE)


def submit(controller, number=1, artifact=ARTIFACT):
    controller.dispatch("modify")
    return controller.submit(f"submission-{number}", artifact, EVIDENCE)


def evaluate(controller, checks=None, **kwargs):
    controller.dispatch("evaluate")
    controller.evaluate(
        good(config().evaluation_checks) if checks is None else checks,
        EVIDENCE,
        **kwargs,
    )


def through_activation(controller):
    through_baseline(controller)
    submit(controller)
    evaluate(controller)
    controller.dispatch("activate")
    controller.activated(ARTIFACT.sha256, good(ACTIVATION_CHECKS), EVIDENCE)


def finish(controller):
    controller.dispatch("continue")
    controller.outcome(good(OUTCOME_CHECKS), EVIDENCE)
    return controller.account()


def values(controller, kind):
    return [
        r.value["data"] for r in controller.journal.verify() if r.value["kind"] == kind
    ]
