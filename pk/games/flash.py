import asyncio
import random
import unicodedata as _ud

from pk.base import BaseGame, winner

EMOJI_GROUPS = [
    "😀😃😄😁😆😅🤣😂",
    "🙂😊😉😌🙃😇🥲",
    "😐😑😶😕😟🙁😒",
    "😮😯😲😦😧😵",
    "😢😭😥😓😞😔",
    "🤭🤫🤔🤗🤐🤨",
    "😪😴🤤🥱😫😩",
    "😺😸😹😻😼😽🙀😿😾",
    "🕐🕑🕒🕓🕔🕕🕖🕗",
    "🕜🕝🕞🕟🕠🕡🕢🕣",
    "🌕🌖🌗🌘🌑🌒🌓🌔",
    "🌲🌳🌴🎄🌵🌿🍀",
    "🌱🌿🍀🍃🪴🥬",
    "👈👉👆👇👍👎",
    "👊✊🤛🤜✋🤚👋",
    "👌🤞🤟🤘🤙🤏",
    "💖💗💓💞💕💘💝",
]
EMOJI_GROUPS = [[c for c in g if not _ud.category(c).startswith("M")] for g in EMOJI_GROUPS]


class FlashGame(BaseGame):
    """閃現辨識：窗簾打開瞬間有 emoji 飛過，從很像的選項中選出剛剛那個。"""
    key = "flash"
    title = "🎭 閃現辨識"
    description = "窗簾打開瞬間有 emoji 飛過，從很像的選項中選出剛剛那個！共 10 回合，越來越快、窗簾開口越來越小，答對多的贏（同分比作答速度）"
    order = 60
    ROUNDS = 10
    OPTIONS = 6
    FLIGHT_MS = (800, 400)
    GAP = (100, 40)
    LEAD_MS = 600
    CURTAIN_MS = 250
    ANSWER_TIME = 6.0
    REVEAL = 2.2
    CLIENT_JS = r"""
  let optsAt = 0, pickedRound = 0;
  G.flash = {
    label: s => s.round_no ? `第 ${s.round_no}/${s.rounds} 回合` : '準備…',
    panels(s) {
      const p = {};
      for (const k of ['A', 'B']) {
        let sub = '';
        if (s.phase === 'reveal') {
          const pk = s.picks[k];
          sub = !pk ? '沒作答' : (pk.i === s.answer ? `✓ ${sec(pk.ms)} 秒` : `✗ 選了 ${s.options[pk.i]}`);
        } else if (s.phase === 'answer') sub = s.answered[k] ? '已作答' : '思考中…';
        else if (s.phase === 'done') sub = `答對的作答時間 ${sec(s.time[k])} 秒`;
        p[k] = {big: `${s.points[k]} 分`, sub};
      }
      return p;
    },
    render(s) {
      if (!$('theater')) {
        stage.innerHTML = '<div class="theater" id="theater"><div class="fly" id="fly"></div>' +
          '<div class="curtain left"></div><div class="curtain right"></div><div class="theater-msg" id="tmsg"></div></div>' +
          '<div class="fopts" id="fopts"></div><div class="qinfo" id="finfo"></div>';
        if (side) $('fopts').addEventListener('pointerdown', e => {
          const b = e.target.closest('.fopt');
          if (!b || !st || st.phase !== 'answer' || pickedRound === st.round_no) return;
          e.preventDefault();
          pickedRound = st.round_no; b.classList.add('chosen');
          send({a: 'pick', i: +b.dataset.i, ms: Math.round(performance.now() - optsAt)}); buzz(15);
          G.flash.render(st);
        });
      }
      const th = $('theater'), fly = $('fly'), opts = $('fopts');
      if (s.phase === 'flash' && changed('phase', 'round_no')) {
        opts.innerHTML = ''; $('tmsg').textContent = '';
        th.classList.remove('open');
        th.style.setProperty('--shift', Math.min(100, (s.gap / 2) / 51 * 100) + '%');
        fly.textContent = s.emoji;
        fly.style.top = s.y + '%';
        const from = s.direction === 'ltr' ? '-30%' : '130%', to = s.direction === 'ltr' ? '130%' : '-30%';
        fly.style.left = from;
        const openAt = at(s.lead_ms) - performance.now();
        setTimeout(() => {
          th.classList.add('open');
          fly.animate([{left: from}, {left: to}], {duration: s.flight_ms, delay: s.curtain_ms, fill: 'forwards', easing: 'linear'});
          setTimeout(() => { th.classList.remove('open'); fly.textContent = ''; }, s.curtain_ms + s.flight_ms);
        }, Math.max(0, openAt));
      }
      if (s.phase === 'countdown') $('tmsg').textContent = '👀 盯緊窗簾！';
      if ((s.phase === 'answer' || s.phase === 'reveal') && (changed('phase', 'round_no') || !opts.children.length)) {
        if (s.phase === 'answer') optsAt = recvAt;
        $('tmsg').textContent = s.phase === 'answer' ? '剛剛飛過的是？' : '';
        opts.innerHTML = s.options.map((o, i) => `<button type="button" class="fopt" data-i="${i}">${o}</button>`).join('');
      }
      if (s.phase === 'reveal') {
        $('tmsg').textContent = '';
        fly.textContent = ''; th.classList.add('reveal');
        th.dataset.answer = s.options[s.answer];
      } else th.classList.remove('reveal');
      const mine = side && pickedRound === s.round_no;
      opts.querySelectorAll('.fopt').forEach((b, i) => {
        b.disabled = !side || s.phase !== 'answer' || mine;
        b.classList.toggle('correct', s.phase === 'reveal' && i === s.answer);
        if (s.phase === 'reveal' && s.picks) {
          const who = ['A', 'B'].filter(k => s.picks[k] && s.picks[k].i === i);
          b.dataset.who = who.join(' ');
        }
      });
      $('finfo').textContent = s.phase === 'answer'
        ? (side ? (mine ? '已作答，等對手…' : '快選！') : '選手作答中…')
        : (s.phase === 'reveal' ? `答案是 ${s.options[s.answer]}` : '');
    },
  };
"""

    def __init__(self, rnd, hub):
        super().__init__(rnd, hub)
        self.round_no = 0
        self.emoji, self.options, self.answer = "", [], -1
        self.flight_ms, self.direction, self.y, self.gap = 0, "ltr", 50, 100
        self.picks = {"A": None, "B": None}
        self.points = {"A": 0, "B": 0}
        self.time = {"A": 0, "B": 0}

    def public(self):
        d = {"round_no": self.round_no, "rounds": self.ROUNDS, "points": self.points, "time": self.time,
             "answered": {s: v is not None for s, v in self.picks.items()}}
        if self.phase == "flash":
            d.update(emoji=self.emoji, flight_ms=self.flight_ms, lead_ms=self.LEAD_MS,
                     curtain_ms=self.CURTAIN_MS, direction=self.direction, y=self.y, gap=self.gap)
        if self.phase in ("answer", "reveal"):
            d["options"] = self.options
        if self.phase == "reveal":
            d.update(answer=self.answer, picks=self.picks)
        return d

    def handle(self, side, data):
        if data.get("a") != "pick" or self.phase != "answer" or self.picks[side] is not None:
            return
        try:
            i, ms = int(data.get("i")), int(data.get("ms"))
        except (TypeError, ValueError):
            return
        if not 0 <= i < len(self.options):
            return
        self.picks[side] = {"i": i, "ms": max(0, min(ms, int(self.ANSWER_TIME * 1000)))}
        self.mark()

    async def run(self):
        await self.countdown()
        lo, hi = self.FLIGHT_MS[1], self.FLIGHT_MS[0]
        groups = random.sample(EMOJI_GROUPS, len(EMOJI_GROUPS))
        for r in range(1, self.ROUNDS + 1):
            self.round_no = r
            group = groups[(r - 1) % len(groups)]
            self.options = random.sample(group, min(self.OPTIONS, len(group)))
            self.answer = random.randrange(len(self.options))
            self.emoji = self.options[self.answer]
            t = (r - 1) / max(1, self.ROUNDS - 1)
            self.flight_ms = round(hi - (hi - lo) * t)
            self.gap = round(self.GAP[0] - (self.GAP[0] - self.GAP[1]) * t)
            self.direction = random.choice(("ltr", "rtl"))
            self.y = random.randint(25, 75)
            self.picks = {"A": None, "B": None}
            show = (self.LEAD_MS + self.CURTAIN_MS * 2 + self.flight_ms) / 1000 + 0.2
            await self.set_phase("flash", show)
            await asyncio.sleep(show)
            await self.set_phase("answer", self.ANSWER_TIME)
            await self.wait(lambda: None not in self.picks.values(), self.ANSWER_TIME)
            for side, p in self.picks.items():
                if p and p["i"] == self.answer:
                    self.points[side] += 1
                    self.time[side] += p["ms"]
            await self.set_phase("reveal", self.REVEAL)
            await asyncio.sleep(self.REVEAL)
        await self.set_phase("done")

    def outcome(self):
        pa, pb = self.points["A"], self.points["B"]
        ta, tb = self.time["A"], self.time["B"]
        w = winner(pa, pb) or winner(ta, tb, higher_wins=False)
        return w, pa, pb, f"{pa} 分 : {pb} 分（作答 {ta / 1000:.2f} 秒 : {tb / 1000:.2f} 秒）"
