"""PK 遊戲共用合約與工具。"""

import asyncio

COUNTDOWN = 3.0


class BaseGame:
    """一種手機小遊戲。子類別放在 pk/games/，會被自動掃描。

    必填 class 屬性：key、title、description。
    選填：team（團體賽）、engine（False = 只出現在選單、沒有即時引擎，如現場對決）、
         order（選單順序）、CLIENT_JS（擂台前端，寫入 G.<key>）。
    必實作：run()、handle()、public()、outcome()（engine=False 的除外）。
    """

    key = ""
    title = ""
    description = ""
    team = False
    engine = True
    order = 50
    CLIENT_JS = ""
    # 主持人開盤時可以調整的一個參數（關卡數或遊玩時間）；None = 不能調。
    # attr：要覆寫的 class 屬性；fmt：顯示在盤名後面的文字，例如「15 秒」。
    # 例：{"attr": "DURATION", "label": "遊玩時間（秒）", "fmt": "{} 秒", "default": 10, "min": 5, "max": 60}
    SETTING: dict | None = None

    @classmethod
    def setting_text(cls, value: int) -> str:
        return cls.SETTING["fmt"].format(value) if cls.SETTING else ""

    def apply_setting(self, value: int) -> None:
        """把主持人選的值套到這一場（只改這個物件，不影響預設值）。需要連動其他參數的遊戲可以覆寫。"""
        attr = self.SETTING["attr"]
        setattr(self, attr, type(getattr(self, attr))(value))

    def __init__(self, rnd, hub):
        self.hub = hub
        self.round_id = rnd["id"]
        self.sides = {rnd["player_a"]: "A", rnd["player_b"]: "B"}
        self.uid = None
        self.phase = "countdown"
        self.phase_end: float | None = None
        self.dirty = False
        self.changed = asyncio.Event()
        value = rnd["setting"] if "setting" in rnd.keys() else None
        if self.SETTING and value is not None:
            self.apply_setting(value)

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

    def public(self) -> dict:
        return {}

    def handle(self, side: str, data: dict) -> None:
        pass

    async def run(self) -> None:
        raise NotImplementedError

    def outcome(self) -> tuple[str | None, int, int, str]:
        raise NotImplementedError


class TeamGame(BaseGame):
    """團體賽：sides 是整隊隊員，不只有兩名選手。"""

    team = True

    def __init__(self, rnd, hub):
        super().__init__(rnd, hub)
        import db
        with db.read() as conn:
            members = {"A": db.team_members(conn, rnd["team_a"]), "B": db.team_members(conn, rnd["team_b"])}
        self.sides = {m["id"]: side for side, ms in members.items() for m in ms}
        self.names = {m["id"]: m["name"] for ms in members.values() for m in ms}
        self.size = {side: max(1, len(ms)) for side, ms in members.items()}
        self.acts: dict[int, int] = {}      # 每個人做了幾次動作（拉／跳／搖），大螢幕用來讓那個人的角色動

    def count_act(self, uid: int) -> None:
        self.acts[uid] = self.acts.get(uid, 0) + 1
        self.mark()

    def message(self) -> dict:
        msg = super().message()
        msg["acts"] = {str(u): n for u, n in self.acts.items()}
        return msg


class SoloGame(BaseGame):
    """全員賽：所有玩家都參加（不分兩邊、不開放下注），結束時依名次直接發點數。
    sides 裡每個人都是 'S'。子類別要實作 awards() → (發放清單 [(uid, 點數, 原因)], 摘要文字)。"""

    solo = True

    def __init__(self, rnd, hub):
        super().__init__(rnd, hub)
        import db
        with db.read() as conn:
            users = db.users(conn)
        self.sides = {u["id"]: "S" for u in users}
        self.names = {u["id"]: u["name"] for u in users}

    def online(self) -> list[int]:
        """目前有連線（開著頁面）的參賽者。"""
        on = {uid for uid in self.hub.clients.values() if uid in self.sides}
        return [u for u in self.sides if u in on]

    def awards(self) -> tuple[list[tuple[int, int, str]], str]:
        raise NotImplementedError

    def outcome(self):
        return None, 0, 0, ""


def winner(a, b, higher_wins=True):
    if a == b:
        return None
    return ("A" if a > b else "B") if higher_wins else ("A" if a < b else "B")
