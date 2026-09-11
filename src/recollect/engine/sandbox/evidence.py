"""Track changed research content across native execution checkpoints."""

import json
from urllib.parse import urldefrag


class ResearchEvidence:
    def __init__(self):
        self.content: dict[str, list[str]] = {}
        self.version = 0
        self.unchanged_calls = 0

    def observe_call(self, tool: str, args: dict, output: str) -> None:
        before = self.version
        self.observe(tool, args, output)
        if tool in {"web_fetch", "web_search"}:
            self.unchanged_calls = (
                self.unchanged_calls + 1 if self.version == before else 0
            )

    def observe(self, tool: str, args: dict, output: str) -> None:
        if tool not in {"web_fetch", "web_search", "read", "edit", "write"}:
            return
        try:
            data = json.loads(output)
        except (ValueError, TypeError):
            data = None
        if isinstance(data, dict):
            if data.get("error"):
                return
            if tool == "web_fetch":
                key = urldefrag(str(data.get("final_url") or args.get("url", "")))[0]
                self._add(key, str(data.get("text", "")).removesuffix(" [...]"))
                return
            if tool == "web_search":
                for item in data.get("results", []):
                    if isinstance(item, dict):
                        self._add(str(item.get("url", "")),
                                  str(item.get("title", "")) + " "
                                  + str(item.get("snippet", "")))
                return
        # Changes in call IDs, output limits, and error metadata aren't findings.
        key = str(args.get("url") or args.get("filePath") or tool)
        self._add(key, output)

    def _add(self, key: str, text: str) -> None:
        text = " ".join(text.split())
        previous = self.content.setdefault(key, [])
        if not text or any(text in known for known in previous):
            return
        previous[:] = [known for known in previous if known not in text]
        previous.append(text)
        self.version += 1
