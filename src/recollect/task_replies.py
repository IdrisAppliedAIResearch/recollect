"""Deterministic replies for file locations and narrow task-only questions."""

import re


def task_question(message: str) -> str | None:
    # Full-message matches deliberately exclude mixed requests and new facts.
    text = message.strip().lower().replace("’", "'").rstrip("?.!")
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
        r"what access restrictions are you hitting)", text,
    ):
        return "status"
    return None


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
