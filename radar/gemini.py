"""Gemini backend (Google Gen AI SDK, `google-genai`). Used when RADAR_AI_PROVIDER picks Gemini.

Covers the same four jobs as the Claude path: batch event scoring with a strict JSON schema, plain-text briefs,
screenshot vision, and the Ask-the-Radar tool loop. Every function returns None on failure or a safety block so
callers fall back exactly as they do for Claude.
"""
from __future__ import annotations
import json
import logging
from .config import settings

log = logging.getLogger("radar.gemini")

_client = None
_BLOCKED = {"SAFETY", "PROHIBITED_CONTENT", "BLOCKLIST", "SPII", "RECITATION", "IMAGE_SAFETY"}


def client():
    global _client
    if _client is None:
        from google import genai
        from google.genai import types
        _client = genai.Client(api_key=settings.gemini_key,
                               http_options=types.HttpOptions(timeout=120_000, retry_options=types.HttpRetryOptions(attempts=3)))
    return _client


def _generate(contents, effort: str | None = None, **config):
    """generate_content with an optional thinking level. Models without thinking levels (2.5 and older) reject
    the field, so on that error the call is retried once without it."""
    from google.genai import types
    if effort:
        config["thinking_config"] = types.ThinkingConfig(thinking_level=effort.upper())
    try:
        return client().models.generate_content(model=settings.gemini_model, contents=contents,
                                                config=types.GenerateContentConfig(**config))
    except Exception as e:  # noqa: BLE001
        if not effort or "thinking" not in str(e).lower():
            raise
        config.pop("thinking_config")
        return client().models.generate_content(model=settings.gemini_model, contents=contents,
                                                config=types.GenerateContentConfig(**config))


def _blocked(resp) -> bool:
    if resp.prompt_feedback and resp.prompt_feedback.block_reason:
        return True
    cand = resp.candidates[0] if resp.candidates else None
    return cand is None or (cand.finish_reason is not None and cand.finish_reason.name in _BLOCKED)


def json_call(system: str, prompt: str, schema: dict, max_tokens: int = 8000) -> dict | None:
    """Structured output: the reply is guaranteed to parse against `schema`."""
    try:
        resp = _generate(prompt, effort="low", system_instruction=system, max_output_tokens=max_tokens,
                         response_mime_type="application/json", response_json_schema=schema)
        if _blocked(resp):
            return None
        return json.loads(resp.text)
    except Exception as e:  # noqa: BLE001
        log.warning("Gemini JSON call failed: %s", e)
        return None


def text(prompt: str, system: str, max_tokens: int = 4000) -> str | None:
    try:
        resp = _generate(prompt, system_instruction=system, max_output_tokens=max_tokens)
        return None if _blocked(resp) else (resp.text or None)
    except Exception as e:  # noqa: BLE001
        log.warning("Gemini text call failed: %s", e)
        return None


def vision(prompt: str, images: list[bytes], mime: str = "image/png") -> str | None:
    from google.genai import types
    try:
        resp = _generate([types.Part.from_bytes(data=b, mime_type=mime) for b in images] + [prompt],
                         effort="low", max_output_tokens=1500)
        return None if _blocked(resp) else (resp.text or "").strip() or None
    except Exception as e:  # noqa: BLE001
        log.info("Gemini vision failed: %s", e)
        return None


def chat(system: str, tools: list[dict], question: str, history: list[dict] | None, run_tool) -> str | None:
    """Manual function-calling loop over the radar's own tools (same TOOLS list Claude uses).

    The model's turns are appended back unchanged so Gemini 3 thought signatures survive between steps.
    """
    from google.genai import types
    decls = [types.FunctionDeclaration(name=t["name"], description=t["description"],
                                       parameters_json_schema=t["input_schema"] if t["input_schema"].get("properties") else None)
             for t in tools]
    config = dict(system_instruction=system, max_output_tokens=4000,
                  tools=[types.Tool(function_declarations=decls)],
                  automatic_function_calling=types.AutomaticFunctionCallingConfig(disable=True))
    contents = [types.Content(role="model" if m["role"] == "assistant" else "user", parts=[types.Part.from_text(text=m["content"])])
                for m in (history or []) if isinstance(m.get("content"), str)]
    contents.append(types.Content(role="user", parts=[types.Part.from_text(text=question)]))
    try:
        for _ in range(8):
            resp = _generate(contents, **config)
            if _blocked(resp) or not resp.candidates[0].content:
                return None
            contents.append(resp.candidates[0].content)
            calls = resp.function_calls or []
            if not calls:
                break
            parts = []
            for fc in calls:
                try:
                    out = {"result": json.dumps(run_tool(fc.name, dict(fc.args or {})), default=str)[:60000]}
                except Exception as e:  # noqa: BLE001
                    out = {"error": str(e)}
                parts.append(types.Part(function_response=types.FunctionResponse(id=fc.id, name=fc.name, response=out)))
            contents.append(types.Content(role="user", parts=parts))
        return resp.text or ""
    except Exception as e:  # noqa: BLE001
        log.warning("Gemini chat failed: %s", e)
        return f"Gemini call failed: {str(e)[:300]}"
