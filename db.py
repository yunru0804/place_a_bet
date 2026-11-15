"""SQLite 存取層。

點數不另外存欄位，而是推算出來：
  餘額 = 1000
       + 已結算且押對的 amount
       - 已結算且押錯的 amount
       - 開盤中/已關盤（尚未結算）的 amount   ← 下注當下先扣
       + adjustments 的 delta 總和            ← 主賽獎勵、小遊戲得分等
  作廢的輪次不計入（等於全額退還）。
"""

import os
import secrets
import sqlite3
from contextlib import contextmanager

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
INSTANCE_DIR = os.path.join(BASE_DIR, "instance")
DB_PATH = os.environ.get("BET_DB", os.path.join(INSTANCE_DIR, "bets.db"))

INITIAL_POINTS = 1000
MAX_BET = 1000
MIN_BET = 10
MAIN_MATCH_PRIZE = 500
TEAM_MATCH_PRIZE = 500      # 團體賽每一場的勝隊，每位成員自動 +500

# 順序即顯示順序
TEAMS = {"red": "紅隊", "yellow": "黃隊", "green": "綠隊", "blue": "藍隊"}

# 遊戲名稱與說明在 pk/games/ 各檔；延遲轉接，避免啟動時跟遊戲模組互抓。
def _catalog(name):
    from pk.registry import GAME_INFO, PK_GAMES, TEAM_GAMES
    return {"PK_GAMES": PK_GAMES, "TEAM_GAMES": TEAM_GAMES, "GAME_INFO": GAME_INFO}[name]


def __getattr__(name):
    if name in ("PK_GAMES", "TEAM_GAMES", "GAME_INFO"):
        return _catalog(name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    id          INTEGER PRIMARY KEY,
    name        TEXT NOT NULL UNIQUE,
    token       TEXT NOT NULL UNIQUE,
    team        TEXT,
    created_at  TEXT NOT NULL DEFAULT (datetime('now', 'localtime'))
);

-- 點數異動：主賽獎勵、小遊戲得分、手動調整。batch 用來把「發給整隊」的多筆歸成一組（可整組撤銷）
CREATE TABLE IF NOT EXISTS adjustments (
    id          INTEGER PRIMARY KEY,
    user_id     INTEGER NOT NULL REFERENCES users(id),
    delta       INTEGER NOT NULL,
    reason      TEXT NOT NULL,
    target      TEXT NOT NULL,
    batch       TEXT NOT NULL,
    created_at  TEXT NOT NULL DEFAULT (datetime('now', 'localtime'))
);
CREATE INDEX IF NOT EXISTS adjustments_user ON adjustments (user_id);

-- kind: normal = 一般 A/B 盤；pk = 投票選 2 人 → 下注 → 連打對決 → 自動結算
-- status 流程：
--   normal: open → locked → settled
--   pk:     voting → open → locked → playing → settled
--   任一未結算狀態都可 → void（全額退還）
CREATE TABLE IF NOT EXISTS rounds (
    id          INTEGER PRIMARY KEY,
    title       TEXT NOT NULL,
    option_a    TEXT NOT NULL,
    option_b    TEXT NOT NULL,
    kind        TEXT NOT NULL DEFAULT 'normal' CHECK (kind IN ('normal', 'pk')),
    game        TEXT,
    status      TEXT NOT NULL CHECK (status IN ('voting', 'open', 'locked', 'playing', 'settled', 'void')),
    result      TEXT CHECK (result IN ('A', 'B')),
    player_a    INTEGER,
    player_b    INTEGER,
    score_a     INTEGER,
    score_b     INTEGER,
    created_at  TEXT NOT NULL DEFAULT (datetime('now', 'localtime')),
    closed_at   TEXT,
    settled_at  TEXT
);

-- 同一時間最多只能有一輪尚未結束
CREATE UNIQUE INDEX IF NOT EXISTS one_active_round
    ON rounds ((1)) WHERE status IN ('voting', 'open', 'locked', 'playing');

-- PK 盤投票：一人一票（可改票）
CREATE TABLE IF NOT EXISTS votes (
    round_id     INTEGER NOT NULL,
    voter_id     INTEGER NOT NULL,
    candidate_id INTEGER NOT NULL,
    PRIMARY KEY (round_id, voter_id)
);

-- epoch：每次「重新開始」換一個新值，讓玩家端的開獎彈窗記錄跟著重置
CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
INSERT OR IGNORE INTO meta (key, value) VALUES ('epoch', '0');

CREATE TABLE IF NOT EXISTS bets (
    id          INTEGER PRIMARY KEY,
    user_id     INTEGER NOT NULL REFERENCES users(id),
    round_id    INTEGER NOT NULL REFERENCES rounds(id),
    choice      TEXT NOT NULL CHECK (choice IN ('A', 'B')),
    amount      INTEGER NOT NULL CHECK (amount > 0),
    updated_at  TEXT NOT NULL DEFAULT (datetime('now', 'localtime')),
    UNIQUE (user_id, round_id)
);
"""

BALANCE_SQL = f"""
SELECT u.id, u.name, u.token, u.team,
       {INITIAL_POINTS} + COALESCE(SUM(CASE
           WHEN r.status = 'settled' AND b.choice = r.result THEN  b.amount
           WHEN r.status = 'settled'                         THEN -b.amount
           WHEN r.status IN ('open', 'locked', 'playing')    THEN -b.amount
           ELSE 0 END), 0)
       + (SELECT COALESCE(SUM(a.delta), 0) FROM adjustments a WHERE a.user_id = u.id) AS balance,
       COALESCE(SUM(CASE WHEN r.status IN ('open', 'locked', 'playing')
                         THEN b.amount ELSE 0 END), 0) AS pending
FROM users u
LEFT JOIN bets b   ON b.user_id = u.id
LEFT JOIN rounds r ON r.id = b.round_id
"""


class BetError(Exception):
    """給使用者看的錯誤訊息。"""


def connect() -> sqlite3.Connection:
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    conn = sqlite3.connect(DB_PATH, isolation_level=None, timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA busy_timeout = 10000")
    return conn


def init_db() -> None:
    conn = connect()
    conn.execute("PRAGMA journal_mode = WAL")
    conn.executescript(SCHEMA)
    # 舊資料庫的 users 沒有 team 欄位 → 補上（既有資料保留）
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(users)")}
    if "team" not in cols:
        conn.execute("ALTER TABLE users ADD COLUMN team TEXT")
    _migrate_rounds_for_pk(conn)
    round_cols = {r["name"] for r in conn.execute("PRAGMA table_info(rounds)")}
    if "game" not in round_cols:
        conn.execute("ALTER TABLE rounds ADD COLUMN game TEXT")
        conn.execute("UPDATE rounds SET game = 'tap' WHERE kind = 'pk'")
    for col in ("team_a", "team_b"):
        if col not in round_cols:
            conn.execute(f"ALTER TABLE rounds ADD COLUMN {col} TEXT")
    conn.close()


def _migrate_rounds_for_pk(conn) -> None:
    """舊版 rounds 表沒有 PK 欄位、status 也不允許 voting/playing → 重建表格（既有資料保留）。"""
    sql = conn.execute("SELECT sql FROM sqlite_master WHERE type = 'table' AND name = 'rounds'").fetchone()[0]
    if "'voting'" in sql:
        return
    old_cols = "id, title, option_a, option_b, status, result, created_at, closed_at, settled_at"
    new_table = SCHEMA.split("CREATE TABLE IF NOT EXISTS rounds", 1)[1].split(";", 1)[0]
    conn.execute("PRAGMA foreign_keys = OFF")  # 重建期間 bets 的外鍵暫時不檢查
    conn.executescript(f"""
        BEGIN IMMEDIATE;
        CREATE TABLE rounds_new {new_table};
        INSERT INTO rounds_new ({old_cols}) SELECT {old_cols} FROM rounds;
        DROP TABLE rounds;
        ALTER TABLE rounds_new RENAME TO rounds;
        CREATE UNIQUE INDEX one_active_round
            ON rounds ((1)) WHERE status IN ('voting', 'open', 'locked', 'playing');
        COMMIT;
    """)
    conn.execute("PRAGMA foreign_keys = ON")


@contextmanager
def read():
    conn = connect()
    try:
        yield conn
    finally:
        conn.close()


@contextmanager
def write():
    """BEGIN IMMEDIATE：取得寫入鎖，避免「關盤」與「下注」同時發生時的競態。"""
    conn = connect()
    try:
        conn.execute("BEGIN IMMEDIATE")
        yield conn
        conn.execute("COMMIT")
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    finally:
        conn.close()


# ---------- 查詢 ----------

def get_user_by_token(conn, token: str):
    return conn.execute(BALANCE_SQL + " WHERE u.token = ? GROUP BY u.id", (token,)).fetchone()


def get_user(conn, user_id: int):
    return conn.execute(BALANCE_SQL + " WHERE u.id = ? GROUP BY u.id", (user_id,)).fetchone()


def leaderboard(conn):
    """依總點數（餘額＋押注中）排序；同分並列、下一名次跳號（1, 1, 3）。"""
    rows = conn.execute(
        BALANCE_SQL + " GROUP BY u.id ORDER BY balance + pending DESC, u.name"
    ).fetchall()
    board, prev_total, rank = [], None, 0
    for i, r in enumerate(rows, start=1):
        total = r["balance"] + r["pending"]
        if total != prev_total:
            rank, prev_total = i, total
        board.append({**dict(r), "total": total, "rank": rank})
    return board


ACTIVE = ("voting", "open", "locked", "playing")


def active_round(conn):
    return conn.execute(
        "SELECT * FROM rounds WHERE status IN ('voting', 'open', 'locked', 'playing')"
    ).fetchone()


def get_vote(conn, round_id: int, voter_id: int):
    return conn.execute("SELECT candidate_id FROM votes WHERE round_id = ? AND voter_id = ?",
                        (round_id, voter_id)).fetchone()


def vote_tally(conn, round_id: int):
    """所有玩家與得票數（得票多的在前）。voted = 這個人自己投過票沒。"""
    return conn.execute(
        """SELECT u.id, u.name, u.team,
                  (SELECT COUNT(*) FROM votes v WHERE v.round_id = ? AND v.candidate_id = u.id) AS votes,
                  EXISTS (SELECT 1 FROM votes v WHERE v.round_id = ? AND v.voter_id = u.id) AS voted
           FROM users u ORDER BY votes DESC, u.name""",
        (round_id, round_id),
    ).fetchall()


def get_round(conn, round_id: int):
    return conn.execute("SELECT * FROM rounds WHERE id = ?", (round_id,)).fetchone()


def get_bet(conn, user_id: int, round_id: int):
    return conn.execute(
        "SELECT * FROM bets WHERE user_id = ? AND round_id = ?", (user_id, round_id)
    ).fetchone()


def user_history(conn, user_id: int):
    """已結算的下注 + 點數異動，新的在前。key 用來判斷玩家看過哪些結果（開獎彈窗）。"""
    return conn.execute(
        """SELECT * FROM (
             SELECT 'bet' AS kind, 'r' || r.id AS key, r.id, r.settled_at AS at,
                    r.title, r.option_a, r.option_b, r.status, r.result,
                    b.choice, b.amount, NULL AS delta
             FROM bets b JOIN rounds r ON r.id = b.round_id
             WHERE b.user_id = ? AND r.status IN ('settled', 'void')
             UNION ALL
             SELECT 'award', 'a' || a.id, a.id, a.created_at,
                    a.reason, NULL, NULL, NULL, NULL, NULL, NULL, a.delta
             FROM adjustments a WHERE a.user_id = ?
           ) ORDER BY at DESC, key DESC""",
        (user_id, user_id),
    ).fetchall()


def adjustments_version(conn, user_id: int) -> str:
    """玩家頁輪詢用：點數異動有增減時這個值會變。"""
    r = conn.execute("SELECT COUNT(*) AS n, COALESCE(MAX(id), 0) AS m FROM adjustments WHERE user_id = ?",
                     (user_id,)).fetchone()
    return f"{r['n']}:{r['m']}"


def recent_awards(conn, limit: int = 30):
    return conn.execute(
        """SELECT batch, target, reason, delta, COUNT(*) AS n, MIN(created_at) AS at
           FROM adjustments GROUP BY batch ORDER BY MIN(id) DESC LIMIT ?""",
        (limit,),
    ).fetchall()


def team_counts(conn) -> dict:
    rows = conn.execute("SELECT team, COUNT(*) AS n FROM users WHERE team IS NOT NULL GROUP BY team")
    return {r["team"]: r["n"] for r in rows}


def round_bets(conn, round_id: int):
    """該輪所有玩家（含沒下注的）及其下注。"""
    return conn.execute(
        """SELECT u.id AS user_id, u.name, u.team, b.choice, b.amount, b.updated_at
           FROM users u
           LEFT JOIN bets b ON b.user_id = u.id AND b.round_id = ?
           ORDER BY b.choice IS NULL, b.choice, b.amount DESC, u.name""",
        (round_id,),
    ).fetchall()


def past_rounds(conn):
    return conn.execute(
        """SELECT r.*,
                  COALESCE(SUM(CASE WHEN b.choice = 'A' THEN b.amount END), 0) AS total_a,
                  COALESCE(SUM(CASE WHEN b.choice = 'B' THEN b.amount END), 0) AS total_b,
                  COUNT(b.id) AS bet_count
           FROM rounds r LEFT JOIN bets b ON b.round_id = r.id
           WHERE r.status IN ('settled', 'void')
           GROUP BY r.id ORDER BY r.id DESC"""
    ).fetchall()


def users(conn):
    return conn.execute("SELECT * FROM users ORDER BY id").fetchall()


# ---------- 玩家動作 ----------

def place_bet(user_id: int, choice: str | None, amount: int) -> str:
    with write() as conn:
        rnd = active_round(conn)
        if rnd is None or rnd["status"] != "open":
            raise BetError("目前沒有開盤中的輪次，無法下注。")

        # PK 盤：被選中的兩人只能押自己；團體賽：兩隊隊員只能押自己的隊
        if rnd["kind"] == "pk":
            own = my_side(conn, rnd, user_id)
            if own and choice != own:
                raise BetError("你的隊伍正在比賽，只能押自己的隊。" if rnd["game"] in _catalog("TEAM_GAMES")
                               else "你是 PK 選手，只能押自己。")

        user = get_user(conn, user_id)
        existing = get_bet(conn, user_id, rnd["id"])
        # 餘額已扣掉本輪舊注，所以可用額度要加回來
        available = user["balance"] + (existing["amount"] if existing else 0)
        limit = min(MAX_BET, available)

        if amount < MIN_BET:
            raise BetError(f"最少要押 {MIN_BET} 點。")
        if amount > limit:
            raise BetError(f"最多只能押 {limit} 點。")

        if choice not in ("A", "B"):
            raise BetError("請選擇 A 或 B。")

        conn.execute(
            """INSERT INTO bets (user_id, round_id, choice, amount) VALUES (?, ?, ?, ?)
               ON CONFLICT (user_id, round_id) DO UPDATE SET
                   choice = excluded.choice, amount = excluded.amount,
                   updated_at = datetime('now', 'localtime')""",
            (user_id, rnd["id"], choice, amount),
        )
        label = rnd["option_a"] if choice == "A" else rnd["option_b"]
        return f"{'已修改' if existing else '已下注'}：{choice}（{label}）{amount} 點。"


def my_side(conn, rnd, user_id: int | None) -> str | None:
    """這位玩家在這一輪是 A 方、B 方，還是旁觀（None）。"""
    if rnd is None or rnd["kind"] != "pk" or user_id is None:
        return None
    if rnd["game"] in _catalog("TEAM_GAMES"):
        row = conn.execute("SELECT team FROM users WHERE id = ?", (user_id,)).fetchone()
        team = row["team"] if row else None
        return {rnd["team_a"]: "A", rnd["team_b"]: "B"}.get(team) if team else None
    return {rnd["player_a"]: "A", rnd["player_b"]: "B"}.get(user_id)


def team_members(conn, team: str):
    return conn.execute("SELECT id, name FROM users WHERE team = ? ORDER BY id", (team,)).fetchall()


# ---------- 管理員動作 ----------

def create_round(title: str, option_a: str, option_b: str, kind: str = "normal",
                 game: str | None = None, team_a: str | None = None, team_b: str | None = None) -> None:
    title, option_a, option_b = title.strip(), option_a.strip(), option_b.strip()
    pk_games, team_games = _catalog("PK_GAMES"), _catalog("TEAM_GAMES")
    if kind == "team":
        game = game or next(iter(team_games), None)
        if game not in team_games:
            raise BetError("請選擇團體賽遊戲。")
        if team_a not in TEAMS or team_b not in TEAMS or team_a == team_b:
            raise BetError("請選兩支不同的隊伍。")
        with read() as conn:
            for t in (team_a, team_b):
                if not team_members(conn, t):
                    raise BetError(f"{TEAMS[t]}沒有任何隊員。")
        option_a, option_b = TEAMS[team_a], TEAMS[team_b]
        title = f"{team_games[game][0]}：{option_a} vs {option_b}"
        kind, status = "pk", "locked"   # 團體賽不投票、不下注，直接等主持人開始
    elif kind == "pk":
        team_a = team_b = None
        if game not in pk_games:
            raise BetError("請選擇 PK 遊戲。")
        title = pk_games[game][0]      # 題目就是遊戲名稱
        option_a = option_b = ""       # 投票結束後才會填入兩位選手的名字
        status = "voting"
    elif kind == "normal":
        if not (title and option_a and option_b):
            raise BetError("題目與兩個選項都必須填寫。")
        status = "open"
        game = team_a = team_b = None
    else:
        raise BetError("盤的種類不正確。")
    with write() as conn:
        if active_round(conn):
            raise BetError("目前還有未結算的輪次，請先結算或作廢。")
        conn.execute(
            """INSERT INTO rounds (title, option_a, option_b, kind, game, status, team_a, team_b)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (title, option_a, option_b, kind, game, status, team_a, team_b),
        )


def cast_vote(voter_id: int, candidate_id: int) -> str:
    with write() as conn:
        rnd = active_round(conn)
        if rnd is None or rnd["status"] != "voting":
            raise BetError("目前不是投票時間。")
        if candidate_id == voter_id:
            raise BetError("不能投給自己。")
        cand = conn.execute("SELECT name FROM users WHERE id = ?", (candidate_id,)).fetchone()
        if cand is None:
            raise BetError("找不到這位玩家。")
        conn.execute(
            """INSERT INTO votes (round_id, voter_id, candidate_id) VALUES (?, ?, ?)
               ON CONFLICT (round_id, voter_id) DO UPDATE SET candidate_id = excluded.candidate_id""",
            (rnd["id"], voter_id, candidate_id),
        )
        return f"已投給「{cand['name']}」。"


def close_voting(round_id: int) -> tuple[str, str]:
    """票數最高的兩人進 PK（同票隨機抽）。回傳 (A 名字, B 名字)。"""
    import random
    with write() as conn:
        rnd = get_round(conn, round_id)
        if rnd is None or rnd["status"] != "voting":
            raise BetError("這一輪不是投票中。")
        tally = vote_tally(conn, round_id)
        if len(tally) < 2:
            raise BetError("至少要有 2 位玩家才能 PK。")
        ranked = sorted(tally, key=lambda r: (-r["votes"], random.random()))
        a, b = ranked[0], ranked[1]
        conn.execute(
            """UPDATE rounds SET status = 'open', player_a = ?, player_b = ?, option_a = ?, option_b = ?
               WHERE id = ?""",
            (a["id"], b["id"], a["name"], b["name"], round_id),
        )
        return a["name"], b["name"]


def start_pk(round_id: int):
    """已關盤的 PK 盤 → 比賽中。回傳該輪資料。"""
    with write() as conn:
        rnd = get_round(conn, round_id)
        if rnd is None or rnd["kind"] != "pk" or rnd["status"] != "locked":
            raise BetError("必須是已關盤的 PK 盤才能開始。")
        if rnd["game"] == "live":
            raise BetError("現場對決請直接按「A 贏 / B 贏」結算。")
        conn.execute("UPDATE rounds SET status = 'playing' WHERE id = ?", (round_id,))
        return get_round(conn, round_id)


def finish_pk(round_id: int, score_a: int, score_b: int, result: str | None) -> str | None:
    """依遊戲判定自動結算；result 為 None（平手）則作廢退還。回傳實際結果。
    團體賽：勝隊每位成員自動 +TEAM_MATCH_PRIZE（記在發放紀錄，可撤銷）。"""
    with write() as conn:
        rnd = get_round(conn, round_id)
        if rnd is None or rnd["status"] != "playing":
            return None  # 期間被作廢了
        conn.execute(
            """UPDATE rounds SET status = ?, result = ?, score_a = ?, score_b = ?,
                                 settled_at = datetime('now', 'localtime') WHERE id = ?""",
            ("settled" if result else "void", result, score_a, score_b, round_id),
        )
        team_games = _catalog("TEAM_GAMES")
        if result and rnd["game"] in team_games:
            team = rnd["team_a"] if result == "A" else rnd["team_b"]
            members = team_members(conn, team)
            reason = f"{team_games[rnd['game']][0]}勝利（{TEAMS[team]}）"
            batch = secrets.token_hex(6)
            conn.executemany(
                "INSERT INTO adjustments (user_id, delta, reason, target, batch) VALUES (?, ?, ?, ?, ?)",
                [(m["id"], TEAM_MATCH_PRIZE, reason, TEAMS[team], batch) for m in members],
            )
    return result


def _transition(round_id: int, frm: tuple[str, ...], sql: str, params: tuple, err: str):
    with write() as conn:
        rnd = get_round(conn, round_id)
        if rnd is None or rnd["status"] not in frm:
            raise BetError(err)
        conn.execute(sql, params)


def lock_round(round_id: int) -> None:
    _transition(round_id, ("open",),
                "UPDATE rounds SET status = 'locked', closed_at = datetime('now', 'localtime') WHERE id = ?",
                (round_id,), "這一輪不是開盤中，無法關盤。")


def reopen_round(round_id: int) -> None:
    with read() as conn:
        rnd = get_round(conn, round_id)
    if rnd is not None and rnd["game"] in _catalog("TEAM_GAMES"):
        raise BetError("團體賽不開放下注。")
    _transition(round_id, ("locked",),
                "UPDATE rounds SET status = 'open', closed_at = NULL WHERE id = ?",
                (round_id,), "這一輪不是已關盤狀態，無法重新開盤。")


def settle_round(round_id: int, result: str) -> None:
    if result not in ("A", "B"):
        raise BetError("結果必須是 A 或 B。")
    with read() as conn:
        rnd = get_round(conn, round_id)
    if rnd is not None and rnd["kind"] == "pk" and rnd["game"] != "live":
        raise BetError("PK 盤會依比賽結果自動結算。")
    _transition(round_id, ("locked",),
                "UPDATE rounds SET status = 'settled', result = ?, settled_at = datetime('now', 'localtime') WHERE id = ?",
                (result, round_id), "必須先關盤才能結算。")


def void_round(round_id: int) -> None:
    _transition(round_id, ACTIVE,
                "UPDATE rounds SET status = 'void', settled_at = datetime('now', 'localtime') WHERE id = ?",
                (round_id,), "只有未結算的輪次可以作廢。")


def _check_team(team: str | None) -> str | None:
    team = team or None
    if team is not None and team not in TEAMS:
        raise BetError("隊伍不正確。")
    return team


def create_user(name: str, team: str | None = None) -> None:
    name = name.strip()
    if not name:
        raise BetError("請輸入名字。")
    team = _check_team(team)
    with write() as conn:
        if conn.execute("SELECT 1 FROM users WHERE name = ?", (name,)).fetchone():
            raise BetError(f"「{name}」已經存在。")
        conn.execute("INSERT INTO users (name, token, team) VALUES (?, ?, ?)",
                     (name, secrets.token_urlsafe(16), team))


def set_team(user_id: int, team: str | None) -> None:
    team = _check_team(team)
    with write() as conn:
        conn.execute("UPDATE users SET team = ? WHERE id = ?", (team, user_id))


def award(target: str, amount: int, reason: str) -> str:
    """發點數。target = 'team:<red|yellow|green|blue>' 或 'user:<id>'；amount 可為負數（扣點）。"""
    reason = reason.strip()
    if not reason:
        raise BetError("請填寫原因。")
    if amount == 0:
        raise BetError("點數不能是 0。")
    kind, _, value = target.partition(":")
    with write() as conn:
        if kind == "team" and value in TEAMS:
            members = conn.execute("SELECT id FROM users WHERE team = ?", (value,)).fetchall()
            label = TEAMS[value]
        elif kind == "user" and value.isdigit():
            members = conn.execute("SELECT id, name FROM users WHERE id = ?", (int(value),)).fetchall()
            label = members[0]["name"] if members else ""
        else:
            raise BetError("請選擇發放對象。")
        if not members:
            raise BetError(f"{label or '對象'}沒有任何玩家。")
        batch = secrets.token_hex(6)
        conn.executemany(
            "INSERT INTO adjustments (user_id, delta, reason, target, batch) VALUES (?, ?, ?, ?, ?)",
            [(m["id"], amount, reason, label, batch) for m in members],
        )
    sign = "+" if amount > 0 else ""
    return f"已發放：{label}（{len(members)} 人）各 {sign}{amount} 點，原因：{reason}。"


def undo_award(batch: str) -> None:
    with write() as conn:
        if conn.execute("DELETE FROM adjustments WHERE batch = ?", (batch,)).rowcount == 0:
            raise BetError("找不到這筆發放紀錄。")


def get_epoch(conn) -> str:
    row = conn.execute("SELECT value FROM meta WHERE key = 'epoch'").fetchone()
    return row["value"] if row else "0"


def backup(tag: str) -> str:
    """把目前的資料庫完整複製到 instance/backups/，回傳備份檔路徑。"""
    from datetime import datetime
    folder = os.path.join(os.path.dirname(DB_PATH), "backups")
    os.makedirs(folder, exist_ok=True)
    path = os.path.join(folder, f"bets-{datetime.now():%Y%m%d-%H%M%S}-{tag}.db")
    src, dst = connect(), sqlite3.connect(path)
    try:
        src.backup(dst)
    finally:
        dst.close()
        src.close()
    return path


def reset_game() -> str:
    """清空所有輪次、下注與點數異動（所有人回到初始點數），保留玩家、隊伍與連結。"""
    path = backup("before-reset")
    with write() as conn:
        conn.execute("DELETE FROM adjustments")
        conn.execute("DELETE FROM votes")
        conn.execute("DELETE FROM bets")
        conn.execute("DELETE FROM rounds")
        conn.execute("UPDATE meta SET value = ? WHERE key = 'epoch'", (secrets.token_hex(4),))
    return path


def delete_user(user_id: int) -> str:
    """刪除玩家與其所有下注（每人點數獨立計算，不影響其他玩家）。回傳玩家名字。"""
    with read() as conn:
        user = conn.execute("SELECT name FROM users WHERE id = ?", (user_id,)).fetchone()
    if user is None:
        raise BetError("找不到這位玩家。")
    backup("before-delete")
    with write() as conn:
        conn.execute("DELETE FROM adjustments WHERE user_id = ?", (user_id,))
        conn.execute("DELETE FROM votes WHERE voter_id = ? OR candidate_id = ?", (user_id, user_id))
        conn.execute("DELETE FROM bets WHERE user_id = ?", (user_id,))
        conn.execute("DELETE FROM users WHERE id = ?", (user_id,))
    return user["name"]


def reset_token(user_id: int) -> None:
    with write() as conn:
        conn.execute("UPDATE users SET token = ? WHERE id = ?",
                     (secrets.token_urlsafe(16), user_id))
