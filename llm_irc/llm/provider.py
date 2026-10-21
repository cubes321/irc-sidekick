"""OpenAI-compatible chat client pointed at LM Studio."""
from __future__ import annotations

import httpx
from openai import AsyncOpenAI

from ..config import LLMConfig


class LLMUnavailable(RuntimeError):
    pass


class LLMProvider:
    def __init__(self, cfg: LLMConfig) -> None:
        self.cfg = cfg
        self.client = AsyncOpenAI(base_url=cfg.base_url, api_key=cfg.api_key,
                                  timeout=cfg.timeout_s, max_retries=0)
        self.model: str | None = cfg.model or None

    async def discover(self) -> str:
        """Find the model to use: the configured one, else the one LM Studio has loaded.

        LM Studio's /v1/models lists every *downloaded* model (it loads them on demand),
        so picking from it could trigger a slow load of the wrong model. Its native
        /api/v0/models endpoint reports which ones are actually loaded.
        """
        try:
            models = await self.client.models.list()
        except Exception as exc:  # connection refused, timeout, ...
            raise LLMUnavailable(f"LM Studio not reachable at {self.cfg.base_url}: {exc}") from exc
        ids = [model.id for model in models.data]
        if self.cfg.model:
            if self.cfg.model not in ids:
                raise LLMUnavailable(
                    f"model {self.cfg.model!r} not found; available: {', '.join(ids) or 'none'}")
            self.model = self.cfg.model
            return self.model

        loaded = await self._loaded_models()
        if loaded:
            self.model = loaded[0]
        else:
            # Not LM Studio (or an old version): fall back to the first chat-capable model.
            chat = [i for i in ids if "embed" not in i.lower()]
            if not chat:
                raise LLMUnavailable("no chat model available")
            self.model = chat[0]
        return self.model

    async def _loaded_models(self) -> list[str]:
        root = self.cfg.base_url.rstrip("/").removesuffix("/v1")
        try:
            async with httpx.AsyncClient(timeout=5) as http:
                resp = await http.get(f"{root}/api/v0/models")
                resp.raise_for_status()
                data = resp.json().get("data", [])
        except (httpx.HTTPError, ValueError):
            return []
        return [m["id"] for m in data
                if m.get("state") == "loaded" and m.get("type") in ("llm", "vlm")]

    async def chat(self, system: str, user: str, *, max_tokens: int = 300,
                   temperature: float | None = None) -> str:
        if self.model is None:
            await self.discover()
        # Reasoning models (Qwen3 etc.) otherwise spend the whole token budget thinking.
        extra = {"reasoning_effort": self.cfg.reasoning_effort} if self.cfg.reasoning_effort else {}
        resp = await self.client.chat.completions.create(
            model=self.model,
            messages=[{"role": "system", "content": system},
                      {"role": "user", "content": user}],
            max_tokens=max_tokens,
            temperature=self.cfg.temperature if temperature is None else temperature,
            extra_body=extra or None,
        )
        choice = resp.choices[0]
        content = strip_reasoning(choice.message.content or "")
        if not content and getattr(choice.message, "reasoning_content", None):
            raise RuntimeError("the model used its whole token budget on reasoning; "
                               "set llm.reasoning_effort = \"none\" or load a non-reasoning model")
        return content

    async def close(self) -> None:
        await self.client.close()


def strip_reasoning(text: str) -> str:
    """Remove <think>...</think> blocks that reasoning models emit before the answer."""
    while "<think>" in text:
        start = text.index("<think>")
        end = text.find("</think>", start)
        if end == -1:
            text = text[:start]
            break
        text = text[:start] + text[end + len("</think>"):]
    return text.strip()
