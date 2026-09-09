"""Check the deployed chat model's token envelope without altering memory."""

from __future__ import annotations

import httpx


async def check_context(
    client: httpx.AsyncClient, payload: dict, context_tokens: int
) -> int:
    root = str(client.base_url).rstrip("/").removesuffix("/v1")
    template = await client.post(
        f"{root}/apply-template",
        json=payload,
        timeout=15,
    )
    template.raise_for_status()
    prompt = template.json().get("prompt")
    if not isinstance(prompt, str):
        raise ValueError("The model server did not return its formatted prompt.")
    tokenized = await client.post(
        f"{root}/tokenize",
        json={"content": prompt, "add_special": True},
        timeout=15,
    )
    tokenized.raise_for_status()
    tokens = tokenized.json().get("tokens")
    if not isinstance(tokens, list):
        raise ValueError("The model server did not return prompt token accounting.")
    required = len(tokens) + int(payload["max_tokens"])
    # Leave a small template reserve; never shorten the verified RECENT block.
    if required + 64 > context_tokens:
        raise ValueError(
            f"The request needs {len(tokens)} prompt tokens plus "
            f"{payload['max_tokens']} output tokens and a 64-token reserve; "
            f"the deployed model context is {context_tokens}. "
            "The verified memory was not truncated."
        )
    return len(tokens)
