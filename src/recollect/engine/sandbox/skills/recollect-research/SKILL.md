---
name: recollect-research
description: Verify external facts and source identity during delegated web research. Not needed for files made solely from supplied facts.
---

Establish the entity and its official website from search results or cross-linked
sources. A guessed domain, a parked page, or missing search results does not
establish that an organization is absent or inactive. If the general-web leg is
blocked, unrelated scholarly hits do not verify a company. Try a relevant public
index or a known source; report access limitations if verification remains blocked.

Fetch the sources behind important claims. Keep source URLs with the facts they
support, and compare dates, units, and qualifiers before reporting. For named
comparisons, verify both the named entity and the claimed overlap. Distinguish
inference from evidence; omit unsupported entries rather than fill a target count.

The article extractor can omit short factual cards. When a page contains a label
but its value is missing, try `web_fetch` with `view="page"` once. An increased
`max_chars` helps only when the observation says it was truncated. Do not repeat
an unchanged fetch expecting missing content to appear. Use another source or
report the missing fact as unverified.

Send useful facts in `kind=finding` reports as they are verified; an acceptance
or progress message does not publish a finding to the main conversation. Finish
with the supported answer and any specific missing evidence. If work cannot
advance after changing the source or extraction view, report the blocker.
