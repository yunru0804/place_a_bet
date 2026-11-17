import asyncio

from pk.base import BaseGame, winner


class StopwatchGame(BaseGame):
    """停錶：多回合目標秒數，碼表短暫顯示後隱藏，累計差距小的贏。"""
    key = "stopwatch"
    title = "⏱ 停錶挑戰"
    description = "每回合在目標秒數按停（5、10、20 秒…，碼表 2 秒後消失），累計差距小的贏"
    order = 30
    TARGETS_MS = (5000, 10000, 20000)
    TARGET_POOL_MS = (5000, 10000, 20000, 8000, 15000, 12000, 25000, 30000)   # 回合數加多時依序往後取
    SETTING = {"attr": "ROUNDS", "label": "回合數", "fmt": "{} 回合", "default": 3, "min": 1, "max": 8}
    SHOW_MS = 2000
    EXTRA = 8.0
    MISS_PENALTY_MS = 8000
    ROUND_END = 3.0
    BETWEEN = 2.0
    CLIENT_JS = r"""
  let runStart = 0, myStop = null, myStopRound = 0;
  G.stopwatch = {
    label: s => s.round_no ? `第 ${s.round_no}/${s.rounds} 回合 · 目標 ${s.target / 1000} 秒` : '準備…',
    panels(s) {
      const p = {};
      for (const k of ['A', 'B']) {
        const v = s.stopped[k];
        const tot = s.history.length ? `累計差 ${sec(s.total[k])} 秒` : '';
        if (s.phase === 'done') p[k] = {big: sec(s.total[k]), sub: '累計差距（秒）'};
        else if (s.phase === 'round_end') {
          p[k] = v === null ? {big: '沒按', sub: tot} : {big: sec(v) + ' 秒', sub: `差 ${sec(Math.abs(v - s.target))} 秒 · ${tot}`};
        } else p[k] = {big: v ? '已停 ✓' : (s.phase === 'run' ? '計時中' : '–'), sub: tot};
      }
      return p;
    },
    render(s) {
      if (s.phase === 'run' && changed('phase', 'round_no')) { runStart = recvAt; myStop = null; myStopRound = s.round_no; }
      if (!$('clock')) {
        stage.innerHTML = '<div class="sw-target" id="swtarget"></div><div class="clock" id="clock">0.00</div>' +
          (side ? '<button type="button" class="tap-btn stop-btn" id="stopbtn" disabled>準備…</button>' : '');
        if (side) $('stopbtn').addEventListener('pointerdown', e => {
          e.preventDefault();
          if (!st || st.phase !== 'run' || myStop !== null) return;
          myStop = Math.round(performance.now() - runStart);
          send({a: 'stop', ms: myStop}); buzz(30); G.stopwatch.render(st);
        });
      }
      $('swtarget').textContent = s.round_no ? `🎯 目標 ${s.target / 1000} 秒` : '';
      const b = $('stopbtn');
      if (b) {
        const stopped = myStop !== null && myStopRound === s.round_no;
        b.disabled = s.phase !== 'run' || stopped;
        b.textContent = stopped ? '已按停！' : (s.phase === 'run' ? 'STOP' : (s.phase === 'done' ? '結束' : '準備…'));
      }
      const c = $('clock');
      if (s.phase === 'countdown') { c.textContent = '0.00'; c.classList.remove('hidden-time'); }
      else if (s.phase === 'round_end' || s.phase === 'done') { c.textContent = sec(s.target); c.classList.remove('hidden-time'); }
    },
    tick(now) {
      const c = $('clock');
      if (!c || !st || st.phase !== 'run') return;
      const ms = now - runStart;
      const hide = (myStop !== null && myStopRound === st.round_no) || ms >= st.show_ms;
      c.textContent = hide ? '?.??' : sec(ms);
      c.classList.toggle('hidden-time', hide);
    },
  };
"""

    def apply_setting(self, value):
        self.TARGETS_MS = self.TARGET_POOL_MS[:int(value)]

    def __init__(self, rnd, hub):
        super().__init__(rnd, hub)
        self.round_no = 0
        self.stopped = {"A": None, "B": None}
        self.history = []
        self.total = {"A": 0, "B": 0}

    def target(self):
        return self.TARGETS_MS[self.round_no - 1] if self.round_no else self.TARGETS_MS[0]

    def err(self, ms, target):
        return self.MISS_PENALTY_MS if ms is None else abs(ms - target)

    def public(self):
        reveal = self.phase in ("round_end", "done")
        return {"round_no": self.round_no, "rounds": len(self.TARGETS_MS), "target": self.target(),
                "show_ms": self.SHOW_MS, "history": self.history, "total": self.total,
                "stopped": {s: (v if reveal else v is not None) for s, v in self.stopped.items()}}

    def handle(self, side, data):
        if data.get("a") != "stop" or self.phase != "run" or self.stopped[side] is not None:
            return
        try:
            ms = int(data.get("ms"))
        except (TypeError, ValueError):
            return
        cap_ms = self.target() + int(self.EXTRA * 1000)
        self.stopped[side] = max(0, min(ms, cap_ms + 500))
        self.mark()

    async def run(self):
        for i, target in enumerate(self.TARGETS_MS):
            self.round_no = i + 1
            self.stopped = {"A": None, "B": None}
            if i == 0:
                await self.countdown()
            else:
                await self.set_phase("countdown", self.BETWEEN)
                await asyncio.sleep(self.BETWEEN)
            await self.set_phase("run")
            await self.wait(lambda: None not in self.stopped.values(), target / 1000 + self.EXTRA)
            row = {"target": target, **self.stopped}
            self.history.append(row)
            for side in "AB":
                self.total[side] += self.err(self.stopped[side], target)
            await self.set_phase("round_end", self.ROUND_END)
            await asyncio.sleep(self.ROUND_END)
        await self.set_phase("done")

    def outcome(self):
        a, b = self.total["A"], self.total["B"]
        return winner(a, b, higher_wins=False), a, b, f"累計差距 {a / 1000:.2f} 秒 : {b / 1000:.2f} 秒"
