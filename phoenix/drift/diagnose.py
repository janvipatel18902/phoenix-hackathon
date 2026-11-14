"""Diagnose an unknown API error and propose a declarative adaptation spec.

The LLM (when configured) only PROPOSES JSON. It never executes anything: the
proposal goes through spec.validate_spec and the canary before any money moves.
Without an LLM (or when it fails) a deterministic heuristic proposes instead,
so the demo never depends on a network call to a model.
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from typing import Any

import httpx

from phoenix.drift.spec import SpecContext, SpecError

LLM_TIMEOUT_S = 30.0
ANTHROPIC_URL = "https://api.anthropic.com/v1/messages"
# Constants the heuristic may fill in for a newly required field. Derived from
# what this agent is doing (cancelling an event); anything else is unresolvable.
CONSTANT_HINTS: dict[str, str] = {"reason_code": "EVENT_CANCELLED"}
OPERATIONS = {"payments": "create_refund"}


class LLMError(RuntimeError):
    """The LLM call failed or returned something unusable."""


@dataclass
class Proposal:
    spec: dict[str, Any]
    used_llm: bool
    note: str


def is_drift(system: str, http_status: int | None, body: dict[str, Any]) -> bool:
    """A 422 validation_error with field details from a system we can adapt."""
    return (
        system in OPERATIONS
        and http_status == 422
        and body.get("error") == "validation_error"
        and isinstance(body.get("details"), list)
    )


def build_signature(
    system: str, http_status: int, body: dict[str, Any], api_version_from: int = 1
) -> dict[str, Any]:
    """Contract section 4 `signature`. api_version_to is inferred (from + 1):
    the 422 itself does not say which version the server moved to."""
    details = [d for d in body.get("details", []) if isinstance(d, dict)]
    return {
        "operation": OPERATIONS[system],
        "http_status": http_status,
        "unknown_fields": [
            d["field"] for d in details if d.get("message") == "unknown field"
        ],
        "missing_fields": [
            d["field"] for d in details if d.get("message") == "required"
        ],
        "api_version_from": api_version_from,
        "api_version_to": api_version_from + 1,
    }


def describe_signature(system: str, signature: dict[str, Any]) -> str:
    """Deterministic text, built by code, then embedded (stable retrieval)."""
    unknown = signature["unknown_fields"]
    missing = signature["missing_fields"]
    parts = []
    if unknown:
        parts.append(
            f"unknown field{'s' if len(unknown) > 1 else ''} {', '.join(unknown)}"
        )
    if missing:
        parts.append(
            f"missing field{'s' if len(missing) > 1 else ''} {', '.join(missing)}"
        )
    return (
        f"{system} {signature['operation']} returned {signature['http_status']}: "
        + "; ".join(parts)
    )


def _spec(system: str, ops: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "spec_version": 1,
        "system": system,
        "operation": OPERATIONS[system],
        "ops": ops,
    }


def heuristic_spec(
    system: str,
    signature: dict[str, Any],
    ctx: SpecContext,
    *,
    wrong_first: bool = False,
) -> dict[str, Any]:
    """Pair each unknown field with a required field it prefixes, scale dollars
    to minor units, fill other required fields from hints. SpecError if stuck.

    wrong_first reproduces the classic mistake for the demo: convert the type
    but forget the x100 (stores 85 minor units instead of 8500).
    """
    ops: list[dict[str, Any]] = []
    missing = list(signature["missing_fields"])
    for old in signature["unknown_fields"]:
        match = next((m for m in missing if m.startswith(old + "_")), None)
        if match is None:
            ops.append({"op": "remove_field", "field": old})
            continue
        ops.append({"op": "rename_field", "from": old, "to": match})
        missing.remove(match)
        value = ctx.request.get(old)
        if (
            match.endswith("_minor")
            and isinstance(value, str)
            and re.match(r"^\d+\.\d+$", value)
        ):
            ops.append(
                {
                    "op": "scale_value",
                    "field": match,
                    "factor": 1 if wrong_first else 100,
                    "from_type": "decimal_string",
                    "to_type": "integer",
                    "rounding": "half_up",
                }
            )
    for field_name in missing:
        if field_name in CONSTANT_HINTS:
            source = {"source": "constant", "value": CONSTANT_HINTS[field_name]}
        elif field_name in ctx.task_fields:
            source = {"source": "task_field", "value": field_name}
        else:
            raise SpecError(f"no safe value for required field {field_name!r}")
        ops.append({"op": "add_field", "field": field_name, "value": source})
    if not ops:
        raise SpecError("nothing to adapt")
    return _spec(system, ops)


def _llm_prompt(
    system: str,
    signature: dict[str, Any],
    ctx: SpecContext,
    rejected: list[dict[str, Any]],
    feedback: list[str],
) -> str:
    return (
        "A payments API changed. Propose a declarative adaptation spec as JSON ONLY.\n"
        f"Error: {describe_signature(system, signature)}\n"
        f"Error details: {json.dumps(ctx.details)}\n"
        f"Request we sent (one example): {json.dumps(ctx.request)}\n"
        "Allowed ops: rename_field {op,from,to}; scale_value {op,field,factor,from_type,"
        "to_type,rounding}; add_field {op,field,value:{source:'constant'|'task_field',value}};"
        " remove_field {op,field}. Types: decimal_string, integer, number.\n"
        'Shape: {"spec_version":1,"system":"payments","operation":"create_refund","ops":[...]}\n'
        "Context: this agent is cancelling a whole event and refunding every attendee. "
        "Money must stay equal: 85.00 dollars becomes 8500 minor units. "
        "Copy enum values exactly as the API spells them.\n"
        f"What the API or a dry run told us about earlier attempts: {json.dumps(feedback)}\n"
        f"Specs already tried and rejected by the canary: {json.dumps(rejected)}\n"
        "Reply with the JSON object only."
    )


def _llm_provider() -> str:
    explicit = os.environ.get("LLM_PROVIDER", "").lower()
    if explicit:
        return explicit
    return "groq" if os.environ.get("GROQ_API_KEY") else ""


def _call_anthropic(prompt: str) -> str:
    key, model = os.environ.get("LLM_API_KEY"), os.environ.get("LLM_MODEL")
    if not key or not model:
        raise LLMError("LLM_API_KEY / LLM_MODEL not set")
    resp = httpx.post(
        ANTHROPIC_URL,
        headers={"x-api-key": key, "anthropic-version": "2023-06-01"},
        json={
            "model": model,
            "max_tokens": 600,
            "messages": [{"role": "user", "content": prompt}],
        },
        timeout=LLM_TIMEOUT_S,
    )
    resp.raise_for_status()
    return str(resp.json()["content"][0]["text"])


def _call_groq(prompt: str) -> str:
    """Groq's OpenAI-compatible chat completions endpoint."""
    key, model = os.environ.get("GROQ_API_KEY"), os.environ.get("GROQ_MODEL")
    base = os.environ.get("GROQ_BASE_URL", "https://api.groq.com/openai/v1").rstrip("/")
    if not key or not model:
        raise LLMError("GROQ_API_KEY / GROQ_MODEL not set")
    resp = httpx.post(
        f"{base}/chat/completions",
        headers={"Authorization": f"Bearer {key}"},
        json={
            "model": model,
            "temperature": 0,
            "max_tokens": 2000,  # headroom: reasoning models think before answering
            "messages": [{"role": "user", "content": prompt}],
        },
        timeout=LLM_TIMEOUT_S,
    )
    resp.raise_for_status()
    return str(resp.json()["choices"][0]["message"]["content"])


def call_llm(prompt: str) -> str:
    """Returns the model's text. Raises LLMError on any failure (no provider,
    HTTP error, malformed reply) so the caller can fall back to the heuristic."""
    provider = _llm_provider()
    callers = {"anthropic": _call_anthropic, "groq": _call_groq}
    if provider not in callers:
        raise LLMError("no LLM provider configured")
    try:
        return callers[provider](prompt)
    except (httpx.HTTPError, KeyError, IndexError, ValueError) as exc:
        raise LLMError(f"LLM call failed: {type(exc).__name__}") from exc


def parse_llm_json(text: str) -> dict[str, Any]:
    text = re.sub(r"<think>.*?</think>", "", text, flags=re.DOTALL)  # reasoning models
    match = re.search(r"\{.*\}", text, re.DOTALL)
    if match is None:
        raise LLMError("no JSON object in LLM reply")
    try:
        parsed = json.loads(match.group(0))
    except ValueError as exc:
        raise LLMError("LLM reply is not valid JSON") from exc
    if not isinstance(parsed, dict):
        raise LLMError("LLM reply is not a JSON object")
    return parsed


def propose_spec(
    system: str,
    signature: dict[str, Any],
    ctx: SpecContext,
    rejected: list[dict[str, Any]],
    feedback: list[str] | None = None,
) -> Proposal:
    """First proposal may be deliberately wrong when PHOENIX_DEMO_WRONG_FIRST=1
    (the canary demo). After a rejection we go to the LLM / correct heuristic."""
    if os.environ.get("PHOENIX_DEMO_WRONG_FIRST") == "1" and not rejected:
        spec = heuristic_spec(system, signature, ctx, wrong_first=True)
        return Proposal(
            spec, used_llm=False, note="demo: deliberately incomplete first proposal"
        )
    try:
        spec = parse_llm_json(
            call_llm(_llm_prompt(system, signature, ctx, rejected, feedback or []))
        )
        return Proposal(spec, used_llm=True, note="proposed by the LLM")
    except LLMError as exc:
        spec = heuristic_spec(system, signature, ctx)
        return Proposal(spec, used_llm=False, note=f"heuristic fallback ({exc})")
