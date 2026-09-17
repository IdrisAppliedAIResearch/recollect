# Subagent self-modification

Main chat delegates a request to subagent A. When A has no tool that can do it,
Recollect asks the user whether to build one. On a yes it builds the capability,
proves it on B by finishing the same request, and B becomes A.

## Flow

1. **Gap.** A sends a structured `capability_gap` report instead of a result.
   Its task stops but stays cancelable, and the chat asks the user to build or
   skip (by voice, text or the task card buttons).
2. **Tests first.** An author writes stdlib check scripts anchored on the
   original request; a reviewer approves them and they are frozen.
3. **Develop.** A stock OpenCode session plans, gets a JSON plan review, then
   implements in the same session. Each reply is checked against the change
   policy, the frozen checks run in a networkless container, and a fresh agent
   reviews the code. Failures go back into the session.
4. **Prove on B.** The candidate tree becomes bundle image B and the original
   request resumes on it. Other new work keeps running on A meanwhile.
5. **Promote.** When the resumed request completes, B's changed files are
   committed on a new branch, `selfmod/<feature>-<timestamp>`, which is checked
   out. B serves all new work, and the replaced A's sandbox, image and
   directories are removed once its running task settles.

Any failure discards B (sandbox, image, directories) and retries from A with the
failure as feedback, until a build finishes or the user stops it. No elapsed-time
or attempt limit applies.

## Images and restarts

Only A's bundle image is kept. At start, Recollect builds (or reuses) A's image
from the checked-out tree, then removes every other `recollect.bundle` image and
all self-modification directories. A build interrupted by a restart is scrapped.

## Rolling back

```bash
git switch <previous-branch>
```

Then restart Recollect. The commit message names the previous branch.

## Code

| Piece | Module |
|---|---|
| Gap parsing | `gap_trigger.py` |
| Tests first | `tests_first.py` |
| Agentic development and offline checks | `agents.py` |
| Retry loop and the switch to B | `loop.py` |
| Bundle images and A/B routing | `deployment.py` |
| Saving a build on a branch | `promotion.py` |
| Served tree and change scope | `subagent_tree.py` |
| App wiring, notices, cleanup | `service.py` |

Self-modification is on by default; `RECOLLECT_SELFMOD_ENABLED=0` turns it off.
