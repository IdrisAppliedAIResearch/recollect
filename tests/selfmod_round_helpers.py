"""Round fixtures: explicit host observations, never an agent rehearsal."""

from recollect.selfmod.clock import Stamp
from recollect.selfmod.contracts import File, Snapshot
from recollect.selfmod.round import ModificationRound, RoundConfig
from tests.test_selfmod_contracts import make_scope

EVIDENCE = Snapshot((File("fixture.txt", b"host observation"),))
ARTIFACT = Snapshot((File("extension.py", b"nonexecutable fixture bytes"),))


class FakeClock:
    def __init__(self):
        self.ns = 1_000_000_000
        self.boot = "fixture-process"

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


def round_config(contract=None, baseline_sha256=None):
    baseline, _, scope_contract, _, _ = make_scope()
    return RoundConfig("fixture-round", contract or scope_contract,
                       baseline_sha256 or baseline.sha256)


def create(root, config=None, clock=None, fault=None):
    return ModificationRound.create(root, config or round_config(),
                                    clock=clock or FakeClock(),
                                    fault=fault or Fault())


def values(owner, kind):
    return [r.value["data"] for r in owner.journal.verify() if r.value["kind"] == kind]


def submitted(owner, number=None):
    """Files journaled with an accepted candidate (``candidate/<path>`` included)."""
    for record in reversed(owner.journal.verify()):
        data = record.value["data"]
        if (record.value["kind"] == "candidate_submitted"
                and number in (None, data["candidate_number"])):
            return {f.path: f.content for f in record.files.files}
    raise AssertionError("no candidate was submitted")
