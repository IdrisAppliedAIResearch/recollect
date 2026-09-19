---
name: recollect-seams
description: The boundary of what you may build and use here — timer, notices, your namespaced store, channels. Read before booking a reminder or building any capability.
---

<steps>
1. Read the `[recollect identity]` line of your instruction. Those ids
   are real; everything you book carries them verbatim.
2. To act at a moment in time: `schedule_at` with a timezone-aware ISO
   time and a short text. The host, not you, delivers it when it comes
   due — possibly after your process is gone.
3. To keep durable state: your capability's namespaced store (the tools
   you were given expose it; state lives on the host, never in
   `/workspace`, which is scrubbed after every invocation).
4. To send off-device: never send yourself. Book a notice naming the
   channel the user configured; the host sends it at delivery time.
5. When no seam can say what you need, report blocked with a
   capability_gap — a missing seam is a decision for the user, not a
   thing to grow on your own.
</steps>

<rules>
- Identity is copied, never invented: no ids in your instruction, no
  booking notices. Inventing ids misroutes real notices.
- What the user sees goes through a notice (on-device) or a configured
  channel (off-device). No other surfaces exist for you.
- New durable state means the store seam; never ask for, or assume, a
  new endpoint.
- Timers are one-shot. A recurring reminder is re-booked after each
  fire.
- A failed send or booking is reported with its reason, never retried
  in a loop and never claimed without the id that proves it worked.
- Compose what exists. If a seventh mechanism seems needed, that is
  exactly what a capability_gap report is for.
</rules>
