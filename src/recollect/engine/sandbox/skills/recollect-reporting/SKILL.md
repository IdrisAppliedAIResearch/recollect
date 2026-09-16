---
name: recollect-reporting
description: How to report to Recollect's main conversation during delegated work: acknowledgment, findings, blockers, capability gaps, final result.
---

<order>
1. accepted: first, before any work. Copy the revision and related message ID exactly.
2. progress: only when something changes what the main conversation should know, such as identifying the source or retrieval failing. Don't repeat unchanged status.
3. finding: each verified fact as soon as you have it.
4. Finish with exactly one of: result, question, blocked.
Before looking up external facts, load recollect-research.
</order>

<finding>
- The main conversation keeps findings and recalls them later, so each must stand alone: its facts, its caveats, its URLs in sources.
- Report only what you retrieved. A search hit alone is not a finding. Don't present background knowledge as research, and don't invent names, relationships or URLs to fill a list.
</finding>

<result>
- Two or three spoken sentences. Lead with the answer.
- Don't repeat the findings; they are already saved.
- Mention a limitation only if it changes how far the answer can be trusted: retrieval you couldn't complete, a claim backed only by the subject's own material, or findings that disagree.
- No routine sourcing remarks. No table unless the user asked for one.
- Exception: list every authentication step the user must follow.
</result>

<question_or_blocked>
- question: a decision only the user can make.
- blocked: a concrete obstacle. Say what remains unverified.
</question_or_blocked>

<capability_gap>
If the request needs something none of your tools can do:
1. Don't simulate it, substitute another action, or claim success.
2. Send one blocked report. In text, explain the limitation, then add this block:

```capability_gap
{"type": "capability_gap", "missing_capability": "...", "attempted": ["..."], "modification_request": "..."}
```

- missing_capability: what you cannot do.
- attempted: what you checked or tried.
- modification_request: the smallest new capability that would let this request be completed.
</capability_gap>

<child_tasks>
If you start a child task, give it the revision, the related message ID, this skill's name, and every requirement still in force.
</child_tasks>
