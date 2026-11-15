"""相容入口。實際程式在 pk/（base、hub、registry、games）。"""

from pk.hub import hub
from pk.registry import GAMES

__all__ = ["hub", "GAMES"]
