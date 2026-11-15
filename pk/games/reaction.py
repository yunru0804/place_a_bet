import asyncio
import random

from pk.base import BaseGame, winner


class ReactionGame(BaseGame):
    """反應力：等待 → 變綠 → 先按的贏這回合；變綠前按＝偷跑。先拿 TARGET_WINS 勝。"""
    key = "reaction"
    title = "🚦 反應力對決"
    description = "畫面變綠時搶先按！太早按算偷跑，先拿 2 勝"
    order = 20
    TARGET_WINS = 2
    MAX_ROUNDS = 7
    GO_WINDOW = 2.5
    MIN_HUMAN_MS = 90
    WAIT_RANGE = (1.5, 4.0)
    ROUND_END = 2.0
    CLIENT_JS = r"""
  let goAt = 0, pressedRound = 0;
  const fmtRT = v => v === 'foul' ? '偷跑！' : v === 'miss' ? '沒按' : (v / 1000).toFixed(3) + ' 秒';
  G.reaction = {
    label: s => s.round_no ? `第 ${s.round_no} 回合 · 先拿 ${s.target} 勝` : '準備…',
    panels(s) {
      const p = {};
      for (const k of ['A', 'B']) {
        const v = s.pressed[k];
        let sub = '';
        if (s.phase === 'round_end' && v !== null) sub = fmtRT(v);
        else if (v !== null) sub = v === 'foul' ? '偷跑！' : '已按 ✓';
        p[k] = {big: `${s.wins[k]} 勝`, sub};
      }
      return p;
    },
    render(s) {
      if (changed('phase', 'round_no') && s.phase === 'go') goAt = recvAt;
      let cls = 'zone-wait', text = '準備…';
      if (s.phase === 'wait') { cls = 'zone-wait'; text = side ? '等它變綠…<br><small>別偷按！</small>' : '等待變綠…'; }
      else if (s.phase === 'go') { cls = 'zone-go'; text = side ? '按！' : 'GO!'; }
      else if (s.phase === 'round_end') {
        cls = 'zone-end';
        text = s.round_winner ? `${esc(NAMES[s.round_winner])}<br><small>拿下第 ${s.round_no} 回合</small>` : '這回合不算<br><small>重來</small>';
      }
      if (side && pressedRound === s.round_no && (s.phase === 'wait' || s.phase === 'go')) {
        text = s.pressed[side] === 'foul' ? '偷跑了！😱' : '已按！';
      }
      if (!$('zone')) {
        stage.innerHTML = '<div class="zone" id="zone"></div>';
        if (side) $('zone').addEventListener('pointerdown', e => {
          e.preventDefault();
          if (!st || pressedRound === st.round_no) return;
          if (st.phase === 'wait') { pressedRound = st.round_no; send({a: 'press'}); buzz(200); }
          else if (st.phase === 'go') {
            pressedRound = st.round_no; send({a: 'press', rt: Math.round(performance.now() - goAt)}); buzz(20);
          } else return;
          G.reaction.render(st);
        });
      }
      const z = $('zone');
      z.className = 'zone ' + cls + (side ? ' mine' : '');
      z.innerHTML = text;
      if (s.phase === 'go' && changed('phase')) buzz(side ? 40 : 0);
    },
  };
"""

    def __init__(self, rnd, hub):
        super().__init__(rnd, hub)
        self.wins = {"A": 0, "B": 0}
        self.round_no = 0
        self.pressed = {"A": None, "B": None}
        self.round_winner = None

    def public(self):
        return {"wins": self.wins, "round_no": self.round_no, "pressed": self.pressed,
                "round_winner": self.round_winner, "target": self.TARGET_WINS}

    def handle(self, side, data):
        if data.get("a") != "press" or self.pressed[side] is not None:
            return
        if self.phase == "wait":
            self.pressed[side] = "foul"
        elif self.phase == "go":
            try:
                rt = int(data.get("rt"))
            except (TypeError, ValueError):
                return
            self.pressed[side] = "foul" if rt < self.MIN_HUMAN_MS else min(rt, 5000)
        else:
            return
        self.mark()

    def _resolve(self):
        p = {s: (v if v is not None else "miss") for s, v in self.pressed.items()}
        self.pressed = p
        fa, fb = p["A"] == "foul", p["B"] == "foul"
        if fa or fb:
            return None if (fa and fb) else ("B" if fa else "A")
        na, nb = isinstance(p["A"], int), isinstance(p["B"], int)
        if na and nb:
            return winner(p["A"], p["B"], higher_wins=False)
        if na or nb:
            return "A" if na else "B"
        return None

    async def run(self):
        await self.countdown()
        while max(self.wins.values()) < self.TARGET_WINS and self.round_no < self.MAX_ROUNDS:
            self.round_no += 1
            self.pressed = {"A": None, "B": None}
            self.round_winner = None
            await self.set_phase("wait")
            fouled = await self.wait(lambda: "foul" in self.pressed.values(), random.uniform(*self.WAIT_RANGE))
            if not fouled:
                await self.set_phase("go", self.GO_WINDOW)
                await self.wait(lambda: None not in self.pressed.values(), self.GO_WINDOW)
            self.round_winner = self._resolve()
            if self.round_winner:
                self.wins[self.round_winner] += 1
            await self.set_phase("round_end", self.ROUND_END)
            await asyncio.sleep(self.ROUND_END)
        await self.set_phase("done")

    def outcome(self):
        a, b = self.wins["A"], self.wins["B"]
        return winner(a, b), a, b, f"{a} 勝 : {b} 勝"
