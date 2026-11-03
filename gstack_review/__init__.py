"""GStack Local AI Code Review System.

A local, rule-driven, multi-module code review system backed by a local LLM
(Ollama / llama.cpp / vLLM — any OpenAI-compatible /v1/chat/completions endpoint).

Key capabilities (mapped to the original requirements):
  1. Local AI model only (no cloud APIs).
  2. Large projects: multi-file / multi-module with a cross-module call & dependency graph.
  3. Custom rules via natural language (compiled by the local LLM into structured rules).
  4. Rule-based checking -> rule-structured reports.
  5. Built-in standards: CERT C (and CERT C++ pack scaffold).
"""

__version__ = "0.1.0"
