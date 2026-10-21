"""Configuration loaded from a TOML file."""
from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass, field, fields
from pathlib import Path


def read_env(name: str) -> str | None:
    """An environment variable, falling back to the Windows per-user store.

    `setx NAME value` only reaches terminals opened afterwards, so a client started
    from an older window would otherwise not see the password at all.
    """
    if not name:
        return None
    value = os.environ.get(name)
    if value is None and os.name == "nt":
        import winreg
        try:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, "Environment") as key:
                value, _ = winreg.QueryValueEx(key, name)
        except OSError:
            value = None
    return value or None


@dataclass
class NetworkConfig:
    name: str
    host: str
    port: int = 6697
    tls: bool = True
    tls_verify: bool = True
    nick: str = "llmirc"
    alt_nicks: list[str] = field(default_factory=list)
    username: str = ""
    realname: str = "LLM IRC Client"
    channels: list[str] = field(default_factory=list)
    server_password_env: str = ""
    # Services login: "nickserv", "q" (QuakeNet CHALLENGEAUTH) or "" for none.
    auth: str = ""
    auth_username: str = ""        # account name (defaults to nick)
    auth_password_env: str = ""    # env var holding the password
    nickserv_password_env: str = ""  # shorthand for auth = "nickserv"
    hide_host: bool = False        # set user mode +x and join only once the host is hidden
    auth_timeout_s: float = 15.0   # join anyway if login/host hiding takes longer

    def __post_init__(self) -> None:
        if self.nickserv_password_env and not self.auth:
            self.auth = "nickserv"
            self.auth_password_env = self.auth_password_env or self.nickserv_password_env
        self.auth = self.auth.lower()
        if self.auth not in ("", "nickserv", "q"):
            raise ValueError(f"[networks] {self.name}: auth must be 'nickserv', 'q' or ''")

    @property
    def server_password(self) -> str | None:
        return read_env(self.server_password_env)

    @property
    def auth_password(self) -> str | None:
        return read_env(self.auth_password_env)

    @property
    def auth_account(self) -> str:
        return self.auth_username or self.nick


@dataclass
class LLMConfig:
    enabled: bool = True
    base_url: str = "http://localhost:1234/v1"
    api_key: str = "lm-studio"
    model: str = ""  # empty: use whatever model LM Studio has loaded
    max_concurrency: int = 3
    context_lines: int = 25
    suggest_debounce_s: float = 2.0
    temperature: float = 0.7
    timeout_s: float = 60.0
    reasoning_effort: str = "none"  # "" to leave the model's default thinking on


@dataclass
class FeatureConfig:
    auto_suggest: bool = True
    suggestion_count: int = 3
    summary_threshold: int = 50
    triage_keywords: list[str] = field(default_factory=list)
    llm_triage: bool = True
    translate_target: str = "English"
    profiles: bool = True
    profile_batch: int = 30
    style_examples: int = 8


@dataclass
class Config:
    networks: list[NetworkConfig] = field(default_factory=list)
    llm: LLMConfig = field(default_factory=LLMConfig)
    features: FeatureConfig = field(default_factory=FeatureConfig)
    db_path: str = "llm_irc.sqlite3"


def _build(cls, data: dict, section: str):
    known = {f.name for f in fields(cls)}
    unknown = set(data) - known
    if unknown:
        raise ValueError(f"unknown key(s) in [{section}]: {', '.join(sorted(unknown))}")
    return cls(**data)


def load(path: str | Path) -> Config:
    path = Path(path)
    with path.open("rb") as fh:
        raw = tomllib.load(fh)

    networks = [_build(NetworkConfig, n, "networks") for n in raw.get("networks", [])]
    if not networks:
        raise ValueError(f"{path}: define at least one [[networks]] entry")
    db_path = raw.get("db_path", "llm_irc.sqlite3")
    if not Path(db_path).is_absolute():
        db_path = str(path.parent / db_path)
    return Config(
        networks=networks,
        llm=_build(LLMConfig, raw.get("llm", {}), "llm"),
        features=_build(FeatureConfig, raw.get("features", {}), "features"),
        db_path=db_path,
    )
