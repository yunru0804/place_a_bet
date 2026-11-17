import asyncio
import random

from pk.base import BaseGame, winner


def _math_question():
    op = random.choice("+-×")
    if op == "+":
        x, y = random.randint(12, 89), random.randint(12, 89)
        ans = x + y
    elif op == "-":
        x = random.randint(30, 99)
        y = random.randint(11, x - 5)
        ans = x - y
    else:
        x, y = random.randint(3, 9), random.randint(6, 19)
        ans = x * y
    opts = {ans}
    for d in random.sample([1, 2, 10, -1, -2, -10, 11, -11, 9, -9], 10):
        if len(opts) == 4:
            break
        if ans + d > 0:
            opts.add(ans + d)
    opts = list(opts)
    random.shuffle(opts)
    return f"{x} {op} {y} = ?", opts, opts.index(ans)


class MathGame(BaseGame):
    """搶答心算：同一題四選一，先答對得 1 分；答錯這題就不能再答。"""
    key = "math"
    title = "🧮 搶答心算"
    description = "同一題誰先答對得 1 分，答錯這題就不能再答，分數高的贏"
    order = 40
    TOTAL_Q = 10
    SETTING = {"attr": "TOTAL_Q", "label": "題數", "fmt": "{} 題", "default": 10, "min": 3, "max": 30}
    Q_TIME = 10.0
    REVEAL = 1.8
    CLIENT_JS = r"""
  let answered = 0;
  G.math = {
    label: s => s.q_no ? `第 ${s.q_no}/${s.total_q} 題` : '準備…',
    panels(s) {
      const p = {};
      for (const k of ['A', 'B']) {
        let sub = '';
        if (s.q_winner === k) sub = '答對 ✓';
        else if (s.wrong[k]) sub = '答錯 ✗';
        p[k] = {big: `${s.points[k]} 分`, sub};
      }
      return p;
    },
    render(s) {
      if (s.phase !== 'question' && s.phase !== 'reveal') {
        if (s.phase === 'done') stage.innerHTML = '';
        return;
      }
      if (changed('q_no', 'phase') || !$('opts')) {
        stage.innerHTML = `<div class="mq">${esc(s.question)}</div><div class="opts" id="opts">` +
          s.options.map((o, i) => `<button type="button" class="opt" data-i="${i}">${o}</button>`).join('') + '</div>';
        if (side) $('opts').addEventListener('pointerdown', e => {
          const b = e.target.closest('.opt');
          if (!b || !st || st.phase !== 'question' || answered === st.q_no || st.wrong[side] || st.q_winner) return;
          e.preventDefault();
          answered = st.q_no; b.classList.add('chosen'); send({a: 'ans', i: +b.dataset.i}); buzz(15);
          G.math.render(st);
        });
      }
      const mine = side && (answered === s.q_no || s.wrong[side]);
      document.querySelectorAll('#opts .opt').forEach((b, i) => {
        b.disabled = !side || s.phase !== 'question' || mine || !!s.q_winner;
        b.classList.toggle('correct', s.phase === 'reveal' && i === s.answer);
      });
      let info = $('qinfo');
      if (!info) { info = document.createElement('div'); info.id = 'qinfo'; info.className = 'qinfo'; stage.appendChild(info); }
      info.textContent = s.phase === 'reveal'
        ? (s.q_winner ? `${NAMES[s.q_winner]} 搶到 1 分！` : '沒人答對')
        : (side && s.wrong[side] ? '答錯了，等下一題…' : (side && answered === s.q_no ? '已作答…' : ''));
    },
  };
"""

    def __init__(self, rnd, hub):
        super().__init__(rnd, hub)
        self.points = {"A": 0, "B": 0}
        self.q_no = 0
        self.question, self.options, self.answer = "", [], -1
        self.wrong = {"A": False, "B": False}
        self.q_winner = None

    def public(self):
        d = {"points": self.points, "q_no": self.q_no, "question": self.question,
             "options": self.options, "wrong": self.wrong, "q_winner": self.q_winner,
             "total_q": self.TOTAL_Q}
        if self.phase == "reveal":
            d["answer"] = self.answer
        return d

    def handle(self, side, data):
        if data.get("a") != "ans" or self.phase != "question" or self.q_winner or self.wrong[side]:
            return
        try:
            i = int(data.get("i"))
        except (TypeError, ValueError):
            return
        if i == self.answer:
            self.q_winner = side
            self.points[side] += 1
        else:
            self.wrong[side] = True
        self.mark()

    async def run(self):
        await self.countdown()
        while self.q_no < self.TOTAL_Q:
            self.q_no += 1
            self.question, self.options, self.answer = _math_question()
            self.wrong = {"A": False, "B": False}
            self.q_winner = None
            await self.set_phase("question", self.Q_TIME)
            await self.wait(lambda: self.q_winner or all(self.wrong.values()), self.Q_TIME)
            await self.set_phase("reveal", self.REVEAL)
            await asyncio.sleep(self.REVEAL)
        await self.set_phase("done")

    def outcome(self):
        a, b = self.points["A"], self.points["B"]
        return winner(a, b), a, b, f"{a} 分 : {b} 分"
