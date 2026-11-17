import asyncio
from collections import deque
from fractions import Fraction

from pk.base import TeamGame


class TugGame(TeamGame):
    """團體拔河：兩隊全員一起點，先把繩結拉過線的隊伍贏。"""
    key = "tug"
    title = "🪢 拔河"
    description = "兩隊全員一起狂點把繩子拉向自己！不計時，先把繩結拉過線的隊伍贏。依隊伍人數平均計算，人少不吃虧。勝隊每人 +500 點，輸的不扣點！"
    order = 10
    PULL = 2.0
    MAX_PER_SEC = 20
    CLIENT_JS = r"""
  let tugLocal = 0;
  G.tug = {
    label: s => s.phase === 'play' ? '🪢 用力拉！' : (s.phase === 'done' ? '比賽結束' : '準備…'),
    panels(s) {
      const p = {};
      for (const k of ['A', 'B']) {
        let sub = `${s.size[k]} 人`;
        if (s.phase === 'done' && s.mvp && s.mvp[k]) sub = `MVP ${s.mvp[k].name}（${s.mvp[k].taps} 下）`;
        p[k] = {big: `${s.taps[k]} 下`, sub};
      }
      return p;
    },
    render(s) {
      if (!$('rope')) {
        stage.innerHTML =
          `<div class="rope-wrap"><div class="rope-zone za ${$('panel-A').className.match(/team-\w+/)?.[0] || ''}"></div>` +
          `<div class="rope-zone zb ${$('panel-B').className.match(/team-\w+/)?.[0] || ''}"></div>` +
          '<div class="rope" id="rope"></div><div class="rope-mid"></div>' +
          '<div class="knot" id="knot">🎀</div>' +
          `<span class="rope-end ea">${esc(NAMES.A)}</span><span class="rope-end eb">${esc(NAMES.B)}</span></div>` +
          (side ? `<button type="button" class="tap-btn tug-btn ${$('panel-' + side).className.match(/team-\w+/)?.[0] || ''}" id="tugbtn" disabled>準備…</button>` : '');
        if (side) $('tugbtn').addEventListener('pointerdown', e => {
          e.preventDefault();
          if (!st || st.phase !== 'play') return;
          tugLocal++; send({a: 'tap'}); buzz(0, 'tap'); pop($('tugbtn'));
        });
      }
      $('knot').style.left = (50 + s.pos * 0.42) + '%';
      $('knot').classList.toggle('lead-a', s.pos < 0);
      $('knot').classList.toggle('lead-b', s.pos > 0);
      const b = $('tugbtn');
      if (b) {
        b.disabled = s.phase !== 'play';
        b.textContent = s.phase === 'play' ? (side === 'A' ? '⬅ 拉！' : '拉！➡') : (s.phase === 'done' ? '結束' : '準備…');
      }
    },
  };
"""

    def __init__(self, rnd, hub):
        super().__init__(rnd, hub)
        self.pos = Fraction(0)
        self.taps = {"A": 0, "B": 0}
        self.per_user: dict[int, int] = {}
        self.recent: dict[int, deque] = {}

    def mvp(self):
        out = {}
        for side in "AB":
            best = max(((n, uid) for uid, n in self.per_user.items() if self.sides.get(uid) == side), default=None)
            out[side] = {"name": self.names[best[1]], "taps": best[0]} if best else None
        return out

    def public(self):
        d = {"pos": round(float(self.pos), 1), "taps": self.taps, "size": self.size}
        if self.phase == "done":
            d["mvp"] = self.mvp()
        return d

    def handle(self, side, data):
        if data.get("a") != "tap" or self.phase != "play" or abs(self.pos) >= 100:
            return
        now, uid = self.now(), self.uid
        recent = self.recent.setdefault(uid, deque())
        while recent and now - recent[0] > 1.0:
            recent.popleft()
        if len(recent) >= self.MAX_PER_SEC:
            return
        recent.append(now)
        self.per_user[uid] = self.per_user.get(uid, 0) + 1
        self.taps[side] += 1
        pull = Fraction(self.PULL) / self.size[side]
        self.pos = max(Fraction(-100), min(Fraction(100), self.pos + (-pull if side == "A" else pull)))
        self.dirty = True

    async def run(self):
        await self.countdown()
        await self.set_phase("play")
        while abs(self.pos) < 100:
            await asyncio.sleep(0.1)
            if self.dirty:
                await self.push()
        await self.set_phase("done")

    def outcome(self):
        w = "A" if self.pos <= -100 else "B" if self.pos >= 100 else None
        a, b = self.taps["A"], self.taps["B"]
        return w, a, b, f"拉過線！（{a} 下 : {b} 下）"
