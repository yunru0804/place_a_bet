"""全員賽：聲音模仿大賽。

流程：輪盤抽一位出題者 → 出題者錄一段聲音 → 所有人（出題者也要）模仿這段聲音 → 依模仿度排名發點數
（第 1 名 1500、第 2 名 1400…每名少 100，最少 100；同分同名次同點數）。
主持人可以按「跳過等待」直接進到下一步。

相似度（前端 Meyda 抽特徵、伺服器比對）：
1. 手機把錄音轉成 16kHz，每 512 點取一格 MFCC（第 1～12 維；第 0 維是音量，丟掉），頭尾靜音裁掉。
2. 每段錄音各自減掉自己的平均（CMN），抵消不同手機麥克風的差異。
3. 用 DTW 把快慢不同的兩段對齊，每格用 cosine 距離，總距離除以路徑長度。
4. 距離換成 0～100 分（用 macOS 合成語音校正過：同一句話不同人約 75～95 分，不同句子 / 雜音 40 分以下）。
"""

import asyncio
import math
import random
import secrets

import db
from pk.base import SoloGame

# ---------------- 相似度 ----------------
SCORE_MID = 0.24      # 距離 = 這個值時 50 分
SCORE_SLOPE = 0.045   # 越小 → 分數對距離越敏感
MIN_FRAMES = 8        # 有聲音的格數少於這個（約 0.25 秒）當作沒錄到


def _cmn_unit(x):
    """減掉平均（CMN）後每格正規化成單位向量，cosine 距離 = 1 - 內積。"""
    n, d = len(x), len(x[0])
    mu = [sum(r[j] for r in x) / n for j in range(d)]
    out = []
    for r in x:
        v = [r[j] - mu[j] for j in range(d)]
        norm = math.sqrt(sum(t * t for t in v)) or 1e-9
        out.append([t / norm for t in v])
    return out


def dtw_distance(a, b) -> float:
    a, b = _cmn_unit(a), _cmn_unit(b)
    n, m = len(a), len(b)
    inf = float("inf")
    prev = [0.0] + [inf] * m
    for i in range(1, n + 1):
        cur = [inf] * (m + 1)
        ai = a[i - 1]
        for j in range(1, m + 1):
            cost = 1.0 - sum(map(float.__mul__, ai, b[j - 1]))
            cur[j] = cost + min(prev[j], cur[j - 1], prev[j - 1])
        prev = cur
    return prev[m] / (n + m)


def similarity(ref, imit) -> float:
    """0～100 分。"""
    if len(ref) < MIN_FRAMES or len(imit) < MIN_FRAMES:
        return 0.0
    d = dtw_distance(ref, imit)
    score = 100 / (1 + math.exp((d - SCORE_MID) / SCORE_SLOPE))
    ratio = max(len(ref), len(imit)) / min(len(ref), len(imit))
    if ratio > 1.6:                     # 長度差太多（例如只講半句）再打折
        score *= 1.6 / ratio
    return round(score, 1)


def check_feats(feats) -> list | None:
    """前端送來的 MFCC：最多 10 秒、每格 12 維、都是有限的數字。"""
    if not isinstance(feats, list) or not (MIN_FRAMES <= len(feats) <= 400):
        return None
    out = []
    for row in feats:
        if not isinstance(row, list) or len(row) != 12:
            return None
        try:
            r = [float(v) for v in row]
        except (TypeError, ValueError):
            return None
        if not all(math.isfinite(v) and abs(v) < 1e4 for v in r):
            return None
        out.append(r)
    return out


class VoiceGame(SoloGame):
    key = "voice"
    title = "🎙 聲音模仿大賽"
    description = "輪盤抽一人出題，所有人（含出題者）模仿那段聲音，越像名次越前：第 1 名 +1500，每名少 100。"
    order = 10
    VENDOR_JS = "meyda.min.js"
    CLIP_SEC = 5
    SETTING = {"attr": "CLIP_SEC", "label": "錄音長度（秒）", "fmt": "錄 {} 秒", "default": 5, "min": 3, "max": 10}

    FIRST_PRIZE = 1500
    STEP = 100
    MIN_PRIZE = 100
    SPIN = 6.0             # 輪盤轉幾秒
    RECORD_TIME = 25.0     # 出題時間
    IMITATE_TIME = 45.0    # 模仿時間（可以先重複聽題目）
    REVEAL = 30.0          # 公布排名＋大家重聽
    IMIT_EXTRA = 1.5       # 模仿最長 = 題目長度 + 這麼多秒（不超過 CLIP_SEC）
    GRACE = 4.0            # 時間到後再等上傳
    MAX_SPINS = 3          # 抽到的人沒出題就重抽，最多抽幾次

    def __init__(self, rnd, hub):
        super().__init__(rnd, hub)
        self.wheel: list[int] = []
        self.pick = 0
        self.spins = 0
        self.questioner: int | None = None
        self.prompt: dict | None = None
        self.expected: list[int] = []
        self.imits: dict[int, dict] = {}          # uid -> {clip, score}（pending = 計算中）
        self.clips: dict[str, bytes] = {}
        self.ranking: list[dict] = []
        self.skipping = False

    def skip(self) -> None:
        """主持人按「跳過等待」：結束目前這個階段。"""
        self.skipping = True
        self.mark()

    async def _stage(self, phase: str, seconds: float, done=lambda: False) -> None:
        self.skipping = False
        await self.set_phase(phase, seconds)
        await self.wait(lambda: self.skipping or done(), seconds + self.GRACE)

    def _clip(self, wav: bytes) -> str:
        cid = secrets.token_urlsafe(8)
        self.clips[cid] = wav
        return cid

    def public(self):
        d = {
            "clip_sec": self.CLIP_SEC, "imit_sec": self.imit_sec(),
            "first": self.FIRST_PRIZE, "step": self.STEP,
            "wheel": [self.names[u] for u in self.wheel], "pick": self.pick, "spin": self.spins,
            "questioner": self.questioner,
            "questioner_name": self.names.get(self.questioner, "") if self.questioner else "",
            "prompt_ok": self.prompt is not None,
            "need": len(self.expected),
            "done_n": sum(1 for v in self.imits.values() if not v.get("pending")),
            "imitated": [u for u, v in self.imits.items() if not v.get("pending")],
        }
        if self.phase in ("imitate", "reveal") and self.prompt:
            d["prompt_clip"] = self.prompt["clip"]
        if self.phase == "reveal":
            d["ranking"] = self.ranking
        return d

    async def submit(self, uid: int, role: str, wav: bytes, feats) -> str | None:
        if uid not in self.sides:
            return "你不是這場的玩家。"
        feats = check_feats(feats)
        if feats is None:
            return "沒有錄到有效的聲音，再錄一次。"
        if role == "prompt":
            if self.phase != "record":
                return "現在不是出題時間。"
            if uid != self.questioner:
                return "這場不是由你出題。"
            if self.prompt is not None:
                return "題目已經送出了。"
            self.prompt = {"clip": self._clip(wav), "feats": feats}
        elif role == "imitate":
            if self.phase != "imitate":
                return "現在不是模仿時間。"
            if uid in self.imits:
                return "只能模仿一次喔。"
            self.imits[uid] = {"pending": True}
            score = await asyncio.to_thread(similarity, self.prompt["feats"], feats)
            self.imits[uid] = {"clip": self._clip(wav), "score": score}
        else:
            return "動作不正確。"
        self.mark()
        return None

    def imit_sec(self) -> int:
        """模仿可以錄多久：題目長度 + 1.5 秒（題目只有 3 秒就不用等滿 10 秒）。"""
        if not self.prompt:
            return self.CLIP_SEC
        sec = len(self.prompt["feats"]) * 512 / 16000 + self.IMIT_EXTRA
        return max(2, min(self.CLIP_SEC, math.ceil(sec)))

    def _all_done(self) -> bool:
        return all(u in self.imits and not self.imits[u].get("pending") for u in self.expected)

    def _rank(self):
        rows = sorted(((v["score"], u) for u, v in self.imits.items() if not v.get("pending")), reverse=True)
        out, prev, rank = [], None, 0
        for i, (score, u) in enumerate(rows, start=1):
            if score != prev:
                rank, prev = i, score
            award = max(self.MIN_PRIZE, self.FIRST_PRIZE - self.STEP * (rank - 1))
            out.append({"uid": u, "name": self.names[u], "score": score, "rank": rank, "award": award,
                        "clip": self.imits[u]["clip"]})
        self.ranking = out

    async def run(self):
        await self.countdown()
        tried: set[int] = set()
        while self.prompt is None and self.spins < self.MAX_SPINS:
            pool = [u for u in self.online() if u not in tried] or [u for u in self.sides if u not in tried]
            if not pool:
                break
            self.wheel = pool[:]
            random.shuffle(self.wheel)
            self.pick = random.randrange(len(self.wheel))
            self.questioner = self.wheel[self.pick]
            tried.add(self.questioner)
            self.spins += 1
            await self.set_phase("roulette", self.SPIN)
            await asyncio.sleep(self.SPIN + 0.6)
            await self._stage("record", self.RECORD_TIME, lambda: self.prompt is not None)
        if self.prompt is not None:
            on = set(self.online())
            self.expected = [u for u in self.sides if u in on or u == self.questioner]   # 出題者也要模仿
            await self._stage("imitate", self.IMITATE_TIME, self._all_done)
        self._rank()
        await self._stage("reveal", self.REVEAL)

    def awards(self):
        if self.prompt is None:
            return [], "沒有人出題，本輪結束"
        rows = [(r["uid"], r["award"], f"{self.title} 第 {r['rank']} 名（{r['score']} 分）") for r in self.ranking]
        if not self.ranking:
            return rows, f"{self.names[self.questioner]} 出題，但沒有人模仿"
        top = self.ranking[0]
        return rows, f"🥇 {top['name']} 模仿度 {top['score']} 分！共 {len(self.ranking)} 人模仿"

    CLIENT_JS = r"""
  // ---- 聲音模仿大賽：輪盤 → 出題 → 全員模仿 → 排名 ----
  const VX = {
    SR: 16000, N: 512, HOP: 512,
    resample(pcm, from) {
      if (from === VX.SR) return pcm;
      const r = from / VX.SR, n = Math.floor(pcm.length / r), out = new Float32Array(n);
      for (let i = 0; i < n; i++) {
        const s = Math.floor(i * r), e = Math.max(s + 1, Math.floor((i + 1) * r));
        let a = 0; for (let j = s; j < e; j++) a += pcm[j];
        out[i] = a / (e - s);
      }
      return out;
    },
    features(pcm) {
      Meyda.bufferSize = VX.N; Meyda.sampleRate = VX.SR;
      Meyda.numberOfMFCCCoefficients = 13; Meyda.windowingFunction = 'hanning';
      const mf = [], rms = [];
      for (let i = 0; i + VX.N <= pcm.length; i += VX.HOP) {
        const r = Meyda.extract(['mfcc', 'rms'], pcm.slice(i, i + VX.N));
        mf.push(r.mfcc); rms.push(r.rms);
      }
      const peak = Math.max(0, ...rms), thr = Math.max(peak * 0.1, 0.004);
      let s = 0, e = rms.length - 1;
      while (s < e && rms[s] < thr) s++;
      while (e > s && rms[e] < thr) e--;
      const feats = [];
      for (let i = s; i <= e; i++) feats.push(mf[i].slice(1).map(v => Math.round(v * 100) / 100));
      return {feats, peak};
    },
    wav(pcm) {
      const n = pcm.length, buf = new ArrayBuffer(44 + n * 2), v = new DataView(buf);
      const w = (o, s) => { for (let i = 0; i < s.length; i++) v.setUint8(o + i, s.charCodeAt(i)); };
      w(0, 'RIFF'); v.setUint32(4, 36 + n * 2, true); w(8, 'WAVE'); w(12, 'fmt ');
      v.setUint32(16, 16, true); v.setUint16(20, 1, true); v.setUint16(22, 1, true);
      v.setUint32(24, VX.SR, true); v.setUint32(28, VX.SR * 2, true); v.setUint16(32, 2, true); v.setUint16(34, 16, true);
      w(36, 'data'); v.setUint32(40, n * 2, true);
      for (let i = 0; i < n; i++) { const s = Math.max(-1, Math.min(1, pcm[i])); v.setInt16(44 + i * 2, s < 0 ? s * 0x8000 : s * 0x7fff, true); }
      let str = ''; const u8 = new Uint8Array(buf);
      for (let i = 0; i < u8.length; i += 0x8000) str += String.fromCharCode.apply(null, u8.subarray(i, i + 0x8000));
      return btoa(str);
    },
  };
  const VO_ME = +(document.getElementById('arena').dataset.uid || 0) || null;
  const VO_IDLE = {phase: 'ready', imitated: [], need: 0, done_n: 0, wheel: []};
  const VO_COLORS = ['#e63946', '#f7c948', '#2ec4b6', '#4d7cff', '#ff8c42', '#8a5cf6', '#5dfc8d', '#ff5fa2'];
  let voCtx = null, voStream = null, voRec = null, voBusy = false, voMsg = '', voSent = {}, voView = '', voPhase = null, voSpun = 0;
  const voCache = {};
  const voAudio = async () => {
    if (!voCtx) voCtx = new (window.AudioContext || window.webkitAudioContext)();
    if (voCtx.state !== 'running') await voCtx.resume();
  };
  const voMic = async () => {
    await voAudio();
    if (!voStream) voStream = await navigator.mediaDevices.getUserMedia(
      {audio: {echoCancellation: false, noiseSuppression: false, autoGainControl: true}});
  };
  const voRender = () => G.voice.render(st || VO_IDLE);
  const voRecord = maxSec => new Promise(resolve => {
    const src = voCtx.createMediaStreamSource(voStream);
    const proc = voCtx.createScriptProcessor(4096, 1, 1);
    const chunks = [];
    let done = false, tm = null, heard = false, quiet = 0;
    proc.onaudioprocess = e => {
      const d = e.inputBuffer.getChannelData(0);
      chunks.push(new Float32Array(d));
      let a = 0; for (let i = 0; i < d.length; i += 8) a += d[i] * d[i];
      const rms = Math.sqrt(a / (d.length / 8));
      const lv = $('volevel'); if (lv) lv.style.width = Math.min(100, rms * 400) + '%';
      // 講完自動停：聽到聲音之後，連續安靜 1.2 秒就結束（不用等滿秒數）
      if (rms > 0.03) { heard = true; quiet = 0; }
      else if (heard && (quiet += d.length / voCtx.sampleRate) > 1.2) stop();
    };
    src.connect(proc); proc.connect(voCtx.destination);
    const stop = () => {
      if (done) return;
      done = true; clearTimeout(tm); voRec = null;
      proc.disconnect(); src.disconnect();
      const lv = $('volevel'); if (lv) lv.style.width = '0%';
      const n = chunks.reduce((a, c) => a + c.length, 0), pcm = new Float32Array(n);
      let o = 0; for (const c of chunks) { pcm.set(c, o); o += c.length; }
      resolve(pcm);
    };
    tm = setTimeout(stop, maxSec * 1000);
    voRec = {stop, t0: performance.now(), max: maxSec};
  });
  const voTake = async role => {
    if (voBusy || voSent[role]) return;
    voBusy = true; voMsg = '';
    try { await voMic(); } catch (e) {
      voBusy = false; voMsg = '麥克風打不開：請允許使用麥克風，或關掉分頁重開連結'; voRender(); return;
    }
    buzz(20, 'blip');
    const pending = voRecord(role === 'imitate' ? (st.imit_sec || st.clip_sec || 5) : ((st && st.clip_sec) || 5));
    voRender();
    const pcm = await pending;
    voMsg = '分析中…'; voRender();
    const p16 = VX.resample(pcm, voCtx.sampleRate);
    const f = VX.features(p16);
    if (f.peak < 0.01 || f.feats.length < 8) {
      voBusy = false; voMsg = '沒錄到聲音，再錄一次（靠近麥克風大聲一點）'; voRender(); return;
    }
    let pk = 0; for (let i = 0; i < p16.length; i++) pk = Math.max(pk, Math.abs(p16[i]));
    const gain = pk > 0 ? Math.min(8, 0.9 / pk) : 1;          // 播放用：把音量拉到差不多大聲
    for (let i = 0; i < p16.length; i++) p16[i] *= gain;
    try {
      const r = await fetch('/pk/voice', {method: 'POST',
        headers: {'Content-Type': 'application/json', 'ngrok-skip-browser-warning': '1'},
        body: JSON.stringify({t: q, round: ROUND, role, wav: VX.wav(p16), feats: f.feats})});
      const j = await r.json().catch(() => ({}));
      if (r.ok && !j.error) {
        voSent[role] = true;
        voMsg = role === 'prompt' ? '題目送出 ✓' : '送出 ✓';
        buzz(30, 'coin');
      } else voMsg = j.error || '上傳失敗，再試一次';
    } catch (e) { voMsg = '上傳失敗，再試一次'; }
    voBusy = false; voRender();
  };
  const voPlay = async clip => {
    if (!clip) return;
    try {
      await voAudio();
      let buf = voCache[clip];
      if (!buf) {
        const r = await fetch(`/pk/clip/${ROUND}/${clip}`, {headers: {'ngrok-skip-browser-warning': '1'}});
        if (!r.ok) throw 0;
        buf = voCache[clip] = await voCtx.decodeAudioData(await r.arrayBuffer());
      }
      const s = voCtx.createBufferSource(); s.buffer = buf; s.connect(voCtx.destination); s.start();
    } catch (e) { voMsg = '播放失敗，再按一次'; voRender(); }
  };
  const voBtn = (act, text, cls = '', extra = '') =>
    `<button type="button" class="vo-btn ${cls}" data-act="${act}" ${extra}>${text}</button>`;
  // 輪盤：每一格一個名字，轉到抽中的那格停在上方指針
  const voSpin = s => {
    const box = $('vowheelbox'), n = s.wheel.length;
    if (!n) return;
    box.hidden = false;
    const seg = 360 / n;
    const grad = s.wheel.map((_, i) => `${VO_COLORS[i % VO_COLORS.length]} ${i * seg}deg ${(i + 1) * seg}deg`).join(',');
    box.innerHTML = '<div class="vo-pointer">▼</div><div class="vo-wheel" id="vowheel"></div>';
    const wh = $('vowheel');
    wh.style.background = `conic-gradient(${grad})`;
    wh.innerHTML = s.wheel.map((name, i) =>
      `<span style="transform: rotate(${i * seg + seg / 2 - 90}deg)"><b>${esc(name)}</b></span>`).join('');
    const target = 360 * 6 - (s.pick * seg + seg / 2);
    const left = Math.max(0, at(st.ends_in) - performance.now());
    if (left < 400) { wh.style.transform = `rotate(${target}deg)`; return; }
    wh.style.transform = 'rotate(0deg)';
    void wh.offsetWidth;
    wh.style.transition = `transform ${left}ms cubic-bezier(.15, .65, .12, 1)`;
    wh.style.transform = `rotate(${target}deg)`;
    let last = -1;
    const clicker = setInterval(() => {   // 指針滴答聲
      const m = getComputedStyle(wh).transform;
      if (m === 'none') return;
      const [a, b] = m.slice(7, -1).split(',').map(Number);
      const deg = ((Math.atan2(b, a) * 180 / Math.PI) + 360) % 360, k = Math.floor(deg / seg);
      if (k !== last) { last = k; if (window.sfx) sfx.play('tick'); }
    }, 30);
    setTimeout(() => {
      clearInterval(clicker);
      buzz([40, 30, 40], 'coin');
      if (window.fx) fx.celebrate('small');
    }, left + 50);
  };

  G.voice = {
    prepare() { if (side) voRender(); },
    label: s => ({countdown: '準備…', roulette: '🎡 抽出題者', record: '🎙 出題中', imitate: '🦜 全員模仿中', reveal: '🏆 排行榜'})[s.phase] || '準備…',
    panels: () => ({A: {big: ''}, B: {big: ''}}),
    render(s) {
      if (!$('vo')) {
        stage.innerHTML = '<div class="vo" id="vo"><div class="vo-wheel-box" id="vowheelbox" hidden></div>' +
          '<div class="vo-role" id="vorole"></div>' +
          '<div class="vo-meter"><i id="volevel"></i></div><div class="vo-msg" id="vomsg"></div>' +
          '<div class="vo-btns" id="vobtns"></div><ol class="vo-rank" id="vorank"></ol></div>';
        stage.addEventListener('click', e => {
          const b = e.target.closest('[data-act]');
          if (!b || b.disabled) return;
          const act = b.dataset.act;
          if (act === 'mic') voMic().then(() => { voMsg = '麥克風 OK ✓'; voRender(); })
            .catch(() => { voMsg = '麥克風打不開：請允許使用麥克風'; voRender(); });
          else if (act === 'stop') { if (voRec) voRec.stop(); }
          else if (act === 'play') voPlay(b.dataset.clip);
          else voTake(act);
        });
      }
      if (s.phase !== voPhase) { if (voPhase !== null && !voBusy) voMsg = ''; voPhase = s.phase; }
      if (s.phase === 'roulette' && voSpun !== s.spin) { voSpun = s.spin; voSent = {}; voSpin(s); }
      if (s.phase !== 'roulette' && s.phase !== 'record') $('vowheelbox').hidden = true;
      const qn = esc(s.questioner_name || ''), iAmQ = side && s.questioner === VO_ME;
      const sec = s.clip_sec || 5;
      let role = '', btns = '', rank = '';
      if (voRec) {
        btns = voBtn('stop', '⏹ 錄好了', 'rec');
      } else if (s.phase === 'roulette') {
        role = s.spin > 1 ? '沒出題，重抽！' : '誰要出題？';
      } else if (s.phase === 'record') {
        if (iAmQ) {
          role = voSent.prompt ? '' : `🎯 你出題！錄一句台詞、怪聲或歌（${sec} 秒內，講完會自動停）`;
          btns = voSent.prompt ? '' : voBtn('prompt', '🔴 開始錄題目', 'big', voBusy ? 'disabled' : '');
        } else role = `🎯 <b>${qn}</b> 出題中…`;
      } else if (s.phase === 'imitate') {
        if (side) {
          role = voSent.imitate ? `等其他人…（${s.done_n}/${s.need}）`
            : (iAmQ ? '換你模仿自己的題目！只有一次機會' : '聽完題目再模仿，只有一次機會');
          btns = voBtn('play', '▶ 聽題目', 'play', `data-clip="${s.prompt_clip || ''}"`) +
                 (voSent.imitate ? '' : voBtn('imitate', '🔴 開始模仿', 'big', voBusy ? 'disabled' : ''));
        } else {
          role = `模仿中…（${s.done_n}/${s.need}）`;
          btns = voBtn('play', `▶ ${qn} 的題目`, 'play', `data-clip="${s.prompt_clip || ''}"`);
        }
      } else if (s.phase === 'reveal') {
        const me = (s.ranking || []).find(r => r.uid === VO_ME);
        role = !s.prompt_ok ? '沒有人出題，本輪結束'
          : me ? `你第 <b>${me.rank}</b> 名 <b>+${me.award}</b>`
          : side ? '你這輪沒有模仿 😢' : '';
        if (s.prompt_clip) btns = voBtn('play', `▶ 原音（${qn}）`, 'play', `data-clip="${s.prompt_clip}"`);
        const medal = r => ['🥇', '🥈', '🥉'][r - 1] || `${r}`;
        rank = (s.ranking || []).map(r =>
          `<li class="${r.uid === VO_ME ? 'me' : ''}"><span class="vr-rank">${medal(r.rank)}</span>` +
          `<span class="vr-name">${esc(r.name)}<small>${r.score} 分</small></span>` +
          `<span class="vr-award">+${r.award}</span>` +
          `<button type="button" class="vr-play" data-act="play" data-clip="${r.clip}">▶</button></li>`).join('');
      } else if (side) {
        role = voStream ? '' : '先開麥克風（iPhone 選「允許」）';
        btns = voStream ? '' : voBtn('mic', '🎤 開啟麥克風', 'big');
        if (voStream && !voMsg) voMsg = '麥克風 OK ✓ 等主持人開始';
      } else role = '準備中…';
      const view = [role, btns, voMsg, rank].join('|');
      if (view === voView) return;
      voView = view;
      $('vorole').innerHTML = role;
      $('vomsg').textContent = voMsg;
      $('vobtns').innerHTML = btns;
      $('vorank').innerHTML = rank;
    },
    tick(now) {
      if (!voRec) return;
      const b = stage.querySelector('[data-act=stop]');
      if (b) b.textContent = `⏹ 錄好了（${Math.max(0, voRec.max - (now - voRec.t0) / 1000).toFixed(1)}）`;
    },
  };
"""
