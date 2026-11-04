"""Local LLM client abstraction.

Talks to any OpenAI-compatible `/v1/chat/completions` endpoint — Ollama,
LM Studio, llama.cpp server, or vLLM. No cloud dependency. All model calls
are local.

JSON mode is negotiated adaptively because backends differ:
  * OpenAI / Ollama   -> accept `json_object` (and Ollama's `format:"json"`).
  * LM Studio         -> only accepts `json_schema` (or `text`); rejects `json_object`.
The client tries `json_schema` (strict) first, then `json_object`, then falls
back to "no response_format + a JSON instruction in the prompt", so the same
code works against every local backend.
"""
from __future__ import annotations

import json
import re
import urllib.request
import urllib.error
from dataclasses import dataclass


@dataclass
class ChatMessage:
    role: str
    content: str


class _HTTPJSONError(Exception):
    def __init__(self, code: int, body: str):
        super().__init__(f"HTTP {code}: {body[:200]}")
        self.code = code
        self.body = body


class LLMClient:
    """Thin, dependency-free client for an OpenAI-compatible local endpoint."""

    def __init__(self, endpoint: str, model: str, api_key: str = "lmstudio", timeout_ms: int = 300000):
        self.endpoint = endpoint.rstrip("/")
        self.model = model
        self.api_key = api_key
        self.timeout = timeout_ms / 1000.0

    # ---- liveness / model discovery -------------------------------------
    def healthcheck(self) -> bool:
        # NOTE: `list_models()` swallows exceptions and returns [] on failure, so
        # we must validate the HTTP response status directly — otherwise an
        # unreachable endpoint would be reported as "healthy" (silent degradation).
        try:
            url = f"{self.endpoint}/models"
            with urllib.request.urlopen(urllib.request.Request(url, headers=self._headers()), timeout=10) as resp:
                return resp.status == 200
        except Exception:
            return False

    def list_models(self) -> list[str]:
        url = f"{self.endpoint}/models"
        try:
            with urllib.request.urlopen(urllib.request.Request(url, headers=self._headers()), timeout=10) as resp:
                body = json.loads(resp.read().decode("utf-8"))
            return [m.get("id") for m in body.get("data", [])]
        except Exception:
            return []

    # ---- core chat ------------------------------------------------------
    def _headers(self) -> dict:
        return {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {self.api_key}",
        }

    @staticmethod
    def _extract_json(content: str) -> dict:
        content = (content or "").strip()
        m = re.search(r"```(?:json)?\s*(.*?)```", content, re.DOTALL)
        if m:
            content = m.group(1).strip()
        try:
            return json.loads(content)
        except Exception:
            s, e = content.find("{"), content.rfind("}")
            if s != -1 and e != -1 and e > s:
                try:
                    return json.loads(content[s:e + 1])
                except Exception:
                    pass
            raise ValueError(f"模型未返回可解析的 JSON: {content[:200]}")

    def _post(self, payload: dict) -> str:
        data = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(
            f"{self.endpoint}/chat/completions", data=data, headers=self._headers(), method="POST")
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                body = json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            raise _HTTPJSONError(e.code, e.read().decode("utf-8", "replace"))
        return body["choices"][0]["message"]["content"]

    @staticmethod
    def _response_candidates(schema: dict | None) -> list[dict | None]:
        if schema is not None:
            return [
                {"type": "json_schema", "json_schema": {"name": "rule", "schema": schema, "strict": True}},
                {"type": "json_object"},
                None,  # no response_format; rely on `format:"json"` + prompt
            ]
        return [
            {"type": "json_object"},
            {"type": "json_schema", "json_schema": {"name": "obj", "schema": {"type": "object", "additionalProperties": True}, "strict": False}},
            None,
        ]

    @staticmethod
    def _as_api_messages(messages) -> list[dict]:
        out = []
        for m in messages:
            if isinstance(m, dict):
                out.append({"role": m["role"], "content": m["content"]})
            else:
                out.append({"role": m.role, "content": m.content})
        return out

    def chat(self, messages: list[ChatMessage], *, temperature: float = 0.0,
             max_tokens: int = 2048, json_mode: bool = False, response_schema: dict | None = None) -> str:
        base = {
            "model": self.model,
            "messages": self._as_api_messages(messages),
            "temperature": temperature,
            "max_tokens": max_tokens,
            "stream": False,
        }
        if not json_mode:
            return self._post(base)

        # JSON mode: try a chain of response_format options for backend compat.
        base["format"] = "json"  # Ollama native; ignored by others
        last_err: Exception | None = None
        for rf in self._response_candidates(response_schema):
            payload = dict(base)
            msgs = [dict(m) for m in base["messages"]]
            if rf is not None:
                payload["response_format"] = rf
            else:
                # Last resort: no response_format; push the constraint into the prompt.
                suffix = "\n只输出符合以下 JSON Schema 的 JSON 对象，不要任何解释或 Markdown 代码块:\n"
                suffix += json.dumps(response_schema if response_schema else {"type": "object"}, ensure_ascii=False)
                msgs[-1] = {"role": "user", "content": msgs[-1]["content"] + suffix}
            payload["messages"] = msgs
            try:
                return self._post(payload)
            except _HTTPJSONError as e:
                if e.code == 400 and "response_format" in e.body:
                    last_err = e
                    continue
                raise
        raise last_err or RuntimeError("json_mode 协商失败")

    def chat_json(self, messages: list[ChatMessage], response_schema: dict | None = None,
                  temperature: float = 0.0) -> dict:
        content = self.chat(messages, temperature=temperature, json_mode=True, response_schema=response_schema)
        return self._extract_json(content)
