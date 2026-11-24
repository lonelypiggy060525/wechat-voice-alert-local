"""Configuration store for WeChat Voice Alert."""
from __future__ import annotations

import json
import os
from dataclasses import dataclass, field, asdict
from typing import Dict, List

APP_NAME = "WeChatDBAlert"
CONFIG_DIR = os.path.join(os.environ.get("APPDATA", os.path.expanduser("~")), APP_NAME)
CONFIG_PATH = os.path.join(CONFIG_DIR, "config.json")
LOG_PATH = os.path.join(CONFIG_DIR, "last_messages.log")


@dataclass
class Config:
    enabled: bool = True
    # sound
    sound_enabled: bool = True
    sound_path: str = "builtin:bell_soft"   # builtin:<name> or absolute file path
    sound_volume: float = 0.85              # 0.0 - 1.0
    custom_sounds: List[Dict[str, str]] = field(default_factory=list)  # [{"name","path"}] library
    # speech
    tts_enabled: bool = True
    tts_template: str = "{sender}说：{content}"
    tts_voice: str = ""                     # empty = auto pick best Chinese voice
    tts_rate: int = 0                       # -10 .. 10
    tts_volume: int = 100                   # 0 .. 100
    max_content_chars: int = 90
    # filtering: one persistent switch per contact, plus the default for unlisted ones
    default_announce: bool = True
    contact_rules: Dict[str, bool] = field(default_factory=dict)   # name -> announce?
    skip_when_wechat_focused: bool = True
    auto_restore_window: bool = True       # keep WeChat un-minimized so the list stays readable
    stealth_mode: bool = False             # park the WeChat window outside the screen
    full_read: bool = True                 # open long messages to speak the whole text
    # appearance
    theme: str = "midnight"                 # midnight | cream | mist
    # hotkey
    hotkey: str = "ctrl+alt+q"
    # runtime
    poll_interval_ms: int = 600
    show_debug_log: bool = False
    # legacy fields, kept only so old config files migrate cleanly
    filter_mode: str = "all"
    whitelist: List[str] = field(default_factory=list)
    blacklist: List[str] = field(default_factory=list)

    def __post_init__(self) -> None:
        # migrate the old whitelist/blacklist model into per-contact switches
        if not self.contact_rules:
            rules: Dict[str, bool] = {}
            for name in self.whitelist:
                if name.strip():
                    rules[name.strip()] = True
            for name in self.blacklist:
                if name.strip():
                    rules[name.strip()] = False
            if rules:
                self.contact_rules = rules
            if self.filter_mode == "whitelist":
                self.default_announce = False
            elif self.filter_mode == "blacklist":
                self.default_announce = True
        self.contact_rules = {str(k): bool(v) for k, v in self.contact_rules.items() if str(k).strip()}
        # keep a picked single file in the reusable library too
        self.custom_sounds = [dict(item) for item in self.custom_sounds
                              if isinstance(item, dict) and item.get("path")]
        if self.sound_path and not self.sound_path.startswith("builtin:"):
            if not any(item["path"] == self.sound_path for item in self.custom_sounds):
                self.custom_sounds.append({
                    "name": os.path.splitext(os.path.basename(self.sound_path))[0],
                    "path": self.sound_path,
                })

    def save(self) -> None:
        os.makedirs(CONFIG_DIR, exist_ok=True)
        data = asdict(self)
        for legacy in ("filter_mode", "whitelist", "blacklist"):
            data.pop(legacy, None)   # migrated into contact_rules; don't write stale copies
        tmp = CONFIG_PATH + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        os.replace(tmp, CONFIG_PATH)

    @classmethod
    def load(cls) -> "Config":
        try:
            with open(CONFIG_PATH, "r", encoding="utf-8") as f:
                data = json.load(f)
        except (OSError, ValueError):
            return cls()
        known = {k: v for k, v in data.items() if k in cls.__dataclass_fields__}
        if not isinstance(known.get("contact_rules"), dict):
            known.pop("contact_rules", None)
        cfg = cls(**known)
        if cfg.filter_mode not in ("all", "whitelist", "blacklist"):
            cfg.filter_mode = "all"
        return cfg


def append_log(text: str) -> None:
    try:
        os.makedirs(CONFIG_DIR, exist_ok=True)
        with open(LOG_PATH, "a", encoding="utf-8") as f:
            import datetime
            f.write(f"[{datetime.datetime.now():%Y-%m-%d %H:%M:%S}] {text}\n")
    except OSError:
        pass
