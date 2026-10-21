# LLM IRC

A terminal IRC client (Python + [Textual](https://textual.textualize.io/)) with a local
LLM (LM Studio) working in the background.

- Tabs per network, channel and private chat; `/join`, `/part`, `/close` from inside the client
- **Suggested replies**: after the channel goes quiet for a moment, 3 replies in the channel's
  tone appear above the input. `Tab` cycles them into the input, `F6`/`F7`/`F8` (or `Alt+1..3`) picks one. Nothing
  is sent until you press Enter. `Ctrl+Space`/`F2` asks right away and builds on your draft.
- **Catch-up summary**: `F5` or `/summary` summarizes what you missed in a tab
- **Rephrase**: `Ctrl+R` makes your draft polite; `/rephrase concise|casual|formal|fix|lang:German`.
  `Ctrl+Z` undoes it.
- **Mention triage**: your nick and `triage_keywords` highlight instantly. In background tabs the
  LLM also flags questions that seem aimed at you even without your nick.
- **Translation**: `/translate on` in a tab adds translations under foreign-language lines
- **User profiles**: every `profile_batch` messages, the LLM updates a short sketch of each user's
  style, interests and expertise. View it with `/profile nick`; profiles also make suggestions
  fit the person you're replying to. Stored only in your local SQLite file; `/forget nick` deletes
  one, and `features.profiles = false` turns the feature off.

## Setup

```bash
pip install -e .[dev]
cp config.example.toml config.toml   # then set your nick and channels
python -m llm_irc                    # or: python -m llm_irc -c other.toml
```

Start LM Studio's local server (default `http://localhost:1234`) with a chat model loaded.
The client uses whichever model is **loaded**, unless `llm.model` names one. If LM Studio is
not running, IRC still works; `/llm` reconnects later.

### Logging in to services

Passwords are read from environment variables named in the config, never from the file.
On Windows, `setx QUAKENET_PASS "yourpassword"` stores one; open a new terminal afterwards.

- **NickServ** (Libera, OFTC, ...): `auth = "nickserv"`
- **QuakeNet Q**: `auth = "q"`, `auth_username = "<Q account>"`, usually with `hide_host = true`.
  The client uses Q's CHALLENGEAUTH (HMAC-SHA-256), so your password never crosses the
  network, which matters because QuakeNet has no TLS. With `hide_host`, it sets `+x` and
  waits until the server confirms your hidden host before joining, so channels never see
  your real address. If login fails or takes longer than `auth_timeout_s`, it reports this
  and joins anyway.

Use Windows Terminal (or any modern terminal); the legacy Windows console renders Textual poorly.

## How the LLM work is scheduled

All features share LM Studio's parallel slots (`llm.max_concurrency`, default 3) through one
priority queue (`llm_irc/llm/scheduler.py`):

`explicit requests > auto-suggest > triage > translation > profiling`

Background jobs may use at most `max_concurrency - 1` slots, so one is always free for
something you're waiting on. A new message cancels an outdated suggestion that is still
being generated. Cheap local checks (regex, a stopword language guess, question detection)
decide what is worth sending to the model at all.

`llm.reasoning_effort = "none"` (the default) turns off "thinking" in reasoning models such as
Qwen3. Without it they can spend the whole token budget reasoning and return nothing.

## Tests

```bash
python -m pytest
```

Includes an in-process fake IRC server (`tests/fake_ircd.py`, also runnable as
`python -m tests.fake_ircd 6667` for manual testing with `tls = false`) and a fake LLM.

## License

MIT, see [LICENSE](LICENSE).
