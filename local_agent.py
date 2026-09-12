#!/usr/bin/env python3
"""Local-only inference layer for Secret Agent.

The model endpoint must be bound to localhost. Optional connectors can retrieve
organization data over the network, but model inference never leaves the device.
"""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from collections import Counter
from typing import Any

from connectors import connector_context
from demo import get_task_context, search_corpus

PRODUCT_NAME = "Secret Agent"
MODEL = os.environ.get("SECRET_AGENT_MODEL_NAME", "Qwen2.5-1.5B-Instruct Q4_K_M")
MODEL_API_URL = os.environ.get(
    "SECRET_AGENT_LLM_URL", "http://127.0.0.1:8081/v1/chat/completions"
)

SYSTEM_PROMPT = """You are Secret Agent, an on-device AI for an organization.

All language-model inference runs locally on the employee's computer. You may receive two kinds of context:
1. INTERNAL PROCEDURAL MEMORY: organization-specific procedures, rules, examples, and prior cases retrieved from the local SQLite corpus.
2. CONNECTED ORGANIZATION CONTEXT: data explicitly retrieved from user-authorized connectors such as Granola.

Rules:
- Treat retrieved organization context as authoritative only for what it actually states.
- Never invent organization policy, approvals, identities, dates, outcomes, or actions.
- If required information is missing, say exactly what is missing.
- Separate explicit decisions from proposals and speculation.
- For high-stakes medical, legal, regulatory, security, or compliance material, provide structured support but do not impersonate an authorized decision-maker.
- Preserve useful source links or citations included in connector context.
- Do not claim you searched the internet. The model itself has no internet access.
- Answer naturally and concisely unless the user asks for detail.
"""


def _procedural_context(prompt: str) -> dict[str, Any]:
    search = search_corpus(prompt, 10)
    results = search.get("results") or []
    if not results:
        return {"search_results": [], "task": None, "records": []}

    # Prefer tasks that recur across the high-ranked retrieval set while giving
    # the first authoritative hit a strong tie-breaker.
    counts = Counter(str(row.get("task", "")) for row in results if row.get("task"))
    first_task = str(results[0].get("task", ""))
    best_task = max(counts, key=lambda name: (counts[name], name == first_task)) if counts else first_task
    bundle = get_task_context(best_task, 2) if best_task else {"records": []}
    return {
        "search_results": results[:6],
        "task": best_task or None,
        "records": bundle.get("records") or [],
    }


def _context_text(prompt: str) -> str:
    procedural = _procedural_context(prompt)
    parts: list[str] = []
    if procedural.get("records"):
        parts.append("INTERNAL PROCEDURAL MEMORY:\n" + json.dumps(procedural, ensure_ascii=False, indent=2))

    for item in connector_context(prompt):
        parts.append(
            f"CONNECTED ORGANIZATION CONTEXT — {item['source']}:\n{item['content']}"
        )

    if not parts:
        return "No organization-specific context was retrieved for this request."
    return "\n\n".join(parts)


def _local_chat(messages: list[dict[str, str]]) -> dict[str, Any]:
    payload = json.dumps(
        {
            "model": MODEL,
            "messages": messages,
            "temperature": 0.15,
            "max_tokens": 1000,
            "stream": False,
        }
    ).encode("utf-8")
    request = urllib.request.Request(
        MODEL_API_URL,
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=180) as response:
            return json.load(response)
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")[:2000]
        raise RuntimeError(f"Local model returned HTTP {exc.code}: {body}") from exc
    except urllib.error.URLError as exc:
        raise RuntimeError(
            "Secret Agent's local model is not running. Restart the desktop app so it can launch the on-device model runtime."
        ) from exc


def run_agent(user_prompt: str, verbose: bool = False) -> str:
    context = _context_text(user_prompt)
    messages = [
        {"role": "system", "content": SYSTEM_PROMPT},
        {
            "role": "user",
            "content": (
                f"ORGANIZATION CONTEXT\n{context}\n\n"
                f"EMPLOYEE REQUEST\n{user_prompt}\n\n"
                "Use only the organization context that is relevant to the request."
            ),
        },
    ]
    response = _local_chat(messages)
    choices = response.get("choices") or []
    if not choices:
        raise RuntimeError(f"Local model returned no choices: {response}")
    message = choices[0].get("message") or {}
    answer = str(message.get("content") or "").strip()
    if verbose:
        print(answer)
    return answer
