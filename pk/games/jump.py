"""團體跳繩：跟上繩拍點跳，連續與全員齊跳有加成。"""

import asyncio

from pk.base import TeamGame, winner


class JumpGame(TeamGame):
    """兩隊全員一起跳繩。繩子有節奏地轉，畫面提示「跳！」時按一下。
    跳過：個人連跳越高分（1→2→3…最高 COMBO_CAP）。
    漏跳或太早按：連跳歸零。
    同一拍全隊都跳過：齊跳加成，連續齊跳再加乘。
    分數依人數平均，人少不吃虧。時間到分數高的贏。"""

    key = "jump"
    title = "🤸 跳繩"
    description = (
        "拿著手機跟上繩拍一起跳（往上一頓就算跳，也可以按按鈕）！跳過就得分，連續跳過連跳加成；"
        "全隊同一拍都跳過再加齊跳分。時間到比總分，人少不吃虧。勝隊每人 +500 點。"
    )
    order = 20
    DURATION = 20.0
    SETTING = {"attr": "DURATION", "label": "遊玩時間（秒）", "fmt": "{} 秒", "default": 20, "min": 10, "max": 120}
    PERIOD_START = 1.05
    PERIOD_END = 0.68
    WINDOW = 0.42          # 每拍最後這段比例才算跳過
    COMBO_CAP = 5
    TOGETHER = 4           # 齊跳基礎分（再乘連續齊跳次數，最後除人數）
    CLIENT_JS = r"""
  let jumpedBeat = -1, jmOn = false, jmDenied = false, jmLast = 0, jmShown = null;
  // 現在是不是在「該跳」的區間：用本機時間即時算（伺服器只在每拍開頭送一次狀態）
  const jmWindow = () => st && st.phase === 'play' && st.beat_in != null
    && at(st.beat_in) - performance.now() <= st.window_ms;
  const jmPaint = () => {
    const on = jmWindow(), used = jumpedBeat === st.beat;
    const key = `${st.beat}|${on}|${used}`;
    if (key === jmShown) return;
    jmShown = key;
    $('jr').classList.toggle('go', on);
    $('jr').classList.toggle('wait', !on);
    $('jrkid').textContent = on ? '🤸' : '🧍';
    const hint = $('jrhint'), b = $('jrbtn');
    if (side && used) hint.textContent = '跳過！等下一拍';
    else hint.textContent = on ? (jmOn ? '跳！（往上一頓）' : '跳！') : '等繩子過來…';
    if (b) { b.disabled = used; b.textContent = used ? '已跳 ✓' : (on ? '跳！' : '等拍子'); }
  };
  const JUMP_IDLE = {phase: 'ready'};
  const JM_NEED_PERM = !!(typeof DeviceMotionEvent !== 'undefined' && DeviceMotionEvent.requestPermission);
  const JM_HTTP = JM_NEED_PERM && !window.isSecureContext;
  const JM_G = 17;          // 合加速度超過約 1.7g 算「往上一頓」（起跳或把手機往上甩）
  const JM_COOLDOWN = 450;  // 落地那一下也會很大，冷卻 0.45 秒避免算成下一拍偷跑
  const doJump = () => {
    if (GAME !== 'jump' || !st || st.phase !== 'play' || jumpedBeat === st.beat) return;
    jumpedBeat = st.beat;
    send({a: 'jump'}); buzz(18, 'jump');
    const b = $('jrbtn'); if (b) pop(b);
    G.jump.render(st);
  };
  const onJumpMotion = e => {
    const g = e.accelerationIncludingGravity;
    if (!g) return;
    const m = Math.hypot(g.x || 0, g.y || 0, g.z || 0);
    const now = performance.now();
    if (m > JM_G && now - jmLast > JM_COOLDOWN) { jmLast = now; doJump(); }
  };
  const armJump = async () => {
    if (jmOn || JM_HTTP) return;
    try {
      if (JM_NEED_PERM && await DeviceMotionEvent.requestPermission() !== 'granted') throw 0;
    } catch (err) { jmDenied = true; G.jump.render(st || JUMP_IDLE); return; }
    window.addEventListener('devicemotion', onJumpMotion);
    jmOn = true;
    G.jump.render(st || JUMP_IDLE);
  };
  if (GAME === 'jump' && side && !JM_NEED_PERM) {     // Android：不用問權限，直接開
    window.addEventListener('devicemotion', onJumpMotion);
    jmOn = true;
  }
  G.jump = {
    // 開賽前就顯示「開啟體感」：iPhone 要先按一下、選允許
    prepare() { if (side) G.jump.render(JUMP_IDLE); },
    label: s => s.phase === 'play'
      ? (s.together && s.together[side || 'A'] > 1 ? `齊跳 ×${s.together[side || 'A']}！` : '跟著跳！')
      : (s.phase === 'done' ? '比賽結束' : '準備…'),
    panels(s) {
      const p = {};
      const top = Math.max(20, s.score.A, s.score.B) * 1.15;
      for (const k of ['A', 'B']) {
        let sub = `${s.size[k]} 人 · 連跳 ${s.combo[k]}`;
        if (s.together[k] > 1) sub += ` · 齊跳×${s.together[k]}`;
        if (s.phase === 'done' && s.mvp && s.mvp[k]) sub = `MVP ${s.mvp[k].name}（${s.mvp[k].pts} 分）`;
        p[k] = {big: s.score[k], bar: s.score[k] / top, sub};
      }
      return p;
    },
    render(s) {
      if (!$('jr')) {
        stage.innerHTML =
          '<div class="jr" id="jr">' +
            '<span class="jr-handle jh-l"></span><span class="jr-handle jh-r"></span>' +
            '<div class="jr-rope" id="jrarc"></div>' +
            '<div class="jr-kid" id="jrkid">🧍</div>' +
            '<div class="jr-floor"></div>' +
          '</div>' +
          '<div class="jr-hint" id="jrhint"></div>' +
          (side ? `<button type="button" class="tap-btn jr-btn ${$('panel-' + side).className.match(/team-\w+/)?.[0] || ''}" id="jrbtn">準備…</button>` : '');
        if (side) {
          const jb = $('jrbtn');
          // iOS 只把 click 當成使用者手勢，權限一定要在 click 裡要
          jb.addEventListener('click', () => {
            if ((!st || st.phase !== 'play') && !jmOn && !JM_HTTP && !jmDenied) armJump();
          });
          // 比賽中按按鈕也算跳（備用）
          jb.addEventListener('pointerdown', e => {
            if (!st || st.phase !== 'play') return;
            e.preventDefault();
            doJump();
          });
        }
      }
      const hint = $('jrhint');
      const b = $('jrbtn');
      if (s.phase !== 'play') {
        $('jr').classList.remove('go', 'wait');
        if (hint) hint.textContent = s.phase === 'done' ? '時間到！'
          : !side ? '繩子要轉了…'
          : JM_HTTP ? '這個網址不能用體感，比賽時請按按鈕跳'
          : jmDenied ? '動作權限被拒絕：比賽時請按按鈕跳，或關掉分頁重開連結再開啟'
          : jmOn ? '體感已開啟！拿著手機，繩子到腳下時往上跳' : '先按「開啟體感」→ 選「允許」';
        if (b) {
          const canArm = s.phase !== 'done' && !jmOn && !JM_HTTP && !jmDenied;
          b.disabled = !canArm;
          b.textContent = s.phase === 'done' ? '結束' : (canArm ? '開啟體感' : '等開始');
        }
        return;
      }
      jmShown = null;
      jmPaint();
    },
    tick(now) {
      if (!st || st.phase !== 'play') return;
      const left = Math.max(0, at(st.beat_in) - now);
      const p = st.period_ms ? 1 - left / st.period_ms : 0;
      const arc = $('jrarc');
      if (arc) {
        const foot = p > 0.55;
        arc.style.transform = `scaleY(${(1 - p * 2).toFixed(3)})`;
        arc.classList.toggle('front', foot);
      }
      if ($('jr')) jmPaint();
    },
  };
"""

    def __init__(self, rnd, hub):
        super().__init__(rnd, hub)
        self.score = {"A": 0.0, "B": 0.0}
        self.jumps = {"A": 0, "B": 0}
        self.together = {"A": 0, "B": 0}
        self.combo_user: dict[int, int] = {}
        self.pts_user: dict[int, int] = {}
        self.beat = 0
        self.period = self.PERIOD_START
        self.beat_end = 0.0
        self.jumped: set[int] = set()

    def _add(self, side: str, raw: float) -> None:
        self.score[side] += raw / self.size[side]

    def combo_team(self, side: str) -> int:
        vals = [self.combo_user.get(uid, 0) for uid, s in self.sides.items() if s == side]
        return max(vals, default=0)

    def mvp(self):
        out = {}
        for side in "AB":
            best = max(((n, uid) for uid, n in self.pts_user.items() if self.sides.get(uid) == side), default=None)
            out[side] = {"name": self.names[best[1]], "pts": best[0]} if best else None
        return out

    def public(self):
        now = self.now()
        beat_in = None if self.phase != "play" else max(0, int((self.beat_end - now) * 1000))
        d = {
            "score": {s: round(self.score[s]) for s in "AB"},
            "jumps": self.jumps,
            "combo": {s: self.combo_team(s) for s in "AB"},
            "together": self.together,
            "size": self.size,
            "beat": self.beat,
            "period_ms": int(self.period * 1000),
            "window_ms": int(self.period * self.WINDOW * 1000),
            "beat_in": beat_in,
        }
        if self.phase == "done":
            d["mvp"] = self.mvp()
        return d

    def handle(self, side, data):
        if data.get("a") != "jump" or self.phase != "play" or self.uid is None:
            return
        uid, now = self.uid, self.now()
        if uid in self.jumped or now > self.beat_end:
            return
        self.jumped.add(uid)
        window_start = self.beat_end - self.period * self.WINDOW
        if now < window_start:
            self.combo_user[uid] = 0
            self.dirty = True
            return
        c = self.combo_user.get(uid, 0) + 1
        self.combo_user[uid] = c
        pts = min(c, self.COMBO_CAP)
        self.pts_user[uid] = self.pts_user.get(uid, 0) + pts
        self.jumps[side] += 1
        self._add(side, pts)
        self.dirty = True

    def _resolve_beat(self):
        for uid, side in self.sides.items():
            if uid not in self.jumped:
                self.combo_user[uid] = 0
        for side in "AB":
            members = [u for u, s in self.sides.items() if s == side]
            if members and all(u in self.jumped for u in members):
                self.together[side] += 1
                self._add(side, self.TOGETHER * self.together[side] * len(members))
            else:
                self.together[side] = 0
        self.jumped = set()

    async def run(self):
        await self.countdown()
        end = self.now() + self.DURATION
        t = 0.0
        await self.set_phase("play", self.DURATION)
        while self.now() < end:
            t = min(1.0, 1.0 - max(0.0, end - self.now()) / self.DURATION)
            self.period = self.PERIOD_START + (self.PERIOD_END - self.PERIOD_START) * t
            self.beat += 1
            self.jumped = set()
            self.beat_end = self.now() + self.period
            await self.push()
            remain = self.beat_end - self.now()
            if remain > 0:
                await self.wait(lambda: False, remain)
            self._resolve_beat()
            self.dirty = True
        self.score = {s: round(self.score[s]) for s in "AB"}
        await self.set_phase("done")

    def outcome(self):
        a, b = int(self.score["A"]), int(self.score["B"])
        return winner(a, b), a, b, f"{a} 分 : {b} 分（跳過 {self.jumps['A']} : {self.jumps['B']}）"
