"""PK 小遊戲：即時同步（WebSocket）與判定。

架構
- 每種遊戲是一個 BaseGame 子類別，實作：
    run()       ：用 await 串起各階段（倒數 → 遊戲 → 結束）
    handle()    ：處理選手送來的動作（同步、要快）
    public()    ：給所有人看的狀態（選手與觀眾看到的一樣）
    outcome()   ：結束後回傳 (勝方 'A'/'B'/None, A 分數, B 分數, 結果說明)
- 狀態一有變化就整包廣播（type=state）；時間一律用「距離現在幾毫秒」，
  不依賴手機時鐘是否準確。手機收到後自己換算、自己畫倒數。
- 判定都在伺服器；需要量測反應時間的遊戲（反應力、停錶）由手機量測後回報，
  這樣網路延遲不會影響公平性。
- 狀態只存在記憶體；比賽中伺服器重啟的話該輪會卡在「比賽中」，主持人作廢即可。
"""

import asyncio
import json
import random
from collections import deque
from fractions import Fraction

import db

COUNTDOWN = 3.0


class BaseGame:
    key = ""

    def __init__(self, rnd, hub):
        self.hub = hub
        self.round_id = rnd["id"]
        self.sides = {rnd["player_a"]: "A", rnd["player_b"]: "B"}
        self.phase = "countdown"
        self.phase_end: float | None = None
        self.dirty = False
        self.changed = asyncio.Event()

    # ---- 工具 ----
    @staticmethod
    def now() -> float:
        return asyncio.get_running_loop().time()

    def message(self) -> dict:
        now = self.now()
        msg = {
            "type": "state", "round": self.round_id, "game": self.key, "phase": self.phase,
            "ends_in": None if self.phase_end is None else max(0, int((self.phase_end - now) * 1000)),
        }
        msg.update(self.public())
        return msg

    async def push(self) -> None:
        self.dirty = False
        await self.hub.broadcast(self.message())

    async def set_phase(self, phase: str, duration: float | None = None) -> None:
        self.phase = phase
        self.phase_end = None if duration is None else self.now() + duration
        await self.push()

    async def countdown(self) -> None:
        await self.set_phase("countdown", COUNTDOWN)
        await asyncio.sleep(COUNTDOWN)

    def mark(self) -> None:
        """handle() 改了狀態後呼叫：讓 run() 立刻推送並重新檢查條件。"""
        self.dirty = True
        self.changed.set()

    async def wait(self, cond, timeout: float) -> bool:
        """等到 cond() 成立或逾時；期間有變化就推送。"""
        end = self.now() + timeout
        while True:
            self.changed.clear()
            if self.dirty:
                await self.push()
            if cond():
                return True
            remaining = end - self.now()
            if remaining <= 0:
                return False
            try:
                await asyncio.wait_for(self.changed.wait(), min(remaining, 0.5))
            except asyncio.TimeoutError:
                pass

    # ---- 子類別實作 ----
    def public(self) -> dict:
        return {}

    def handle(self, side: str, data: dict) -> None:
        pass

    async def run(self) -> None:
        raise NotImplementedError

    def outcome(self) -> tuple[str | None, int, int, str]:
        raise NotImplementedError


def _winner(a, b, higher_wins=True):
    if a == b:
        return None
    return ("A" if a > b else "B") if higher_wins else ("A" if a < b else "B")


# ============================================================
class TapGame(BaseGame):
    """連打：時間內點最多下的贏。每秒最多計 MAX_PER_SEC 下（1 秒視窗，容許網路把點擊擠在一起送達）。"""
    key = "tap"
    DURATION = 10.0
    MAX_PER_SEC = 25

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
        self.dirty = True          # 連打太頻繁，不逐下推送，由 run() 每 0.1 秒推一次

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
        return _winner(a, b), a, b, f"{a} 下 : {b} 下"


# ============================================================
class ReactionGame(BaseGame):
    """反應力：等待（隨機 1.5~4 秒）→ 變綠 → 先按的贏這回合；變綠前按＝偷跑，直接輸這回合。先拿 2 勝。
    反應時間由手機量測（從收到「變綠」到按下），避免兩人網路延遲不同造成不公平。"""
    key = "reaction"
    TARGET_WINS = 2
    MAX_ROUNDS = 7
    GO_WINDOW = 2.5
    MIN_HUMAN_MS = 90           # 比這還快視為搶先預判（偷跑）
    WAIT_RANGE = (1.5, 4.0)
    ROUND_END = 2.0

    def __init__(self, rnd, hub):
        super().__init__(rnd, hub)
        self.wins = {"A": 0, "B": 0}
        self.round_no = 0
        self.pressed = {"A": None, "B": None}   # 反應毫秒 / 'foul' / 'miss'
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
            return _winner(p["A"], p["B"], higher_wins=False)
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
        return _winner(a, b), a, b, f"{a} 勝 : {b} 勝"


# ============================================================
class StopwatchGame(BaseGame):
    """停錶：3 回合，目標依序 5 / 10 / 20 秒。碼表開始後 SHOW_MS 毫秒就隱藏，憑感覺按停。
    3 回合「與目標的差距」加總，累計差距小的贏。
    - 經過時間由手機量測（從收到「開始」到按下），網路延遲不影響。
    - 比賽中不送出任何剩餘時間（不然等於告訴玩家過了幾秒）；伺服器自己用 cap 收尾。
    - 回合結束前不公開彼此的秒數。沒按的回合，差距以 MISS_PENALTY_MS 計。"""
    key = "stopwatch"
    TARGETS_MS = (5000, 10000, 20000)
    SHOW_MS = 2000
    EXTRA = 8.0                 # 每回合最多等到「目標 + EXTRA 秒」
    MISS_PENALTY_MS = 8000
    ROUND_END = 3.0
    BETWEEN = 2.0               # 下一回合開始前的倒數

    def __init__(self, rnd, hub):
        super().__init__(rnd, hub)
        self.round_no = 0
        self.stopped = {"A": None, "B": None}
        self.history = []        # 每回合 {"target", "A", "B"}（已公開）
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
            await self.set_phase("run")                  # 不給 ends_in：畫面不顯示倒數
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
        return _winner(a, b, higher_wins=False), a, b, f"累計差距 {a / 1000:.2f} 秒 : {b / 1000:.2f} 秒"


# ============================================================
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
    """搶答心算：固定 TOTAL_Q 題，同一題四選一，先答對得 1 分；答錯這題就不能再答。全部答完分數高的贏。"""
    key = "math"
    TOTAL_Q = 10
    Q_TIME = 10.0
    REVEAL = 1.8

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
            d["answer"] = self.answer          # 答案只在公布階段才送出
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
        return _winner(a, b), a, b, f"{a} 分 : {b} 分"


# ============================================================
class MemoryGame(BaseGame):
    """記憶：固定 ROUNDS 回合。每回合色塊依序閃爍 → 兩人同時照順序按，計算按完所花的時間；
    按錯或超時，該回合記為「時限 + PENALTY 秒」。10 回合累計秒數低的贏。
    色塊數：第 1~2 回合 3 個，之後每 2 回合多 1 個（最後 7 個）。時間由伺服器計算。"""
    key = "memory"
    ROUNDS = 10
    ITEM_MS = 650
    LEAD_MS = 800
    PENALTY = 3.0
    ROUND_END = 2.0

    def __init__(self, rnd, hub):
        super().__init__(rnd, hub)
        self.round_no = 0
        self.seq: list[int] = []
        self.progress = {"A": 0, "B": 0}
        self.status = {"A": "wait", "B": "wait"}   # wait / playing / ok / fail
        self.times = {"A": None, "B": None}        # 本回合毫秒
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
            d["seq"] = self.seq                # 只在展示階段送出順序
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
                if self.status[side] == "playing":          # 超時
                    self.status[side] = "fail"
                    self.times[side] = int((self.limit() + self.PENALTY) * 1000)
                self.total[side] += self.times[side]
            await self.set_phase("round_end", self.ROUND_END)
            await asyncio.sleep(self.ROUND_END)
        await self.set_phase("done")

    def outcome(self):
        a, b = self.total["A"], self.total["B"]
        return _winner(a, b, higher_wins=False), a, b, f"累計 {a / 1000:.2f} 秒 : {b / 1000:.2f} 秒"


# ============================================================
# 閃現辨識用的「長得很像」emoji 組：同一組顏色相近、外型接近（顏色不同一眼就分得出來，所以不收）
EMOJI_GROUPS = [
    "😀😃😄😁😆😅🤣😂",   # 大笑臉
    "🙂😊😉😌🙃😇🥲",     # 微笑臉
    "😐😑😶😕😟🙁😒",     # 無言臉
    "😮😯😲😦😧😵",       # 驚訝臉
    "😢😭😥😓😞😔",       # 難過臉
    "🤭🤫🤔🤗🤐🤨",       # 手摀嘴臉
    "😪😴🤤🥱😫😩",       # 想睡臉
    "😺😸😹😻😼😽🙀😿😾",  # 貓臉
    "🕐🕑🕒🕓🕔🕕🕖🕗",   # 整點時鐘
    "🕜🕝🕞🕟🕠🕡🕢🕣",   # 半點時鐘
    "🌕🌖🌗🌘🌑🌒🌓🌔",   # 月相
    "🌲🌳🌴🎄🌵🌿🍀",     # 樹木
    "🌱🌿🍀🍃🪴🥬",       # 葉子
    "👈👉👆👇👍👎",       # 手指方向
    "👊✊🤛🤜✋🤚👋",     # 拳頭手掌
    "👌🤞🤟🤘🤙🤏",       # 手勢
    "💖💗💓💞💕💘💝",     # 粉紅愛心
]
import unicodedata as _ud
EMOJI_GROUPS = [[c for c in g if not _ud.category(c).startswith("M")] for g in EMOJI_GROUPS]


class FlashGame(BaseGame):
    """閃現辨識：窗簾打開 → emoji 快速飛過 → 從 6 個很像的 emoji 選出剛剛那個。
    兩人看同一個、各自作答（不是搶答），答對得 1 分。固定 ROUNDS 回合，
    飛行越來越快、窗簾開口越來越小（emoji 只有經過開口時看得到）。
    分數高的贏；同分比「答對題目的總作答時間」，快的贏。作答時間由手機量測（從選項出現到按下）。"""
    key = "flash"
    ROUNDS = 10
    OPTIONS = 6
    FLIGHT_MS = (800, 400)       # 第 1 回合 → 最後一回合
    GAP = (100, 40)              # 窗簾開口佔舞台寬度 %：第 1 回合全開 → 最後一回合只開中間 40%
    LEAD_MS = 600               # 送出後多久拉開窗簾（讓兩支手機都收到）
    CURTAIN_MS = 250
    ANSWER_TIME = 6.0
    REVEAL = 2.2

    def __init__(self, rnd, hub):
        super().__init__(rnd, hub)
        self.round_no = 0
        self.emoji, self.options, self.answer = "", [], -1
        self.flight_ms, self.direction, self.y, self.gap = 0, "ltr", 50, 100
        self.picks = {"A": None, "B": None}       # {"i": 選項, "ms": 作答毫秒}
        self.points = {"A": 0, "B": 0}
        self.time = {"A": 0, "B": 0}              # 答對題目的累計作答毫秒

    def public(self):
        d = {"round_no": self.round_no, "rounds": self.ROUNDS, "points": self.points, "time": self.time,
             "answered": {s: v is not None for s, v in self.picks.items()}}
        if self.phase == "flash":
            d.update(emoji=self.emoji, flight_ms=self.flight_ms, lead_ms=self.LEAD_MS,
                     curtain_ms=self.CURTAIN_MS, direction=self.direction, y=self.y, gap=self.gap)
        if self.phase in ("answer", "reveal"):
            d["options"] = self.options
        if self.phase == "reveal":
            d.update(answer=self.answer, picks=self.picks)   # 答案與雙方選擇只在公布時送出
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
            t = (r - 1) / max(1, self.ROUNDS - 1)       # 0 → 1
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
        w = _winner(pa, pb) or _winner(ta, tb, higher_wins=False)
        return w, pa, pb, f"{pa} 分 : {pb} 分（作答 {ta / 1000:.2f} 秒 : {tb / 1000:.2f} 秒）"


# ============================================================
class TugGame(BaseGame):
    """團體拔河：兩隊全員一起點，繩結位置 pos 從 0 開始，A 隊往 -100 拉、B 隊往 +100 拉，
    先到 ±100 的隊伍贏。不計時，一定要拉到底才結束（卡住的話主持人可以作廢）。
    每一下的拉力 = PULL / 隊伍人數，所以比的是「人均點擊速度」，人數不同也公平。
    例：4 人隊每人每秒比對手多點 1 下 → 繩結每秒移動 2，約 50 秒拉到底。
    每人每秒最多計 MAX_PER_SEC 下。結束時公布兩隊 MVP（點最多下的隊員）。"""
    key = "tug"
    PULL = 2.0
    MAX_PER_SEC = 20

    def __init__(self, rnd, hub):
        super().__init__(rnd, hub)
        with db.read() as conn:
            members = {"A": db.team_members(conn, rnd["team_a"]), "B": db.team_members(conn, rnd["team_b"])}
        self.sides = {m["id"]: side for side, ms in members.items() for m in ms}
        self.names = {m["id"]: m["name"] for ms in members.values() for m in ms}
        self.size = {side: max(1, len(ms)) for side, ms in members.items()}
        self.pos = Fraction(0)      # 用精確分數：2/3 這種拉力用浮點數累加會停在 99.99999 永遠到不了 100
        self.taps = {"A": 0, "B": 0}
        self.per_user: dict[int, int] = {}
        self.recent: dict[int, deque] = {}
        self.uid = None             # Hub 在呼叫 handle() 前設定是誰點的

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
        await self.set_phase("play")              # 不給 ends_in：不計時、不顯示倒數
        while abs(self.pos) < 100:
            await asyncio.sleep(0.1)
            if self.dirty:
                await self.push()
        await self.set_phase("done")

    def outcome(self):
        w = "A" if self.pos <= -100 else "B" if self.pos >= 100 else None
        a, b = self.taps["A"], self.taps["B"]
        return w, a, b, f"拉過線！（{a} 下 : {b} 下）"


GAMES = {g.key: g for g in (TapGame, ReactionGame, StopwatchGame, MathGame, MemoryGame, FlashGame, TugGame)}


# ============================================================
class Hub:
    def __init__(self):
        self.clients: dict = {}
        self.game: BaseGame | None = None
        self._task: asyncio.Task | None = None

    async def send(self, ws, msg: dict) -> None:
        try:
            await ws.send_text(json.dumps(msg))
        except Exception:
            self.clients.pop(ws, None)

    async def broadcast(self, msg: dict) -> None:
        data = json.dumps(msg)
        for ws in list(self.clients):
            try:
                await ws.send_text(data)
            except Exception:
                self.clients.pop(ws, None)

    def handle(self, user_id: int | None, raw: str) -> None:
        g = self.game
        if g is None or user_id not in g.sides:
            return
        try:
            data = json.loads(raw)
        except ValueError:
            return
        if isinstance(data, dict):
            g.uid = user_id
            g.handle(g.sides[user_id], data)

    async def start(self, rnd) -> None:
        self.game = GAMES[rnd["game"]](rnd, self)
        self._task = asyncio.create_task(self._run(self.game))

    async def _run(self, game: BaseGame) -> None:
        try:
            await game.run()
            result, sa, sb, summary = game.outcome()
            final = await asyncio.to_thread(db.finish_pk, game.round_id, sa, sb, result)
            await self.broadcast({"type": "end", "round": game.round_id, "result": final,
                                  "summary": summary, "voided": final is None})
        finally:
            if self.game is game:
                self.game = None

    async def stop(self, round_id: int) -> None:
        """主持人在比賽中作廢：停止遊戲，不結算。"""
        if self.game and self.game.round_id == round_id:
            if self._task:
                self._task.cancel()
            self.game = None
            await self.broadcast({"type": "end", "round": round_id, "result": None,
                                  "summary": "本輪已作廢", "voided": True})


hub = Hub()
