import asyncio
import random

from pk.base import BaseGame, winner


class MemoryGame(BaseGame):
    """記憶：色塊依序閃爍，兩人同時照順序按，累計秒數低的贏。"""
    key = "memory"
    title = "🧠 記憶挑戰"
    description = "記住色塊閃爍順序再照按（每回合多一格），按錯或超時加罰秒，累計秒數少的贏"
    order = 50
    ROUNDS = 10
    SETTING = {"attr": "ROUNDS", "label": "回合數", "fmt": "{} 回合", "default": 10, "min": 3, "max": 20}
    ITEM_MS = 650
    LEAD_MS = 800
    PENALTY = 3.0
    ROUND_END = 2.0
    CLIENT_JS = r"""
  const PADS = ['🔴', '🔵', '🟢', '🟡'];
  let flashTimers = [];
  const flash = (i, ms = 350) => {
    const p = document.querySelector(`.pad[data-i="${i}"]`);
    if (!p) return;
    p.classList.add('lit'); setTimeout(() => p.classList.remove('lit'), ms);
  };
  G.memory = {
    label: s => s.round_no ? `第 ${s.round_no}/${s.rounds} 回合 · ${s.level} 個色塊` : '準備…',
    panels(s) {
      const p = {};
      for (const k of ['A', 'B']) {
        const t = s.times[k];
        let sub = '';
        if (s.status[k] === 'ok') sub = `✓ ${sec(t)} 秒`;
        else if (s.status[k] === 'fail') sub = `✗ 罰 ${sec(t)} 秒`;
        else if (s.status[k] === 'playing') sub = `輸入中… ${s.progress[k]}/${s.level}`;
        p[k] = {big: sec(s.total[k]) + ' 秒', sub};
      }
      return p;
    },
    render(s) {
      if (!$('pads')) {
        stage.innerHTML = '<div class="mem-info" id="meminfo"></div><div class="pads" id="pads">' +
          PADS.map((_, i) => `<button type="button" class="pad pad-${i}" data-i="${i}"></button>`).join('') + '</div>';
        if (side) $('pads').addEventListener('pointerdown', e => {
          const b = e.target.closest('.pad');
          if (!b || !st || st.phase !== 'input' || st.status[side] !== 'playing') return;
          e.preventDefault();
          flash(+b.dataset.i, 200); send({a: 'pad', i: +b.dataset.i}); buzz(10);
        });
      }
      if (s.phase === 'show' && changed('phase', 'round_no')) {
        flashTimers.forEach(clearTimeout);
        flashTimers = s.seq.map((k, i) => setTimeout(() => flash(k, s.item_ms * 0.7), at(s.lead_ms + i * s.item_ms) - performance.now()));
      }
      const mine = side ? s.status[side] : null;
      const info = {
        countdown: '準備…', show: '👀 記住順序！',
        input: side ? (mine === 'playing' ? '換你按！越快越好' : (mine === 'ok' ? '完成！等對手…' : '按錯了…')) : '選手輸入中…',
        round_end: `第 ${s.round_no} 回合結束`, done: '',
      }[s.phase] || '';
      $('meminfo').textContent = info;
      $('pads').classList.toggle('active', !!side && s.phase === 'input' && mine === 'playing');
    },
  };
"""

    def __init__(self, rnd, hub):
        super().__init__(rnd, hub)
        self.round_no = 0
        self.seq: list[int] = []
        self.progress = {"A": 0, "B": 0}
        self.status = {"A": "wait", "B": "wait"}
        self.times = {"A": None, "B": None}
        self.total = {"A": 0, "B": 0}
        self.input_start = 0.0

    @staticmethod
    def length(round_no: int) -> int:
        return 3 + (round_no - 1) // 2

    def limit(self) -> float:
        return len(self.seq) * 1.0 + 4

    def public(self):
        d = {"round_no": self.round_no, "rounds": self.ROUNDS, "level": len(self.seq),
             "progress": self.progress, "status": self.status, "times": self.times, "total": self.total,
             "item_ms": self.ITEM_MS, "lead_ms": self.LEAD_MS}
        if self.phase == "show":
            d["seq"] = self.seq
        return d

    def handle(self, side, data):
        if data.get("a") != "pad" or self.phase != "input" or self.status[side] != "playing":
            return
        try:
            i = int(data.get("i"))
        except (TypeError, ValueError):
            return
        if self.seq[self.progress[side]] == i:
            self.progress[side] += 1
            if self.progress[side] == len(self.seq):
                self.status[side] = "ok"
                self.times[side] = int((self.now() - self.input_start) * 1000)
        else:
            self.status[side] = "fail"
            self.times[side] = int((self.limit() + self.PENALTY) * 1000)
        self.mark()

    async def run(self):
        await self.countdown()
        for r in range(1, self.ROUNDS + 1):
            self.round_no = r
            self.seq = [random.randrange(4) for _ in range(self.length(r))]
            self.progress = {"A": 0, "B": 0}
            self.status = {"A": "wait", "B": "wait"}
            self.times = {"A": None, "B": None}
            show = (self.LEAD_MS + len(self.seq) * self.ITEM_MS) / 1000 + 0.3
            await self.set_phase("show", show)
            await asyncio.sleep(show)
            self.status = {"A": "playing", "B": "playing"}
            self.input_start = self.now()
            await self.set_phase("input", self.limit())
            await self.wait(lambda: "playing" not in self.status.values(), self.limit())
            for side in "AB":
                if self.status[side] == "playing":
                    self.status[side] = "fail"
                    self.times[side] = int((self.limit() + self.PENALTY) * 1000)
                self.total[side] += self.times[side]
            await self.set_phase("round_end", self.ROUND_END)
            await asyncio.sleep(self.ROUND_END)
        await self.set_phase("done")

    def outcome(self):
        a, b = self.total["A"], self.total["B"]
        return winner(a, b, higher_wins=False), a, b, f"累計 {a / 1000:.2f} 秒 : {b / 1000:.2f} 秒"
