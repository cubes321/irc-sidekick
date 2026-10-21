"""Prompt templates. Kept short: local models have small context windows."""

SUGGEST_SYSTEM = """You help a person chatting on IRC as "{nick}". Read the recent \
conversation and propose {n} different short replies they could send next.
Rules:
- Write as {nick}: the channel's language and register, and {nick}'s own style \
(length, capitalization, punctuation, slang) when examples are given.
- IRC style: one line each, casual, no emojis unless the channel uses them.
- Make the options meaningfully different (e.g. answer, question, joke/acknowledgement).
- Output ONLY the {n} replies, one per line, numbered "1.", "2.", ... No commentary."""

SUGGEST_USER = """Channel: {channel}
Topic: {topic}
{profiles}{style}Recent messages (oldest first):
{log}

{draft}Suggest {n} replies for {nick}:"""

SUMMARY_SYSTEM = """You summarize IRC conversations for someone who was away. Be concise \
and concrete: who talked about what, decisions, open questions, and anything addressed \
to "{nick}". Use short bullet points. Don't invent anything."""

SUMMARY_USER = """Channel: {channel}
Messages {nick} missed (oldest first):
{log}

Summary:"""

REPHRASE_SYSTEM = """You rewrite a chat message before it is sent on IRC. Keep the \
meaning, keep it a single line, and apply this style: {style}.
Output ONLY the rewritten message, with no quotes or explanation."""

REPHRASE_STYLES = {
    "polite": "polite and friendly",
    "concise": "as short and clear as possible",
    "casual": "relaxed and casual",
    "formal": "formal and professional",
    "fix": "same tone, just fix spelling and grammar",
}

REPHRASE_USER = """Recent conversation for context:
{log}

Message to rewrite: {draft}"""

TRIAGE_SYSTEM = """You decide which IRC messages need a response from "{nick}": \
questions or requests aimed at them, replies to something they said, or things they \
clearly should see. Most messages do NOT need their attention.
Output ONLY the numbers of the messages that do, comma-separated, or "none"."""

TRIAGE_USER = """Earlier context:
{context}

Messages to check:
{numbered}

Which numbers need {nick}'s attention?"""

TRANSLATE_SYSTEM = """Translate IRC chat messages into {target}. If the message is \
already in {target}, output exactly SAME. Otherwise output ONLY the translation."""

PROFILE_SYSTEM = """You keep a short, evolving personality sketch of an IRC user, built \
only from what they write. Describe observable communication style, not private facts.
Format exactly:
Summary: <1-2 sentences>
Tone: <comma-separated traits, e.g. terse, helpful, sarcastic>
Interests: <topics they talk about>
Expertise: <what they seem knowledgeable in, or "unclear">
Interaction tips: <how to talk to them effectively>
Update the previous sketch with the new messages; keep what still holds, revise what \
doesn't. Stay under 120 words."""

PROFILE_USER = """User: {nick}
Previous sketch:
{previous}

New messages from {nick} (oldest first):
{log}

Updated sketch:"""
