# Continuous delegated work

Continuous mode lets the assistant delegate research or supported file work,
finish its acknowledgment, and keep conversing while the task runs. Open the
**Research workspace** in the conversation to inspect saved work and send task
instructions. Speaking over a reply stops playback; explicit task cancellation
stops research.

## Enable the OpenCode workflow

Continuous mode is opt-in and requires the Docker OpenCode backend. Add these
settings to the existing `.env`, preserving other settings, then restart Recollect
using the full local launch procedure in [AGENTS.md](../AGENTS.md#71-agent-owned-full-local-launch):

```dotenv
RECOLLECT_SUBAGENT_ENABLED=true
RECOLLECT_SUBAGENT_BACKEND=opencode
RECOLLECT_SUBAGENT_CONTINUOUS_ENABLED=true
RECOLLECT_GENERATOR_CONTEXT_TOKENS=32768
RECOLLECT_SUBAGENT_INFERENCE_TOKENS=2048
```

Use the pinned `recollect-opencode-sandbox:1.18.18` image rebuilt with the current
research tools when its build inputs change. The task reporting tool must be
present in that image. Keep the existing generator URL/model, CPU embedding
configuration, GPU voice settings, and sandbox isolation. There is no host
OpenCode fallback and no second chat model.

Model access is admitted at inference boundaries. The default remains one model
slot. An optional two-slot configuration uses
`RECOLLECT_GENERATOR_PARALLEL_SLOTS=2` with llama-server
`--ctx-size 65536 --parallel 2`; the context setting above remains **32768 per
slot**. With the default `RECOLLECT_GENERATOR_PARALLEL_SLOTS=1`, use the established
`--ctx-size 32768 --parallel 1` launch. The application setting and server launch
must agree. These settings do not guarantee two permanent prompt caches; cache
reuse and GPU capacity need validation with the actual model and voice workload.
Measured operating evidence is documented separately.

Disabling continuous mode preserves saved tasks and downloads. It disables new
continuous task commands. The legacy backend and `/v1` conversation behavior are
unchanged; this workflow is exposed through the Recollect `/api` chat interface.

## Working with a task

| Action | Outcome |
| --- | --- |
| Ask the assistant to research or create a supported file | A durable task is queued. Its original request remains inspectable even if the spoken acknowledgment is interrupted. |
| Ask for progress | The assistant receives saved findings and current state. Tool activity alone is not treated as a finding. |
| Send direction or answer a task question | A new instruction revision is saved on the owned task. The UI distinguishes receipt from worker acceptance. |
| Cancel task | Queued work is removed from admission; running work is stopped. Saved findings and file versions remain. Cancellation can show as requested until cleanup finishes. |
| Continue work | Finished or stopped work gets an explicit follow-up task linked to its saved predecessor. An active task receives steering instead. |
| Quiet updates | Routine proactive progress is suppressed. The browser mutes task notification speech; results, questions, and blockers remain visible. |

One delegated task owns the sandbox at a time, with up to eight queued tasks.
The queue is FIFO and has no promised start time. A task waiting for your answer
can retain ownership, so other research remains queued while the main chat stays
available. Additional model inference slots do not create additional independent
research workers.

There is no overall elapsed-time deadline for a task. Network requests and
individual inference calls still have bounds. Native step limits act as
checkpoints: useful work can be saved and continued in the same invocation.
Repeated checkpoints without new evidence, repeated reconciliation failures,
context exhaustion, or execution errors produce a blocked/partial outcome.
Use **Continue work** or provide a new direction after resolving the blocker.

If a change arrives after the native worker finishes, the saved result retains
the revision it actually used and the later instruction becomes a linked
follow-up. A full queue can leave that follow-up visibly blocked for later
continuation. Retrying the same request ID does not start duplicate work.

## Files, retention, and deletion

Workers can export UTF-8 `.txt`, `.md`, `.csv`, and `.json` files. Other formats
are outside this release's artifact export capability. Downloads use opaque
artifact IDs; the API does not accept arbitrary host paths. Export rejects links,
reparse points, hardlinks, unsafe paths, unstable copies, and oversized files.
Checkpoint/export happens after native work is quiescent.

Set `RECOLLECT_DOWNLOADS_DIR` to the desktop's absolute Downloads directory to
also save each exported version there automatically. Existing files are never
overwritten: collisions receive numbered names. The task panel and completion
update show the recorded location and a download link. A failed Downloads copy
leaves the archived file available through that link. In host mode this directory
is on the desktop; a Surface client uses the link to download onto the Surface.
Deleting task storage does not delete delivered Downloads files.

Task starts, steering, cancellation, and other controls are display-only by
default. Mixed requests can supply `memory_reply` containing the substantive
conversation without the work acknowledgment; the trace records that exact
stored response. Ordinary chat still forms episodes. Pure file-location queries
are answered from saved artifact records without model generation or ingestion.
Recognized pure progress queries cannot be stored even if the model incorrectly
sets `status_only=false`; other conversational replies still use explicit routing.

Changed file contents create immutable versions. Identical contents reuse the
latest version. A failed export leaves previously published versions available.
Continuations restore retained files into a clean workspace, with newer saved
versions taking precedence. The worker's native conversation itself is not
restored across separate invocations.

| Storage bound | Current limit |
| --- | --- |
| One exported file | 2 MiB |
| All retained file versions in one task | 32 MiB and 512 versions |
| Saved tasks in one conversation | 128 |
| One task record | 256 KiB |
| One mailbox message | 64 KiB |
| One task mailbox | 10,000 messages or 8 MiB |
| Entries inspected in one workspace scan | 2,048 |

Capacity errors are explicit. Existing published files are not silently deleted
to make room. Task data has no automatic expiry. There is currently no individual
file-version deletion control; deletion applies to a saved task or all saved
research in the conversation.

Use **Manage this saved task** to delete one task, or **Manage saved research**
to delete all task data in that conversation. Cancel active and queued work and
wait for it to stop first. Deletion removes findings, mailbox updates, and file
versions, while ordinary chat turns and episodic memory remain. Deleting a task
that later tasks depend on can make their continuation unavailable; the UI shows
those dependencies before deletion. A reset invalidates old task/message IDs.

## Memory and restart behavior

Task state lives in each conversation's `tasks.sqlite`; immutable exports live
under `task-artifacts/`. These private directories are outside the sandbox mount.
They are separate from `episodes.sqlite`. Conversations cannot read or restore
one another's tasks or artifacts.

Raw tool activity, internal agent messages, and proactive notifications add no
episodes. A status-only exchange identified as such is saved for display without
episodic ingestion. Substantive conversation and user steering can still become
ordinary remembered exchanges. Status classification is part of the main
assistant's tool decision; mixed status/substantive requests are not automatically
excluded from memory.

The main model receives a bounded temporary task snapshot containing relevant
state, findings, sources, files, recent directions, and delivered conversational
references such as an ordered list or pending question. This supplements verified
retrieval without changing its packing budget or strict shadow comparison.
Generation traces report `task_context_chars`, related `task_ids`, and
`model_queue_ms`; `total_prompt_chars` includes supplemental task input. Retrieval
character accounting continues to describe the verified memory block itself.

Closing or reloading the browser does not cancel server-owned work. Reconnecting
loads saved status and notifications without replaying old speech. On server
restart, previously running or cancel-requested tasks become interrupted;
queued work is recovered in creation order. Stopped blocked tasks remain
available for explicit continuation. No native process is assumed to still own
a persisted running label. Saved results, reported findings, and checkpointed
files survive; uncheckpointed native work may be lost.

Shutdown stops admission, requests worker cancellation, preserves what can be
checkpointed, and then tears down the sandbox. An unresponsive owned sandbox has
a bounded emergency stop path. If isolation cannot be confirmed after failure,
workspace reuse is refused rather than copying uncertain files into another task.

## API reference

All task routes use the application's existing access boundary and validate
conversation ownership. Start work through `/api/chat` with a stable `request_id`;
the main assistant decides whether to delegate. There is no separate public
start-task endpoint.

| Method and route | Purpose |
| --- | --- |
| `GET /api/sessions/{session_id}/tasks` | Snapshot with `enabled`, tasks, recent notifications, and mailbox cursor. |
| `POST /api/sessions/{session_id}/tasks/{task_id}/messages` | Send `steer`, `continue`, `cancel`, or `quiet`. |
| `GET /api/sessions/{session_id}/tasks/{task_id}/artifacts/{artifact_id}` | Download an immutable version. |
| `DELETE /api/sessions/{session_id}/tasks/{task_id}` | Delete stopped task data and exports. |
| `POST /api/sessions/{session_id}/tasks/reset` | Delete all stopped task data in the conversation. |
| `POST /api/voice/notification` | Speak a saved notification using `session_id`, `notification_id`, and optional `stream`. |

Example steering body:

```json
{
  "request_id": "client-generated-unique-id",
  "operation": "steer",
  "text": "Include installation in the total price."
}
```

Reuse the same request ID when retrying the same operation. `quiet` uses a boolean
`quiet` field; an answer can carry `reply_to` identifying the task's saved
question. The browser polls snapshots; there is no public raw-mailbox stream.
Missing owned resources return 404; invalid controls or conflicting task state
return 409. Downloads are attachments with private, non-cacheable responses.
