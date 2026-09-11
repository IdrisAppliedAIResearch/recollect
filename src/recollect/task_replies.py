"""Deterministic replies for file locations and narrow task-only questions."""

import re


def operational_reply(text: str) -> bool:
    """Recognize work claims and task-request narration in memory candidates."""
    text = text.lower().replace("’", "'")
    work_claim = re.search(
        r"\b(?:i(?:'m| am| will|'ll|'ve| have)?|we(?:'re| are|'ll| will)) "
        r"(?:am |have |just |now |currently |going to |will |be |start |started |"
        r"begin |begun |do |doing |some |quick |need to )*"
        r"(?:research(?:ing)?|look(?:ing)? (?:up|into)|search(?:ing)?|"
        r"work(?:ing)? on|creat(?:e|ing|ed)|sav(?:e|ing|ed)|"
        r"revis(?:e|ing|ed)|updat(?:e|ing|ed)|queu(?:e|ing|ed)|"
        r"kick(?:ed|ing)? off|start(?:ed|ing)? (?:the |a )?task)\b", text,
    )
    request_history = re.search(
        r"\b(?:the user|you|they) (?:are |is )?"
        r"(?:asking|asked|requesting|requested|want|wants) (?:me|us|you) to\b", text,
    )
    task_decision = re.search(
        r"\b(?:the user|you|they) (?:declined|canceled|cancelled|stopped|paused) "
        r"(?:the |any |further |more )*(?:research|task|delegation|file creation)\b",
        text,
    )
    return bool(work_claim or request_history or task_decision)


def unstarted_work(message: str, reply: str) -> bool:
    # Explicit research commands need an operation even if the model merely says
    # "I can help". This narrow command grammar excludes questions about research.
    text = message.lower().replace("’", "'")
    if re.search(r"\b(?:don't|do not|no need to) (?:research|search|look up)\b", text):
        return False
    if re.match(r"(?:please )?(?:explain|define|translate|quote)\b", text):
        return False
    if re.search(
        r"\b(?:if you (?:give|provide|tell)|once you|which company|which topic|"
        r"what company|"
        r"what topic|what dates|could you specify|could you provide)\b", reply.lower(),
    ):
        return False
    return bool(re.search(
        r"(?:^|[.!?;]\s+)(?:please )?"
        r"(?:(?:can|could|would|will|do) you (?:please |want to |mind )?|"
        r"i (?:want|need) you to )?"
        r"(?:do )?(?:(?:some|quick|a little) )*"
        r"(?:research|look up|find out|investigate|search for)\b", text,
    ))


def substantive_memory(text: str) -> str | None:
    # A mixed reply can retain a separate fact sentence, never its work promise.
    sentences = re.split(r"(?<=[.!?;])\s+", text.strip())
    return " ".join(s for s in sentences if not operational_reply(s)).strip() or None


def task_question(message: str) -> str | None:
    # Full-message matches deliberately exclude mixed requests and new facts.
    text = message.strip().lower().replace("’", "'").rstrip("?.!")
    if re.fullmatch(
        r"why (?:did you not|didn't you) (?:say anything|tell me|speak|update me)"
        r"(?: when you got (?:a |the )?(?:partial answer|result|findings))?", text,
    ):
        return "delivery"
    if re.fullmatch(
        r"(?:what(?:'s| is) the (?:relative |absolute |local )?path "
        r"(?:for|to) (?:that|the|my) (?:file|document)|"
        r"where (?:is|did you save|can i find) (?:that|the|my) (?:file|document)|"
        r"where (?:is|are) (?:that|the|my) (?:file|document)s? saved|"
        r"how (?:can|do) i (?:download|open) (?:that|the|my) (?:file|document))",
        text,
    ):
        return "files"
    if re.fullmatch(
        r"(?:any updates|(?:what(?:'s| is) the )?(?:status|progress)|"
        r"how(?:'s| is) (?:it|the (?:task|research|work)) (?:going|progressing)|"
        r"what have you (?:found|learned) so far|"
        r"(?:is it|are you) (?:done|finished|still working)|"
        r"what access restrictions are you hitting|"
        r"you (?:didn't|did not|haven't|have not) (?:actually )?"
        r"start(?:ed)? (?:looking|working|researching|the research|the task)|"
        r"what(?:'s| is) (?:the )?(?:subagent|worker) doing|"
        r"(?:check|ask) (?:the )?(?:subagent|worker)(?: for)? (?:an? )?updates?|"
        r"what can you see of (?:the work that )?(?:the )?"
        r"(?:research agent|subagent|worker)(?:'s| is) doing)", text,
    ):
        return "status"
    return None


def worker_message(message: str) -> bool:
    """Explicitly addressing the worker is already a routing decision."""
    return bool(re.match(
        r"(?:please )?(?:tell|ask|message) (?:the )?(?:subagent|worker)\b",
        message.strip(), re.IGNORECASE,
    ))


def activity_reply(task: dict) -> str:
    state = task["state"]
    if state == "queued":
        return "The research is queued and waiting to start."
    if task["revision"] > (task.get("accepted_revision") or 1):
        return "Your message is saved; the worker hasn't acknowledged it yet."
    if state == "blocked":
        return "The worker is waiting for a new direction or an answer."
    activity = task.get("activity") or task.get("checkpoint", {}).get("activity", {})
    actions = {
        "web_search": "searching for sources", "web_fetch": "reading a source",
        "skill": "loading its task instructions", "read": "reading a file",
        "write": "writing a file", "edit": "editing a file",
    }
    action = actions.get(activity.get("tool"), "using a research tool")
    if activity:
        return (f"The latest recorded step was {action}. "
                "I'm waiting for its next report.")
    return "The worker is active, but it hasn't reported a new finding yet."


def status_reply(tasks: list[dict]) -> str:
    replies = []
    for task in tasks:
        text = activity_reply(task)
        if task.get("progress"):
            text += " Latest report: " + task["progress"][:2_000]
        if task.get("findings"):
            text += " Findings so far: " + " ".join(task["findings"][-3:])
        if len(tasks) > 1:
            text = task["objective"][:160] + ": " + text
        replies.append(text)
    return "\n\n".join(replies)


def artifact_reply(tasks: list[dict]) -> str:
    artifacts = [
        (task, item) for task in reversed(tasks)
        for item in task.get("artifacts", [])
    ]
    if not artifacts:
        return "There is no saved file in this conversation yet."
    if len(artifacts) > 8:
        return "There are several saved files. Which filename do you mean?"
    lines = []
    for task, item in artifacts:
        url = (f"/api/sessions/{task['session_id']}/tasks/{task['task_id']}"
               f"/artifacts/{item['artifact_id']}")
        # Display the saved name without allowing it to change Markdown syntax.
        name = item["filename"].replace("[", "\\[").replace("]", "\\]")
        line = f"[{name}]({url})"
        if item.get("download_path"):
            line += f" — saved on the desktop at {item['download_path']}"
        else:
            line += " — use this link to download the saved file"
        lines.append(line)
    return "\n\n".join(lines)
