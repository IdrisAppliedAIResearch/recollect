---
name: recollect-reporting
description: Report accepted instructions, useful findings, blockers, and final answers to Recollect's main conversation during delegated work.
---

Use `recollect_research_report_message` to acknowledge each instruction with
`kind=accepted` before working. Copy its revision and related message ID exactly.
Preserve earlier requirements unless replaced, and relay changes to native child
tasks. Give a child the revision, message ID, and this reporting skill's name.
For external fact-finding, load `recollect-research` before choosing sources.

Send `kind=progress` when a meaningful step changes what the main agent should
know, such as resolving the source identity or encountering a retrieval problem.
Do not repeat unchanged status or wait for the main agent to poll for updates.

Report supported discoveries with `kind=finding` as they become useful, including
the actual facts and supporting URLs in `sources`. Report completed observations,
not invented progress. Use `blocked` or `question` for a concrete obstacle or
missing decision. A search hit alone does not establish a finding.
If retrieval fails, report what remains unverified. Do not manufacture names,
relationships, or source URLs to fill a requested list. Distinguish retrieved
facts from hypotheses, and do not present background knowledge as current research.

Finish with `kind=result`: put the answer itself in `text`, including relevant
names, comparisons, caveats, and supporting URLs. If it exceeds the tool's text
limit, report coherent findings first, then synthesize them in the result.
The main agent must be able to explain the answer from these reports without
opening a file. The final conversational response should contain that answer too.
Use concise prose suitable for speaking aloud by default. Reserve tables for a
user-requested table; do not expand a brief comparison into extra research fields.

Research, summaries, lists, and comparisons are conversational answers by default.
Do not create a document or extra format merely to finish research. Load
`recollect-files` when the user requests creation or revision of a saved file.
Files never replace reporting the findings.
