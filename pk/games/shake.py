"""團體賽：全員搖手機開香檳，目標 = 人數 × PER_MEMBER。"""

import asyncio
from collections import deque

from pk.base import TeamGame, winner


class ShakeGame(TeamGame):
    """兩隊各搖一瓶香檳。每人搖自己的手機，次數加進該隊。
    目標 = 隊伍人數 × PER_MEMBER（3 人 300、4 人 400）。先達標的隊贏。"""

    key = "shake"
    title = "🍾 開香檳"
    description = (
        "全員猛搖手機，把香檳搖到泡沫要噴出來！"
        "先把瓶子搖爆的隊贏，勝隊每人 +500 點。"
    )
    order = 30
    PER_MEMBER = 100
    SETTING = {"attr": "PER_MEMBER", "label": "每人要搖幾下", "fmt": "每人 {} 下", "default": 100, "min": 30, "max": 500}
    MAX_PER_SEC = 8
    CLIENT_JS = r"""
  let shakeOn = false, lastShake = 0, prevG = null, shakeDenied = false;
  const SHAKE_IDLE = {phase: 'ready', heat: {A: 0, B: 0}};   // 開賽前先畫畫面用
  const needsMotionPerm = !!(typeof DeviceMotionEvent !== 'undefined' && DeviceMotionEvent.requestPermission);
  const httpBlocked = needsMotionPerm && !window.isSecureContext;
  const shakeOnce = () => {
    if (GAME !== 'shake' || !st || st.phase !== 'play') return;
    const now = performance.now();
    if (now - lastShake < 90) return;
    lastShake = now;
    send({a: 'shake'});
    buzz(12, 'fizz');
    ME.shaking();                                     // 有搖角色才搖
    const b = $('champ-' + side);
    if (b) { b.classList.remove('hit'); void b.offsetWidth; b.classList.add('hit'); }
  };
  const onMotion = e => {
    const g = e.accelerationIncludingGravity;
    if (!g) return;
    const v = [g.x || 0, g.y || 0, g.z || 0];
    if (prevG) {
      const d = Math.hypot(v[0] - prevG[0], v[1] - prevG[1], v[2] - prevG[2]);
      if (d > 6) shakeOnce();
    }
    prevG = v;
  };
  const armShake = async () => {
    if (shakeOn || httpBlocked) return;
    try {
      if (needsMotionPerm) {
        const p = await DeviceMotionEvent.requestPermission();
        if (p !== 'granted') { shakeDenied = true; G.shake.render(st || SHAKE_IDLE); return; }
      }
    } catch (err) { shakeDenied = true; G.shake.render(st || SHAKE_IDLE); return; }
    window.addEventListener('devicemotion', onMotion);
    shakeOn = true;
    G.shake.render(st || SHAKE_IDLE);
  };
  // 所有遊戲的 JS 都在同一個擂台裡，只有開香檳這場才掛感應器
  if (GAME === 'shake' && side && !needsMotionPerm) {
    window.addEventListener('devicemotion', onMotion);
    shakeOn = true;
  }
  G.shake = {
    // 開賽前就顯示「開啟搖晃」：iPhone 要先按一下、選允許，倒數那 3 秒才來得及
    prepare() { if (side) G.shake.render(SHAKE_IDLE); },
    label: s => {
      if (s.phase !== 'play') return s.phase === 'done' ? '砰！！' : '準備…';
      const h = Math.max(s.heat.A, s.heat.B);
      if (h >= 0.88) return '要爆了——！！';
      if (h >= 0.65) return '泡沫往上衝！';
      return '🍾 用力搖！';
    },
    panels(s) {
      const p = {};
      for (const k of ['A', 'B']) {
        const h = s.heat[k];
        let sub = `${s.size[k]} 人一起搖`;
        if (h >= 0.88) sub = '塞子要飛了！！';
        else if (h >= 0.65) sub = '氣泡狂湧…';
        else if (h >= 0.35) sub = '開始起泡';
        if (s.phase === 'done' && s.mvp && s.mvp[k]) sub = `搖最兇：${s.mvp[k].name}`;
        p[k] = {big: h >= 1 ? '💥' : (h >= 0.88 ? '🫧' : '🍾'), sub};
      }
      return p;
    },
    render(s) {
      if (!$('champ')) {
        const bot = k => {
          const cls = $('panel-' + k).className.match(/team-\w+/)?.[0] || '';
          return `<div class="champ-bot ${cls}" id="champ-${k}" data-k="${k}">` +
            `<div class="champ-cork" id="cork-${k}">🍾</div>` +
            `<div class="champ-glass"><div class="champ-fill" id="fill-${k}"><i class="champ-foam"></i></div></div>` +
            `<div class="champ-name">${esc(NAMES[k])}</div></div>`;
        };
        stage.innerHTML = `<div class="champ" id="champ">${bot('A')}${bot('B')}</div>` +
          '<div class="jr-hint" id="shakehint"></div>' +
          (side ? `<button type="button" class="tap-btn jr-btn ${$('panel-' + side).className.match(/team-\w+/)?.[0] || ''}" id="shakebtn">開啟搖晃</button>` : '');
        if (ME.on) {                                    // 有角色：站在自己的瓶子旁邊（兩瓶中間、面向自己的瓶子）
          $('champ').appendChild(ME.el.parentNode);
          $('champ').classList.add('has-me', 'me-' + side);
        }
        if (side) {
          const sb = $('shakebtn');
          // iOS Safari 只把「click / touchend」當成使用者手勢；在 pointerdown 裡呼叫
          // DeviceMotionEvent.requestPermission() 不會跳出權限視窗，搖晃就永遠收不到。
          sb.addEventListener('click', () => {
            if (!shakeOn && !httpBlocked && !shakeDenied) armShake();
          });
          // 比賽中按按鈕也算一次搖（不能搖的手機用狂點代替）
          sb.addEventListener('pointerdown', e => {
            if (!st || st.phase !== 'play') return;
            e.preventDefault();
            if (shakeOn || httpBlocked || shakeDenied) shakeOnce();
          });
        }
      }
      for (const k of ['A', 'B']) {
        const h = s.heat[k];
        const el = $('champ-' + k);
        $('fill-' + k).style.height = Math.min(100, h * 100) + '%';
        el.classList.toggle('me', side === k);
        el.classList.toggle('simmer', h >= 0.4 && h < 0.7);
        el.classList.toggle('boil', h >= 0.7 && h < 0.88);
        el.classList.toggle('critical', h >= 0.88 && h < 1);
        el.classList.toggle('full', h >= 1);
      }
      const hint = $('shakehint');
      const b = $('shakebtn');
      if (s.phase !== 'play') {
        if (hint) hint.textContent = s.phase === 'done' ? '開瓶了！'
          : httpBlocked ? 'iPhone 請狂點按鈕（http 不能搖晃）'
          : shakeDenied ? '動作權限被拒絕：比賽時請狂點按鈕，或關掉分頁重開連結再按「開啟搖晃」'
          : shakeOn ? '已開啟搖晃！等倒數' : '先按「開啟搖晃」→ 選「允許」，等倒數';
        if (b) { b.disabled = s.phase === 'done'; b.textContent = s.phase === 'done' ? '結束' : (shakeOn || httpBlocked ? '等開始' : '開啟搖晃'); }
        return;
      }
      if (hint) {
        if (!side) hint.textContent = '選手正在搖…';
        else if (httpBlocked || shakeDenied) hint.textContent = '這頁無法用感應器，請狂點「搖！」';
        else if (shakeOn) hint.textContent = '搖手機！點按鈕也算';
        else hint.textContent = '先按「開啟搖晃」允許動作與方向';
      }
      if (b) {
        b.disabled = false;
        b.textContent = (shakeOn || httpBlocked || shakeDenied) ? '搖！' : '開啟搖晃';
      }
    },
  };
"""

    def __init__(self, rnd, hub):
        super().__init__(rnd, hub)
        self.shakes = {"A": 0, "B": 0}
        self.target = {s: self.size[s] * self.PER_MEMBER for s in "AB"}
        self.per_user: dict[int, int] = {}
        self.recent: dict[int, deque] = {}

    def mvp(self):
        out = {}
        for side in "AB":
            best = max(((n, uid) for uid, n in self.per_user.items() if self.sides.get(uid) == side), default=None)
            out[side] = {"name": self.names[best[1]]} if best else None
        return out

    def public(self):
        heat = {s: min(1.0, self.shakes[s] / self.target[s]) for s in "AB"}
        d = {"heat": heat, "size": self.size}
        if self.phase == "done":
            d["mvp"] = self.mvp()
        return d

    def handle(self, side, data):
        if data.get("a") != "shake" or self.phase != "play":
            return
        if self.shakes[side] >= self.target[side]:
            return
        now, uid = self.now(), self.uid
        recent = self.recent.setdefault(uid, deque())
        while recent and now - recent[0] > 1.0:
            recent.popleft()
        if len(recent) >= self.MAX_PER_SEC:
            return
        recent.append(now)
        self.per_user[uid] = self.per_user.get(uid, 0) + 1
        self.count_act(uid)
        self.shakes[side] += 1
        self.dirty = True

    async def run(self):
        await self.countdown()
        await self.set_phase("play")
        while self.shakes["A"] < self.target["A"] and self.shakes["B"] < self.target["B"]:
            await asyncio.sleep(0.08)
            if self.dirty:
                await self.push()
        await self.set_phase("done")

    def outcome(self):
        a, b = self.shakes["A"], self.shakes["B"]
        ta, tb = self.target["A"], self.target["B"]
        if a >= ta and b >= tb:
            w = winner(a / ta, b / tb)
        elif a >= ta:
            w = "A"
        elif b >= tb:
            w = "B"
        else:
            w = None
        return w, a, b, "砰！軟木塞飛出去了！"
