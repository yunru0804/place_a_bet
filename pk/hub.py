"""WebSocket 廣播與一場進行中的遊戲。"""

import asyncio
import json

import db

from pk.base import BaseGame
from pk.registry import GAMES


class Hub:
    def __init__(self):
        self.clients: dict = {}
        self.game: BaseGame | None = None
        self._task: asyncio.Task | None = None

    async def send(self, ws, msg: dict) -> None:
        try:
            await ws.send_text(json.dumps(msg))
        except Exception:
            self.clients.pop(ws, None)

    async def broadcast(self, msg: dict) -> None:
        data = json.dumps(msg)
        for ws in list(self.clients):
            try:
                await ws.send_text(data)
            except Exception:
                self.clients.pop(ws, None)

    def handle(self, user_id: int | None, raw: str) -> None:
        g = self.game
        if g is None or user_id not in g.sides:
            return
        try:
            data = json.loads(raw)
        except ValueError:
            return
        if isinstance(data, dict):
            g.uid = user_id
            g.handle(g.sides[user_id], data)

    async def start(self, rnd) -> None:
        self.game = GAMES[rnd["game"]](rnd, self)
        self._task = asyncio.create_task(self._run(self.game))

    async def _run(self, game: BaseGame) -> None:
        try:
            await game.run()
            result, sa, sb, summary = game.outcome()
            final = await asyncio.to_thread(db.finish_pk, game.round_id, sa, sb, result)
            await self.broadcast({"type": "end", "round": game.round_id, "result": final,
                                  "summary": summary, "voided": final is None})
        finally:
            if self.game is game:
                self.game = None

    async def stop(self, round_id: int) -> None:
        """主持人在比賽中作廢：停止遊戲，不結算。"""
        if self.game and self.game.round_id == round_id:
            if self._task:
                self._task.cancel()
            self.game = None
            await self.broadcast({"type": "end", "round": round_id, "result": None,
                                  "summary": "本輪已作廢", "voided": True})


hub = Hub()
