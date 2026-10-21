"""The LLM features: each builds a prompt, calls the provider and parses the answer."""
from __future__ import annotations

import re
import time
from dataclasses import dataclass
from typing import Iterable

from . import prompts as p
from .provider import LLMProvider


@dataclass
class Line:
    """A chat line as the LLM sees it."""
    nick: str
    text: str
    kind: str = "msg"
    ts: float = 0.0

    def render(self) -> str:
        stamp = time.strftime("%H:%M", time.localtime(self.ts)) if self.ts else ""
        body = f"* {self.nick} {self.text}" if self.kind == "action" else f"<{self.nick}> {self.text}"
        return f"[{stamp}] {body}" if stamp else body


def render_log(lines: Iterable[Line]) -> str:
    return "\n".join(line.render() for line in lines) or "(no messages yet)"


_NUMBERING = re.compile(r"^\s*(?:\d+[.):]|[-*•])\s*")


def parse_options(text: str, own_nick: str, limit: int) -> list[str]:
    """Pull reply options out of a model answer, tolerating different list styles."""
    options = []
    for raw in text.splitlines():
        line = _NUMBERING.sub("", raw).strip()
        if not line or line.endswith(":") and len(line) < 40:
            continue  # blank or a heading like "Here are some replies:"
        line = re.sub(rf"^<?{re.escape(own_nick)}>?:?\s+", "", line, flags=re.IGNORECASE)
        line = line.strip().strip('"“”').strip()
        if line and line not in options:
            options.append(line)
    return options[:limit]


async def suggest(llm: LLMProvider, *, nick: str, channel: str, topic: str,
                  lines: list[Line], n: int, draft: str = "",
                  profiles: dict[str, str] | None = None) -> list[str]:
    profile_text = ""
    if profiles:
        sketches = "\n".join(f"- {who}: {sketch}" for who, sketch in profiles.items())
        profile_text = f"What you know about the people talking:\n{sketches}\n"
    draft_text = f'{nick} has started typing: "{draft}". Build on it.\n' if draft else ""
    answer = await llm.chat(
        p.SUGGEST_SYSTEM.format(nick=nick, n=n),
        p.SUGGEST_USER.format(channel=channel, topic=topic or "(none)", profiles=profile_text,
                              log=render_log(lines), draft=draft_text, nick=nick, n=n),
        max_tokens=60 * n + 40)
    return parse_options(answer, nick, n)


async def summarize(llm: LLMProvider, *, nick: str, channel: str, lines: list[Line]) -> str:
    return await llm.chat(
        p.SUMMARY_SYSTEM.format(nick=nick),
        p.SUMMARY_USER.format(channel=channel, nick=nick, log=render_log(lines)),
        max_tokens=400, temperature=0.3)


async def rephrase(llm: LLMProvider, *, draft: str, style: str, lines: list[Line]) -> str:
    if style.startswith("lang:"):
        style_text = f"translate it into {style[5:]}, natural chat register"
    else:
        style_text = p.REPHRASE_STYLES.get(style, style)
    answer = await llm.chat(
        p.REPHRASE_SYSTEM.format(style=style_text),
        p.REPHRASE_USER.format(log=render_log(lines[-8:]), draft=draft),
        max_tokens=200, temperature=0.4)
    first = next((line for line in answer.splitlines() if line.strip()), draft)
    return first.strip().strip('"“”').strip()


async def triage(llm: LLMProvider, *, nick: str, context: list[Line],
                 candidates: list[Line]) -> list[int]:
    """Indexes into `candidates` of messages that need the user's attention."""
    numbered = "\n".join(f"{i + 1}. {line.render()}" for i, line in enumerate(candidates))
    answer = await llm.chat(
        p.TRIAGE_SYSTEM.format(nick=nick),
        p.TRIAGE_USER.format(context=render_log(context), numbered=numbered, nick=nick),
        max_tokens=40, temperature=0.0)
    if "none" in answer.lower():
        return []
    picked = {int(n) - 1 for n in re.findall(r"\d+", answer)}
    return sorted(i for i in picked if 0 <= i < len(candidates))


async def translate(llm: LLMProvider, *, text: str, target: str) -> str | None:
    """The translation, or None when the text is already in the target language."""
    answer = await llm.chat(p.TRANSLATE_SYSTEM.format(target=target), text,
                            max_tokens=200, temperature=0.2)
    answer = answer.strip()
    if not answer or answer.upper().startswith("SAME") or answer == text:
        return None
    return answer


async def update_profile(llm: LLMProvider, *, nick: str, previous: str,
                         lines: list[Line]) -> str:
    return await llm.chat(
        p.PROFILE_SYSTEM,
        p.PROFILE_USER.format(nick=nick, previous=previous or "(none yet)",
                              log=render_log(lines)),
        max_tokens=250, temperature=0.3)


# --- Cheap, local pre-filters --------------------------------------------------------

_COMMON_EN = {"the", "and", "is", "are", "you", "to", "it", "that", "of", "in", "for",
              "this", "what", "i", "a", "not", "have", "be", "do", "on", "with", "lol",
              "yes", "no", "ok", "my", "but", "so", "just", "can", "was", "how", "why"}
# Frequent function words in Spanish, French, German, Italian, Portuguese, Dutch.
_COMMON_FOREIGN = {"el", "los", "las", "es", "por", "para", "qué", "que", "está", "pero",
                   "todos", "y", "le", "les", "est", "et", "je", "pas", "vous", "avec",
                   "der", "die", "das", "und", "ist", "nicht", "ich", "mit", "auch", "ein",
                   "eine", "che", "sono", "non", "della", "não", "uma", "com", "het", "een",
                   "niet", "zijn", "ik"}


def looks_foreign(text: str, target: str) -> bool:
    """Cheap guess at whether a line is worth sending to the translator.

    Only meaningful for an English target. For other targets every line with real
    words is a candidate, and the model answers SAME for ones that need no work.
    """
    words = re.findall(r"[^\W\d_]+", text.lower())
    if len(words) < 2:
        return False
    if target.lower() not in ("english", "en"):
        return True
    non_ascii = sum(1 for ch in text if ord(ch) > 127 and ch.isalpha())
    if non_ascii / max(1, sum(ch.isalpha() for ch in text)) > 0.3:
        return True
    english = sum(1 for w in words if w in _COMMON_EN)
    foreign = sum(1 for w in words if w in _COMMON_FOREIGN)
    if foreign > english:
        return True
    return len(words) >= 4 and english == 0


def mentions(text: str, nick: str, keywords: list[str]) -> bool:
    """Whole-word match on our nick or any keyword."""
    for word in [nick, *keywords]:
        if word and re.search(rf"(?<![\w-]){re.escape(word)}(?![\w-])", text, re.IGNORECASE):
            return True
    return False


def looks_like_question(text: str) -> bool:
    return "?" in text or bool(re.match(
        r"(?i)^(anyone|any1|does|do|can|could|would|is|are|how|what|why|where|when|who)\b",
        text.strip()))
