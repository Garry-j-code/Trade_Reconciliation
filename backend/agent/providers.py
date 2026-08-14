"""LLM providers: stub (tests / no Bedrock) and Amazon Bedrock Converse.

Live path uses boto3 ``bedrock-runtime`` in ``us-east-1`` with the default
credential chain (``AWS_PROFILE=trade-recon-8948`` is the project profile).
Tests must inject ``StubProvider`` — they never call Bedrock.
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass, field
from typing import Any, Protocol

logger = logging.getLogger(__name__)

DEFAULT_BEDROCK_REGION = "us-east-1"
# Default: Amazon Nova Lite (on-demand foundation model id for us-east-1).
# Cost-focused default; Converse + tool use works on the same BedrockProvider
# path. For later Claude comparison set
# BEDROCK_MODEL_ID=us.anthropic.claude-sonnet-4-5-20250929-v1:0 (Sonnet 4.5
# cross-region inference profile — pass as-is; do not strip ``us.``).
DEFAULT_BEDROCK_MODEL_ID = "amazon.nova-lite-v1:0"
DEFAULT_EMBED_MODEL_ID = "amazon.titan-embed-text-v1"
EMBEDDING_DIM = 1536
MAX_TOOL_CALLS = 5


class BedrockAccessError(RuntimeError):
    """Bedrock invoke denied, model not enabled, or credentials missing."""


@dataclass(frozen=True)
class ToolCall:
    name: str
    input: dict[str, Any]
    tool_use_id: str = "tooluse_stub"


@dataclass
class ProviderTurn:
    """One model turn: optional text plus zero or more tool calls."""

    text: str = ""
    tool_calls: list[ToolCall] = field(default_factory=list)
    raw: dict[str, Any] | None = None
    stop_reason: str = "end_turn"


class LLMProvider(Protocol):
    """Minimal converse-style interface used by the runner."""

    def converse(
        self,
        *,
        messages: list[dict[str, Any]],
        system: str,
        tools: list[dict[str, Any]] | None = None,
    ) -> ProviderTurn: ...


def bedrock_model_id(env: dict[str, str] | None = None) -> str:
    """Return ``BEDROCK_MODEL_ID`` unchanged (whitespace only).

    Inference-profile ids start with ``us.`` and must be passed to Converse
    ``modelId`` as-is. This helper never strips that prefix.
    """
    source = env if env is not None else os.environ
    return (source.get("BEDROCK_MODEL_ID") or DEFAULT_BEDROCK_MODEL_ID).strip()


def bedrock_embed_model_id(env: dict[str, str] | None = None) -> str:
    source = env if env is not None else os.environ
    return (source.get("BEDROCK_EMBED_MODEL_ID") or DEFAULT_EMBED_MODEL_ID).strip()


def bedrock_region(env: dict[str, str] | None = None) -> str:
    source = env if env is not None else os.environ
    return (
        (source.get("AWS_REGION") or "").strip()
        or (source.get("AWS_DEFAULT_REGION") or "").strip()
        or DEFAULT_BEDROCK_REGION
    )


def _access_error_message(exc: BaseException) -> str:
    """Human-readable IAM / model-access hint. Never includes secrets."""
    name = type(exc).__name__
    code = ""
    detail = ""
    if hasattr(exc, "response") and isinstance(exc.response, dict):
        err = exc.response.get("Error") or {}
        code = str(err.get("Code", ""))
        raw = str(err.get("Message") or "")
        # Keep the vendor hint (use-case form, model id) but drop anything that
        # looks like an ARN account path beyond the error class.
        detail = raw.split("http")[0].strip()
        if len(detail) > 240:
            detail = detail[:240] + "…"
    hint = (
        "Default model is amazon.nova-lite-v1:0 (no Anthropic use-case form). "
        "For Claude Sonnet 4.5 use the us.anthropic.… inference-profile id, "
        "enable model access, and submit Anthropic use-case details if prompted. "
        "Grant bedrock:InvokeModel plus bedrock:InvokeModelWithResponseStream. "
        "Override BEDROCK_MODEL_ID if needed."
    )
    label = f"{name}" + (f" ({code})" if code else "")
    extra = f" {detail}" if detail else ""
    return f"Bedrock invoke failed: {label}.{extra} {hint}"


class StubProvider:
    """Scripted LLM for tests and local runs without Bedrock.

    ``script`` is consumed one turn at a time. After it is exhausted, returns
    ``default_text`` (typically a JSON AgentOutput).
    """

    def __init__(
        self,
        script: list[ProviderTurn] | None = None,
        default_text: str = "",
        default_factory: Any | None = None,
    ) -> None:
        self.script = list(script or [])
        self.default_text = default_text
        self.default_factory = default_factory
        self.calls: list[dict[str, Any]] = []
        self._index = 0

    def converse(
        self,
        *,
        messages: list[dict[str, Any]],
        system: str,
        tools: list[dict[str, Any]] | None = None,
    ) -> ProviderTurn:
        self.calls.append(
            {"messages": messages, "system": system, "tools": tools or []}
        )
        if self._index < len(self.script):
            turn = self.script[self._index]
            self._index += 1
            return turn
        if self.default_factory is not None:
            text = self.default_factory(messages=messages, system=system, tools=tools)
            return ProviderTurn(text=str(text), stop_reason="end_turn")
        return ProviderTurn(text=self.default_text, stop_reason="end_turn")


class BedrockProvider:
    """Amazon Bedrock Converse API with tool use."""

    def __init__(
        self,
        *,
        model_id: str | None = None,
        region: str | None = None,
        client: Any | None = None,
        max_tokens: int = 4096,
        temperature: float = 0.0,
        read_timeout: int = 60,
    ) -> None:
        self.model_id = model_id or bedrock_model_id()
        self.region = region or bedrock_region()
        self.max_tokens = max_tokens
        self.temperature = temperature
        self._client = client
        self._read_timeout = read_timeout

    def _client_or_create(self) -> Any:
        if self._client is not None:
            return self._client
        import boto3  # noqa: PLC0415
        from botocore.config import Config  # noqa: PLC0415

        cfg = Config(
            read_timeout=self._read_timeout,
            connect_timeout=10,
            retries={"max_attempts": 2, "mode": "standard"},
        )
        self._client = boto3.client(
            "bedrock-runtime", region_name=self.region, config=cfg
        )
        return self._client

    def converse(
        self,
        *,
        messages: list[dict[str, Any]],
        system: str,
        tools: list[dict[str, Any]] | None = None,
    ) -> ProviderTurn:
        client = self._client_or_create()
        # Inference-profile id (us.anthropic.…) is a valid Converse modelId;
        # no ARN rewrite. Do not strip the geographic prefix.
        kwargs: dict[str, Any] = {
            "modelId": self.model_id,
            "messages": messages,
            "system": [{"text": system}],
            "inferenceConfig": {
                "maxTokens": self.max_tokens,
                "temperature": self.temperature,
            },
        }
        if tools:
            kwargs["toolConfig"] = {"tools": tools}
        try:
            response = client.converse(**kwargs)
        except Exception as exc:  # noqa: BLE001
            raise BedrockAccessError(_access_error_message(exc)) from exc
        return parse_converse_response(response)


def parse_converse_response(response: dict[str, Any]) -> ProviderTurn:
    """Normalize a Bedrock Converse payload into ``ProviderTurn``."""
    output = response.get("output") or {}
    message = output.get("message") or {}
    content = message.get("content") or []
    texts: list[str] = []
    tool_calls: list[ToolCall] = []
    for block in content:
        if not isinstance(block, dict):
            continue
        if "text" in block and block["text"]:
            texts.append(str(block["text"]))
        tool_use = block.get("toolUse")
        if isinstance(tool_use, dict):
            raw_input = tool_use.get("input") or {}
            if not isinstance(raw_input, dict):
                raw_input = {}
            tool_calls.append(
                ToolCall(
                    name=str(tool_use.get("name") or ""),
                    input=raw_input,
                    tool_use_id=str(tool_use.get("toolUseId") or "tooluse_unknown"),
                )
            )
    stop = str(response.get("stopReason") or "end_turn")
    return ProviderTurn(
        text="\n".join(texts).strip(),
        tool_calls=tool_calls,
        raw=response,
        stop_reason=stop,
    )


def make_tool_result_message(
    tool_calls: list[ToolCall],
    results: list[dict[str, Any]],
) -> dict[str, Any]:
    """Bedrock user message carrying toolResult blocks."""
    blocks: list[dict[str, Any]] = []
    for call, result in zip(tool_calls, results, strict=True):
        blocks.append(
            {
                "toolResult": {
                    "toolUseId": call.tool_use_id,
                    "content": [{"json": result}],
                    "status": "success" if not result.get("error") else "error",
                }
            }
        )
    return {"role": "user", "content": blocks}


def assistant_message_from_turn(turn: ProviderTurn) -> dict[str, Any]:
    """Rebuild an assistant message for the next Converse call."""
    if turn.raw and isinstance(turn.raw.get("output"), dict):
        message = turn.raw["output"].get("message")
        if isinstance(message, dict):
            return message
    content: list[dict[str, Any]] = []
    if turn.text:
        content.append({"text": turn.text})
    for call in turn.tool_calls:
        content.append(
            {
                "toolUse": {
                    "toolUseId": call.tool_use_id,
                    "name": call.name,
                    "input": call.input,
                }
            }
        )
    if not content:
        content.append({"text": ""})
    return {"role": "assistant", "content": content}


def provider_from_env(
    name: str | None = None,
    *,
    stub: StubProvider | None = None,
    env: dict[str, str] | None = None,
) -> LLMProvider:
    """``stub`` for tests; ``bedrock`` for live. Default is bedrock."""
    source = env if env is not None else os.environ
    chosen = (name or source.get("AGENT_LLM_PROVIDER") or "bedrock").strip().lower()
    if chosen in {"stub", "fake", "mock"}:
        return stub or StubProvider()
    if chosen in {"bedrock", "live"}:
        return BedrockProvider()
    raise ValueError(f"Unknown AGENT_LLM_PROVIDER {chosen!r} (use stub or bedrock)")


def stub_embedding(text: str, dim: int = EMBEDDING_DIM) -> list[float]:
    """Deterministic token-overlap vector for tests (no Bedrock).

    Hashing the whole string makes unrelated texts equally similar. Token
    hashing keeps notes that share words (e.g. AAPL / split) closer.
    """
    import hashlib
    import math
    import re

    values = [0.0] * dim
    tokens = re.findall(r"[A-Za-z0-9]+", text.lower()) or ["empty"]
    for token in tokens:
        digest = hashlib.sha256(token.encode("utf-8")).digest()
        for i, byte in enumerate(digest):
            values[i % dim] += (byte / 127.5) - 1.0
    norm = math.sqrt(sum(v * v for v in values)) or 1.0
    return [v / norm for v in values]


class Embedder(Protocol):
    def embed(self, text: str) -> list[float]: ...


class StubEmbedder:
    def embed(self, text: str) -> list[float]:
        return stub_embedding(text)


class BedrockEmbedder:
    """Titan embeddings (1536-d) for ``agent_memory.embedding``."""

    def __init__(
        self,
        *,
        model_id: str | None = None,
        region: str | None = None,
        client: Any | None = None,
    ) -> None:
        self.model_id = model_id or bedrock_embed_model_id()
        self.region = region or bedrock_region()
        self._client = client

    def _client_or_create(self) -> Any:
        if self._client is not None:
            return self._client
        import boto3  # noqa: PLC0415

        self._client = boto3.client("bedrock-runtime", region_name=self.region)
        return self._client

    def embed(self, text: str) -> list[float]:
        client = self._client_or_create()
        body = json.dumps({"inputText": text[:8000]})
        try:
            response = client.invoke_model(
                modelId=self.model_id,
                contentType="application/json",
                accept="application/json",
                body=body,
            )
        except Exception as exc:  # noqa: BLE001
            raise BedrockAccessError(_access_error_message(exc)) from exc
        payload = json.loads(response["body"].read())
        vector = payload.get("embedding")
        if not isinstance(vector, list):
            raise BedrockAccessError("Bedrock embedding response missing 'embedding'")
        return [float(x) for x in vector]


def embedder_from_env(
    name: str | None = None,
    *,
    env: dict[str, str] | None = None,
) -> Embedder:
    source = env if env is not None else os.environ
    chosen = (name or source.get("AGENT_EMBED_PROVIDER") or "bedrock").strip().lower()
    if chosen in {"stub", "fake", "mock"}:
        return StubEmbedder()
    return BedrockEmbedder()
