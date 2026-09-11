---
name: recollect-files
description: Create or revise a saved TXT, Markdown, CSV, or JSON file when the user requests a file or download. Ordinary research and conversational summaries do not require this skill.
---

Create only the requested deliverable and format under `/workspace`. A request
for a summary, list, comparison, or research answer alone does not request a file.
Do not produce companion formats unless asked. Supported extensions are `.txt`,
`.md`, `.csv`, and `.json`; content must be UTF-8, at most 2 MiB per file and
32 MiB per task. Shell execution and binary document conversion are unavailable.

Read the completed file to verify its contents. Report its relative workspace
path in the `artifacts` field of `recollect_research_report_message`, along with
the substantive answer in `text` and supporting URLs in `sources`. Only list
user-requested deliverables in `artifacts`; leave scratch notes out.

Recollect archives the workspace and copies explicitly reported deliverables to
the configured Downloads directory without overwriting existing files. The main
agent receives verified download paths and links after export. Do not invent host
paths or claim that `/workspace` is the user's Downloads directory.

For revisions, inspect restored files, preserve unchanged requirements, and
report the revised path again. Prior versions remain available.
