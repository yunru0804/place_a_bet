import asyncio
from collections import deque

from pk.base import BaseGame, winner


class TapGame(BaseGame):
    """連打：時間內點最多下的贏。每秒最多計 MAX_PER_SEC 下。"""
    key = "tap"
    title = "⚡ 連打大賽"
    description = "10 秒內誰點得多誰贏！"
    order = 10
    DURATION = 10.0
    MAX_PER_SEC = 25
    CLIENT_JS = r"""
  let tapLocal = 0;
  G.tap = {
    label: s => s.phase === 'play' ? '⚡ 狂點！' : '準備…',
    panels(s) {
      const sc = {...s.score};
      if (side) sc[side] = Math.max(sc[side], tapLocal);
      const top = Math.max(40, sc.A, sc.B) * 1.1;
      return {A: {big: sc.A, bar: sc.A / top}, B: {big: sc.B, bar: sc.B / top}};
    },
    render(s) {
      if (!side) { stage.innerHTML = ''; return; }
      if (!$('tap')) {
        stage.innerHTML = '<button type="button" class="tap-btn" id="tap" disabled>準備…</button>';
        $('tap').addEventListener('pointerdown', e => {
          e.preventDefault();
          if (!st || st.phase !== 'play' || performance.now() > at(st.ends_in)) return;
          tapLocal++; send({a: 'tap'}); buzz(0, 'tap'); setPanels(); pop($('tap'));
        });
      }
      const b = $('tap');
      b.disabled = s.phase !== 'play';
      b.textContent = s.phase === 'play' ? 'TAP!' : (s.phase === 'done' ? '時間到！' : '準備…');
    },
  };
"""

    def __init__(self, rnd, hub):
        super().__init__(rnd, hub)
        self.score = {"A": 0, "B": 0}
        self.recent = {"A": deque(), "B": deque()}

    def public(self):
        return {"score": self.score}

    def handle(self, side, data):
        if data.get("a") != "tap" or self.phase != "play" or self.now() > self.phase_end:
            return
        recent, now = self.recent[side], self.now()
        while recent and now - recent[0] > 1.0:
            recent.popleft()
        if len(recent) >= self.MAX_PER_SEC:
            return
        recent.append(now)
        self.score[side] += 1
        self.dirty = True

    async def run(self):
        await self.countdown()
        await self.set_phase("play", self.DURATION)
        while self.now() < self.phase_end:
            await asyncio.sleep(0.1)
            if self.dirty:
                await self.push()
        await self.set_phase("done")

    def outcome(self):
        a, b = self.score["A"], self.score["B"]
        return winner(a, b), a, b, f"{a} 下 : {b} 下"
