# DRAFT - Google Calendar preregistration amendment 03: tool-call liveness

**Status: unregistered draft.** Not committed as a registration, not hashed into a
detached receipt, not published and not in force. The user registers it, with any
edits, as part of preregistration. Until then amendments 01 and 02 alone apply.

Proposed registration ID: `recollect-selfmod-calendar-amendment-03`

Drafted 2026-09-15, before any target baseline, Calendar trial, generated Calendar
integration or activation, in response to an implementation finding rather than
an experimental observation.

## 1. Finding

Amendment 02 removes elapsed-time cutoffs from agent work, including tool calls.
Read-only inspection of the pinned OpenCode 1.18.18 binary (SHA-256
`bb71f45b564f9234a97f54d6252a4a41d2f4388ae4b078918f691824cc3b3e54`) shows that
every MCP tool call carries a request timeout: the server's configured `timeout`,
else `experimental.mcp_timeout`, else the bundled MCP SDK default. No configuration
disables it. The same calls pass `resetTimeoutOnProgress: true` and no
`maxTotalTimeout`, so each MCP progress notification restarts the timer.

## 2. Rule

The harness-owned tool host that runs the subagent's MCP tools, including any
modifier-generated tool, executes each tool call apart from its protocol loop and
emits MCP progress notifications while that call is actually running. A healthy
call, however slow or quiet its own work, therefore never reaches the configured
per-request timeout, and no total duration is imposed.

This liveness signal is transport behavior of the tool host, not a work deadline:
it is emitted independently of the tool's progress, never started, stopped or
weakened by elapsed time, and it carries no instruction to the agent. The
configured per-request value (120,000 ms) can expire only if the tool host itself
stops delivering protocol messages. That is an observed host failure under
amendment 02 section 3, recorded as such, not a slow-work classification, retry
condition or refresh authority.

The tool host, its keepalive implementation and the configured value are frozen
in the runtime manifest. The modifier cannot edit them; they sit outside the
subagent's writable surface. Any other elapsed-time cutoff remains prohibited.

## 3. Unchanged

The original protocol, checkpoint protocol, amendment 01 and amendment 02 remain
in force and unedited. Candidate submission, infrastructure-retest and provider
retry counts are unchanged. No observed trial result motivated this amendment.
