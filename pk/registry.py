"""掃描 pk/games/ 裡每個模組，組出選單與引擎對照表。"""

import importlib
import pkgutil

from pk.base import BaseGame


def _load():
    import pk.games as pkg
    specs = []
    for info in pkgutil.iter_modules(pkg.__path__):
        if info.name.startswith("_"):
            continue
        mod = importlib.import_module(f"pk.games.{info.name}")
        for val in vars(mod).values():
            if not isinstance(val, type):
                continue
            if val.__module__ != mod.__name__:
                continue
            if not getattr(val, "key", None):
                continue
            specs.append(val)
    specs.sort(key=lambda c: (getattr(c, "order", 50), c.key))
    return specs


SPECS = _load()
GAMES = {c.key: c for c in SPECS if getattr(c, "engine", True) and issubclass(c, BaseGame)}
PK_GAMES = {c.key: (c.title, c.description) for c in SPECS if not getattr(c, "team", False)}
TEAM_GAMES = {c.key: (c.title, c.description) for c in SPECS if getattr(c, "team", False)}
GAME_INFO = {**PK_GAMES, **TEAM_GAMES}


def client_js() -> str:
    parts = []
    for c in SPECS:
        js = getattr(c, "CLIENT_JS", "") or ""
        if js.strip():
            parts.append(js.strip())
    return "\n\n".join(parts)
