---
name: recollect-files
description: Create or revise a saved TXT, Markdown, CSV or JSON file. Only when the user asked for a file or download.
---

<steps>
1. Write only the requested file, in the requested format, under /workspace.
2. Read the file back to check its contents.
3. Report it: the answer in text, supporting URLs in sources, the file's relative path in artifacts.
</steps>

<rules>
- Allowed: .txt, .md, .csv, .json. UTF-8, at most 2 MiB per file and 32 MiB per task.
- No shell, no binary conversion, no extra formats.
- List only user-requested files in artifacts, not scratch notes.
- The file doesn't replace the answer: still report the findings.
- Recollect copies reported files to the user's Downloads folder and gives the main conversation the links. Don't invent host paths or call /workspace the Downloads folder.
- To revise: read the restored file, keep unchanged requirements, report the path again. Earlier versions stay available.
</rules>
