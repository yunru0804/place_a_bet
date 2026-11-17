"""娛樂城：玩家自己玩的拉霸機、骰寶比大小、撲克猜大小。

- 用的是真實點數（跟下注同一個餘額），輸贏會反映在排行榜。
- 亂數、判定、派彩全部在伺服器做，前端只負責動畫。
- 佛心賭場：期望值偏向玩家（拉霸約 112%、骰寶大小 110%、猜大小每關 108%），玩越多越有機會賺。
"""

import json
import secrets

import db
from db import BetError

CASINO_MIN_BET = 10
CASINO_MAX_BET = None         # None = 沒有上限，可以 ALL IN（只受餘額限制）

_rng = secrets.SystemRandom()

# ---------------- 拉霸機 ----------------
# 三輪各自獨立抽，權重越低越稀有。回報率約 112%，每轉有約 52% 的機率贏錢（兩個一樣就有賺）。
SLOT_WEIGHTS = {"🍒": 6, "🍋": 5, "🍇": 4, "🔔": 3, "⭐": 2, "7️⃣": 1}
SLOT_PAY3 = {"🍒": 7, "🍋": 6, "🍇": 8, "🔔": 12, "⭐": 20, "7️⃣": 50}
SLOT_PAY2 = {"🍒": 1.5, "🍋": 1.5, "🍇": 1.5, "🔔": 2, "⭐": 2, "7️⃣": 3}

# ---------------- 骰寶 ----------------
# 大 11–18、小 3–10，各 50%，押中拿回 2.2 倍（豹子不通殺）。押豹子（三顆一樣，1/36）拿回 41 倍。
SICBO_PAY = {"big": 2.2, "small": 2.2, "triple": 41}     # 拿回的總倍數（含本金）

# ---------------- 猜大小 ----------------
# 每次翻一張 A–K，猜下一張比較大、比較小，或是「一樣」。
# - 猜大／猜小中了：倍數 × (1.08 × 12 / 會贏的牌數)
# - 猜大／猜小時抽到一樣大：幸運牌！倍數 × HILO_LUCKY，繼續猜
# - 猜「一樣」（機率 1/13）：中了倍數 × HILO_SAME，沒中就輸
# 累積到 HILO_CAP 倍自動收手。
HILO_EDGE = 1.08
HILO_LUCKY = 1.5
HILO_SAME = 12
HILO_CAP = 20
RANKS = ["A", "2", "3", "4", "5", "6", "7", "8", "9", "10", "J", "Q", "K"]
SUITS = ["♠", "♥", "♦", "♣"]


def _card():
    return {"r": _rng.randint(1, 13), "s": _rng.choice(SUITS)}


def _hilo_step(rank: int, guess: str) -> float:
    if guess == "same":
        return float(HILO_SAME)
    # 一樣大另外算幸運牌，所以只看另外 12 張：會贏的機率 = wins / 12
    wins = 13 - rank if guess == "hi" else rank - 1
    return 0.0 if wins == 0 else HILO_EDGE * 12 / wins


def rules() -> dict:
    """前端顯示用的賠率表。"""
    return {
        "min": CASINO_MIN_BET, "max": CASINO_MAX_BET,
        "slot": [{"s": s, "three": SLOT_PAY3[s], "two": SLOT_PAY2[s]} for s in reversed(list(SLOT_WEIGHTS))],
        "hilo_cap": HILO_CAP, "hilo_lucky": HILO_LUCKY, "hilo_same": HILO_SAME,
    }


def _check_bet(conn, user_id: int, bet: int) -> None:
    if not isinstance(bet, int) or bet < CASINO_MIN_BET:
        raise BetError(f"最少要押 {CASINO_MIN_BET} 點。")
    if CASINO_MAX_BET is not None and bet > CASINO_MAX_BET:
        raise BetError(f"娛樂城每手最多押 {CASINO_MAX_BET} 點。")
    if db.get_user(conn, user_id)["balance"] < bet:
        raise BetError("籌碼不夠。")


def _balance(conn, user_id: int) -> int:
    return db.get_user(conn, user_id)["balance"]


def _record(conn, user_id, game, bet, payout, state) -> None:
    conn.execute("INSERT INTO casino (user_id, game, bet, payout, state) VALUES (?, ?, ?, ?, ?)",
                 (user_id, game, bet, payout, json.dumps(state, ensure_ascii=False)))


def spin(user_id: int, bet: int) -> dict:
    with db.write() as conn:
        _check_bet(conn, user_id, bet)
        syms, weights = list(SLOT_WEIGHTS), list(SLOT_WEIGHTS.values())
        reels = _rng.choices(syms, weights=weights, k=3)
        mult, line = 0, None
        if reels[0] == reels[1] == reels[2]:
            mult, line = SLOT_PAY3[reels[0]], "three"
        else:
            for s in set(reels):
                if reels.count(s) == 2:
                    mult, line = SLOT_PAY2[s], "two"
        payout = int(bet * mult)
        _record(conn, user_id, "slot", bet, payout, {"reels": reels})
        return {"reels": reels, "mult": mult, "line": line, "bet": bet, "payout": payout,
                "balance": _balance(conn, user_id)}


def sicbo(user_id: int, bet: int, pick: str) -> dict:
    if pick not in SICBO_PAY:
        raise BetError("請選擇大、小或豹子。")
    with db.write() as conn:
        _check_bet(conn, user_id, bet)
        dice = [_rng.randint(1, 6) for _ in range(3)]
        total = sum(dice)
        triple = dice[0] == dice[1] == dice[2]
        size = "big" if total >= 11 else "small"
        win = triple if pick == "triple" else pick == size
        outcome = "triple" if triple else size
        payout = int(bet * SICBO_PAY[pick]) if win else 0
        _record(conn, user_id, "sicbo", bet, payout, {"dice": dice, "pick": pick})
        return {"dice": dice, "total": total, "outcome": outcome, "size": size, "pick": pick, "bet": bet,
                "payout": payout, "balance": _balance(conn, user_id)}


# ---------------- 猜大小：可以連猜，所以要存進行中的狀態 ----------------

def _open_hilo(conn, user_id: int):
    return conn.execute("SELECT * FROM casino WHERE user_id = ? AND status = 'open' AND game = 'hilo'",
                        (user_id,)).fetchone()


def _hilo_view(row, balance: int, **extra) -> dict:
    st = json.loads(row["state"])
    mult = st["mult"]
    return {"id": row["id"], "bet": row["bet"], "card": st["card"], "mult": round(mult, 2),
            "steps": st["steps"], "cashout": int(row["bet"] * mult),
            "next_hi": round(st["mult"] * _hilo_step(st["card"]["r"], "hi"), 2),
            "next_lo": round(st["mult"] * _hilo_step(st["card"]["r"], "lo"), 2),
            "next_same": round(min(HILO_CAP, st["mult"] * HILO_SAME), 2),
            "balance": balance, **extra}


def hilo_current(conn, user_id: int) -> dict | None:
    row = _open_hilo(conn, user_id)
    return _hilo_view(row, _balance(conn, user_id), status="open") if row else None


def hilo_start(user_id: int, bet: int) -> dict:
    with db.write() as conn:
        if _open_hilo(conn, user_id):
            raise BetError("你還有一局猜大小沒結束。")
        _check_bet(conn, user_id, bet)
        state = {"card": _card(), "mult": 1.0, "steps": 0}
        conn.execute("INSERT INTO casino (user_id, game, bet, status, state) VALUES (?, 'hilo', ?, 'open', ?)",
                     (user_id, bet, json.dumps(state)))
        return _hilo_view(_open_hilo(conn, user_id), _balance(conn, user_id), status="open")


def _hilo_close(conn, row, state, payout) -> None:
    conn.execute("UPDATE casino SET status = 'done', payout = ?, state = ? WHERE id = ?",
                 (payout, json.dumps(state), row["id"]))


def hilo_guess(user_id: int, guess: str) -> dict:
    if guess not in ("hi", "lo", "same"):
        raise BetError("請選擇大、小或一樣。")
    with db.write() as conn:
        row = _open_hilo(conn, user_id)
        if row is None:
            raise BetError("沒有進行中的牌局，請先下注。")
        st = json.loads(row["state"])
        step = _hilo_step(st["card"]["r"], guess)
        if step == 0:
            raise BetError("這張牌不可能再" + ("大" if guess == "hi" else "小") + "了，換一邊猜。")
        prev, nxt = st["card"], _card()
        lucky = False
        if guess == "same":
            ok = nxt["r"] == prev["r"]
        elif nxt["r"] == prev["r"]:          # 猜大／小卻抽到一樣：幸運牌
            ok, lucky, step = True, True, HILO_LUCKY
        else:
            ok = nxt["r"] > prev["r"] if guess == "hi" else nxt["r"] < prev["r"]
        st["card"] = nxt
        if not ok:
            _hilo_close(conn, row, st, 0)
            return {"status": "lose", "prev": prev, "card": nxt, "guess": guess, "bet": row["bet"],
                    "payout": 0, "balance": _balance(conn, user_id)}
        st["mult"] = min(HILO_CAP, st["mult"] * step)
        st["steps"] += 1
        if st["mult"] >= HILO_CAP:            # 封頂自動收手
            payout = int(row["bet"] * HILO_CAP)
            _hilo_close(conn, row, st, payout)
            return {"status": "cap", "prev": prev, "card": nxt, "guess": guess, "bet": row["bet"],
                    "mult": HILO_CAP, "payout": payout, "lucky": lucky, "balance": _balance(conn, user_id)}
        conn.execute("UPDATE casino SET state = ? WHERE id = ?", (json.dumps(st), row["id"]))
        row = _open_hilo(conn, user_id)
        return _hilo_view(row, _balance(conn, user_id), status="open", prev=prev, guess=guess,
                          lucky=lucky, step=round(step, 2))


def hilo_cashout(user_id: int) -> dict:
    with db.write() as conn:
        row = _open_hilo(conn, user_id)
        if row is None:
            raise BetError("沒有進行中的牌局。")
        st = json.loads(row["state"])
        if st["steps"] == 0:
            raise BetError("至少要猜中一次才能收手。")
        payout = int(row["bet"] * st["mult"])
        _hilo_close(conn, row, st, payout)
        return {"status": "cashout", "bet": row["bet"], "mult": round(st["mult"], 2), "payout": payout,
                "card": st["card"], "balance": _balance(conn, user_id)}


# ---------------- 紀錄與統計 ----------------

def recent(conn, user_id: int, limit: int = 12):
    return [dict(r) for r in conn.execute(
        """SELECT id, game, bet, payout, created_at FROM casino
           WHERE user_id = ? AND status = 'done' ORDER BY id DESC LIMIT ?""", (user_id, limit))]


def summary(conn):
    """後台用：每位玩家在娛樂城玩了幾手、淨輸贏。"""
    return conn.execute(
        """SELECT u.name, u.team, COUNT(c.id) AS hands, COALESCE(SUM(c.bet), 0) AS wagered,
                  COALESCE(SUM(CASE WHEN c.status = 'done' THEN c.payout - c.bet ELSE -c.bet END), 0) AS net
           FROM casino c JOIN users u ON u.id = c.user_id
           GROUP BY u.id ORDER BY net DESC"""
    ).fetchall()
