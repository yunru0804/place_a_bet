"""把 AI 生成的角色動畫圖轉成網站用的格式：static/chars/<名字>.png。

用法：python3 tools/import_sheet.py 柯怡如

會在 picture/ 找這些檔（png / jpg / webp 都可以）：
  A. 整張：<名字>_sheet.png —— 3 列（拔河、跳繩、開香檳）、4 列（前面加待機）或 6 列（再加勝利、失敗）
  B. 分開：<名字>_tug.png、<名字>_jump.png、<名字>_shake.png，可再加 _idle、_win、_lose
     （每張一列 8 格；AI 常常一次畫不好 24 格，分開生成比較穩）
  C. 每格一張：picture/<名字>_frames/ 資料夾，檔名 <動作>_<1-8>.png
     動作名稱：tug_of_war / tug、jump_rope / jump、champagne / shake，
     可省略：idle（待機）、win（勝利）、lose（失敗）、idle_front（正面待機）、win_front（正面勝利）、
     think_front（思考）、tap_front（點擊）、talk_front（說話）、sing_front（唱歌）
     這種通常已經是透明背景、每張畫布一樣大，會保留角色在畫布裡的位置（不重新置中），
     如果是放大過的像素畫（例如每 6×6 一格），會自動還原成原生像素，保持銳利。
優先順序：C → B → A。

做的事：
  1. 去背景：從圖片四邊取背景色（深藍、白都行），把連到邊緣、顏色接近的部分變透明；
     已經是透明背景的圖會跳過這步
  2. 切格：用空白間隔找出每一列、每一格；找不到剛好 8 格就平均切 8 等份
  3. 對齊：全部用同一個縮放比例（角色大小一致），每格水平置中、底部對齊，
     同一列共用上下範圍，所以跳起來的高度會保留
  4. 輸出一列 8 格、12 列：待機、拔河、跳繩、開香檳、勝利、失敗、正面待機、正面勝利、思考、點擊、說話、唱歌
沒有待機：用開香檳第 1 格輕輕上下呼吸代替；沒有勝利／失敗：用待機代替；沒有正面：用側面代替。
"""

import sys
from pathlib import Path

import numpy as np
from PIL import Image

ROOT = Path(__file__).resolve().parent.parent
SRC = ROOT / "picture"
OUT = ROOT / "static" / "chars"
CW, CH, COLS = 96, 116, 8
ROW_KEYS = ["idle", "tug", "jump", "shake", "win", "lose", "idle_front", "win_front",
            "think_front", "tap_front", "talk_front", "sing_front"]  # 網站依這個順序取列（app.py 的 SPRITE_ROWS）
EXTS = (".png", ".webp", ".jpg", ".jpeg")


def find(name: str, part: str) -> Path | None:
    for ext in EXTS:
        p = SRC / f"{name}_{part}{ext}"
        if p.exists():
            return p
    return None


# ---------------- 去背景 ----------------

def remove_bg(img: Image.Image, tol=48, tight=20) -> np.ndarray:
    # 深藍背景跟黑頭髮距離只有 ~75，門檻不能開太大
    a = np.array(img.convert("RGBA")).astype(np.int32)
    alpha = a[..., 3]
    border = np.concatenate([alpha[0], alpha[-1], alpha[:, 0], alpha[:, -1]])
    if (border < 20).mean() > 0.6:            # 本來就是透明背景
        return a.astype(np.uint8)
    rgb = a[..., :3]
    edge = np.concatenate([rgb[0], rgb[-1], rgb[:, 0], rgb[:, -1]])
    bg = np.median(edge, axis=0)
    dist = np.sqrt(((rgb - bg) ** 2).sum(-1))
    cand = dist < tol
    mask = np.zeros_like(cand)
    mask[0], mask[-1], mask[:, 0], mask[:, -1] = cand[0], cand[-1], cand[:, 0], cand[:, -1]
    while True:                                # 從邊緣往內長，只吃顏色接近背景的
        grow = mask.copy()
        grow[1:] |= mask[:-1]
        grow[:-1] |= mask[1:]
        grow[:, 1:] |= mask[:, :-1]
        grow[:, :-1] |= mask[:, 1:]
        grow &= cand
        if (grow == mask).all():
            break
        mask = grow
    if bg.mean() < 200:                        # 被包住的縫隙（手臂和身體中間）也挖掉；
        mask |= dist < tight                   # 白底不做，不然眼白、牙齒會被挖空
    a[..., 3] = np.where(mask, 0, alpha)
    # 只有緊貼背景的最外圈、顏色又偏背景的像素才半透明化，避免一圈藍邊
    ring = np.zeros_like(mask)
    ring[1:] |= mask[:-1]
    ring[:-1] |= mask[1:]
    ring[:, 1:] |= mask[:, :-1]
    ring[:, :-1] |= mask[:, 1:]
    halo = ring & (~mask) & (dist < tol + 15)
    a[..., 3] = np.where(halo, np.minimum(a[..., 3], 140), a[..., 3])
    return a.astype(np.uint8)


# ---------------- 切格 ----------------

def bands(proj: np.ndarray, min_gap: int) -> list[list[int]]:
    """把投影中「有東西」的區段找出來；間隔小於 min_gap 的合併。"""
    on = proj > 0
    out, start = [], None
    for i, v in enumerate(on):
        if v and start is None:
            start = i
        elif not v and start is not None:
            out.append([start, i])
            start = None
    if start is not None:
        out.append([start, len(on)])
    merged = []
    for b in out:
        if merged and b[0] - merged[-1][1] < min_gap:
            merged[-1][1] = b[1]
        else:
            merged.append(b)
    return merged


def absorb_small(bs: list[list[int]], ratio: float) -> list[list[int]]:
    """太小的區段（星星、汗滴、泡沫）併進最近的鄰居。"""
    bs = [b[:] for b in bs]
    while len(bs) > 1:
        sizes = [b[1] - b[0] for b in bs]
        i = int(np.argmin(sizes))
        if sizes[i] >= max(sizes) * ratio:
            break
        if i == 0:
            j = 1
        elif i == len(bs) - 1:
            j = i - 1
        else:
            j = i - 1 if bs[i][0] - bs[i - 1][1] <= bs[i + 1][0] - bs[i][1] else i + 1
        lo, hi = min(i, j), max(i, j)
        bs[lo] = [min(bs[lo][0], bs[hi][0]), max(bs[lo][1], bs[hi][1])]
        del bs[hi]
    return bs


def split(proj: np.ndarray, n: int, length: int) -> list[list[int]]:
    thr = max(1, proj.max() * 0.01)
    bs = absorb_small(bands(np.where(proj > thr, proj, 0), max(2, length // 200)), 0.35)
    if len(bs) == n:
        return bs
    step = length / n                          # 找不到剛好 n 段就平均切
    return [[round(i * step), round((i + 1) * step)] for i in range(n)]


def cells_of_row(a: np.ndarray) -> tuple[list[np.ndarray], int]:
    """一列 → 8 格（同一列共用上下範圍）。回傳裁好的格子和這列的高度。"""
    alpha = a[..., 3] > 40
    ys = np.where(alpha.any(1))[0]
    if len(ys) == 0:
        raise SystemExit("有一列是空的，去背景可能把角色也挖掉了。")
    a = a[ys[0]:ys[-1] + 1]
    cols = split(alpha[ys[0]:ys[-1] + 1].sum(0), COLS, a.shape[1])
    return [a[:, x0:x1] for x0, x1 in cols], a.shape[0]


# ---------------- 組合 ----------------

def to_cell(cell: np.ndarray, scale: float) -> Image.Image:
    im = Image.fromarray(cell, "RGBA")
    xs = np.where((cell[..., 3] > 40).any(0))[0]
    if len(xs):                                 # 用角色本身左右置中，不是用格子
        im = im.crop((int(xs[0]), 0, int(xs[-1]) + 1, im.height))
    w, h = max(1, round(im.width * scale)), max(1, round(im.height * scale))
    im = im.convert("RGBa").resize((w, h), Image.LANCZOS).convert("RGBA")
    px = np.array(im)
    px[..., 3] = np.where(px[..., 3] < 50, 0, px[..., 3])
    im = Image.fromarray(px, "RGBA")
    out = Image.new("RGBA", (CW, CH), (0, 0, 0, 0))
    out.alpha_composite(im, ((CW - w) // 2, CH - h - 2)) if w <= CW else \
        out.alpha_composite(im.crop(((w - CW) // 2, 0, (w - CW) // 2 + CW, h)), (0, CH - h - 2))
    return out


def load_rows(name: str) -> dict[str, list[np.ndarray]]:
    rows: dict[str, tuple[list[np.ndarray], int]] = {}
    parts = {k: find(name, k) for k in ROW_KEYS}
    if parts["tug"] or parts["jump"] or parts["shake"]:
        for k, p in parts.items():
            if p:
                rows[k] = cells_of_row(remove_bg(Image.open(p)))
                print(f"  {p.name} → {k}")
    else:
        p = find(name, "sheet")
        if not p:
            raise SystemExit(f"picture/ 裡找不到 {name}_sheet.png 或 {name}_tug/_jump/_shake.png")
        a = remove_bg(Image.open(p))
        alpha = a[..., 3] > 40
        prow = alpha.sum(1)
        bs = absorb_small(bands(np.where(prow > max(1, prow.max() * 0.01), prow, 0),
                                max(2, a.shape[0] // 100)), 0.35)
        n = len(bs) if len(bs) in (3, 4, 6) else 3
        if len(bs) != n:
            bs = split(prow, n, a.shape[0])
        keys = {3: ROW_KEYS[1:4], 4: ROW_KEYS[:4], 6: ROW_KEYS[:6]}[n]
        for k, (y0, y1) in zip(keys, bs):
            rows[k] = cells_of_row(a[y0:y1])
        print(f"  {p.name} → {n} 列：{'、'.join(keys)}")
    return rows


# ---------------- C. 每格一張的資料夾 ----------------

FRAME_ALIASES = {"tug_of_war": "tug", "tug": "tug", "jump_rope": "jump", "jump": "jump",
                 "champagne": "shake", "shake": "shake", "idle": "idle", "win": "win", "lose": "lose",
                 "idle_front": "idle_front", "win_front": "win_front",
                 "think_front": "think_front", "tap_front": "tap_front", "talk_front": "talk_front",
                 "sing_front": "sing_front"}


def pixel_block(arrs: list[np.ndarray]) -> tuple[int, int, int]:
    """找出放大倍數和格線起點：顏色變化的位置都會落在同一個倍數上。"""
    xs, ys = [], []
    for a in arrs[:6]:
        diff_x = (a[:, 1:] != a[:, :-1]).any(-1)
        diff_y = (a[1:] != a[:-1]).any(-1)
        xs += list(np.where(diff_x.any(0))[0] + 1)
        ys += list(np.where(diff_y.any(1))[0] + 1)
    for b in range(16, 0, -1):
        ox = np.bincount(np.array(xs) % b).argmax()
        oy = np.bincount(np.array(ys) % b).argmax()
        hit = np.mean([(v - ox) % b == 0 for v in xs] + [(v - oy) % b == 0 for v in ys])
        if hit > 0.97:
            return b, int(ox), int(oy)
    return 1, 0, 0


def strip_rope(jump: list[np.ndarray], others: list[np.ndarray]) -> list[np.ndarray]:
    """把跳繩圖裡畫的繩子拿掉（網站的跳繩畫面自己會畫一條會轉的繩子）。
    繩子 = 只出現在跳繩圖、而且是淺色的顏色；再清掉繩子旁邊沒貼著身體的黑描邊和零碎小塊。"""
    def colors(arrs):
        out = set()
        for a in arrs:
            px = a[a[..., 3] > 0][:, :3]
            out |= set(map(tuple, px.tolist()))
        return out
    other = colors(others)
    rope = {c for c in colors(jump) - other if sum(c) / 3 > 180}
    # 新版跳繩圖本來就沒畫繩子：淺色像素很少就當作沒有繩子，只做置中（不然會誤刪灰塵、反光）
    total = sum(int((a[..., 3] > 0).sum()) for a in jump)
    code = lambda px: (px[..., 0].astype(np.int32) << 16) | (px[..., 1].astype(np.int32) << 8) | px[..., 2]
    rope_codes = np.array([(r << 16) | (g << 8) | bl for r, g, bl in rope], dtype=np.int32)
    rope_px = sum(int((np.isin(code(a[..., :3]), rope_codes) & (a[..., 3] > 0)).sum()) for a in jump)
    has_rope = rope_px > total * 0.03
    out = []
    for a in jump:
        a = a.copy()
        h, w = a.shape[:2]
        if not has_rope:
            out.append(center_body(a))
            continue
        rgb = a[..., :3].tolist()
        gone = np.zeros((h, w), bool)
        for y in range(h):
            for x in range(w):
                if a[y, x, 3] and tuple(rgb[y][x]) in rope:
                    gone[y, x] = True
        a[gone, 3] = 0
        ink = (a[..., :3].max(-1) < 50) & (a[..., 3] > 0)
        body = (a[..., 3] > 0) & ~ink
        for _ in range(3):                    # 繩子的描邊：貼著被拿掉的繩子、四周沒有身體的顏色
            near = np.zeros_like(gone)
            for dy in (-1, 0, 1):
                for dx in (-1, 0, 1):
                    near |= np.roll(np.roll(gone, dy, 0), dx, 1)
            touch = np.zeros_like(body)
            for dy, dx in ((1, 0), (-1, 0), (0, 1), (0, -1)):
                touch |= np.roll(np.roll(body, dy, 0), dx, 1)
            kill = ink & near & ~touch & (a[..., 3] > 0)
            if not kill.any():
                break
            a[kill, 3] = 0
            gone |= kill
        # 零碎小塊（沒連到身上的線頭）
        seen = np.zeros((h, w), bool)
        for y in range(h):
            for x in range(w):
                if a[y, x, 3] == 0 or seen[y, x]:
                    continue
                comp, stack = [], [(y, x)]
                seen[y, x] = True
                while stack:
                    cy, cx = stack.pop()
                    comp.append((cy, cx))
                    for ny, nx in ((cy + 1, cx), (cy - 1, cx), (cy, cx + 1), (cy, cx - 1)):
                        if 0 <= ny < h and 0 <= nx < w and not seen[ny, nx] and a[ny, nx, 3]:
                            seen[ny, nx] = True
                            stack.append((ny, nx))
                # 黑色線頭、或很小的灰塵／星星點點都清掉（停格時腳邊才不會一直有灰塵）
                if (len(comp) < 30 and all(a[p][:3].max() < 50 for p in comp)) or len(comp) < 6:
                    for p in comp:
                        a[p][3] = 0
        out.append(center_body(a))
    print("  跳繩：" + (f"拿掉畫在圖上的繩子（{len(rope)} 種顏色）、" if has_rope else "圖上沒有繩子，") + "每格置中")
    return out


def center_body(a: np.ndarray) -> np.ndarray:
    """每格以身體為中心左右對齊：原圖角色在畫布裡會左右飄，跳起來會撞到網站畫的繩子。"""
    w = a.shape[1]
    body = (a[..., 3] > 0) & (a[..., :3].max(-1) >= 50)
    xs = np.where(body)[1]
    if len(xs):
        shift = w // 2 - int(round(np.median(xs)))
        a = np.roll(a, shift, axis=1)
        if shift > 0:
            a[:, :shift, 3] = 0
        elif shift < 0:
            a[:, shift:, 3] = 0
    return a


def build_from_frames(name: str, folder: Path) -> Path:
    import re
    rows: dict[str, list[tuple[int, Path]]] = {}
    for f in folder.iterdir():
        m = re.fullmatch(r"(.+?)_(\d+)\.(png|webp)", f.name, re.I)
        if m and m.group(1).lower() in FRAME_ALIASES:
            rows.setdefault(FRAME_ALIASES[m.group(1).lower()], []).append((int(m.group(2)), f))
    missing = [k for k in ("tug", "jump", "shake") if k not in rows]
    if missing:
        raise SystemExit("缺少動作：" + "、".join(missing))
    frames = {k: [np.array(Image.open(f).convert("RGBA")) for _, f in sorted(v)][:COLS]
              for k, v in rows.items()}
    for k, v in frames.items():
        if len(v) < COLS:
            raise SystemExit(f"{k} 只有 {len(v)} 格，需要 {COLS} 格。")
        print(f"  {k}: {len(v)} 格")
    all_frames = [a for v in frames.values() for a in v]
    if len({a.shape for a in all_frames}) != 1:
        raise SystemExit("每張圖的大小要一樣。")
    # 不透明背景的話照樣去背景
    if (all_frames[0][0, 0, 3] > 20):
        frames = {k: [remove_bg(Image.fromarray(a)) for a in v] for k, v in frames.items()}
        all_frames = [a for v in frames.values() for a in v]
    b, ox, oy = pixel_block(all_frames)
    def native(a):
        a = a[oy:, ox:]
        h, w = a.shape[0] // b, a.shape[1] // b
        return a[b // 2: h * b: b, b // 2: w * b: b]       # 取每個大格子的中心點
    frames = {k: [native(a) for a in v] for k, v in frames.items()}
    print(f"  原圖放大 {b} 倍 → 原生每格 {frames['tug'][0].shape[1]}×{frames['tug'][0].shape[0]}")
    frames["jump"] = strip_rope(frames["jump"], [a for k, v in frames.items() if k != "jump" for a in v])
    # 只裁掉所有格子共同的上下空白，保留角色在畫布裡的相對位置（跳起來的高度不會不見）
    union = np.zeros(frames["tug"][0].shape[:2], bool)
    for v in frames.values():
        for a in v:
            union |= a[..., 3] > 20
    ys = np.where(union.any(1))[0]
    xs = np.where(union.any(0))[0]
    y0, y1 = max(0, ys[0] - 1), min(union.shape[0], ys[-1] + 2)
    x0, x1 = max(0, xs[0] - 1), min(union.shape[1], xs[-1] + 2)
    fw, fh = x1 - x0, y1 - y0
    imgs = {k: [Image.fromarray(a[y0:y1, x0:x1].copy(), "RGBA") for a in v] for k, v in frames.items()}
    print(f"  輸出每格 {fw}×{fh}")
    return save_sheet(name, imgs, fw, fh, (0, 0, 0, -1, -1, -1, 0, 0))


def save_sheet(name: str, rows: dict[str, list[Image.Image]], fw: int, fh: int, breathe) -> Path:
    """組成 8 欄 × 6 列；缺的列用替代動作補上。"""
    if "idle" not in rows:                     # 沒有待機：開香檳第 1 格上下呼吸
        base = rows["shake"][0]
        rows["idle"] = []
        for dy in breathe:
            f = Image.new("RGBA", (fw, fh), (0, 0, 0, 0))
            f.paste(base, (0, dy))
            rows["idle"].append(f)
    for k, alt in (("win", "idle"), ("lose", "idle"), ("idle_front", "idle"), ("win_front", "win"),
                   ("think_front", "idle_front"), ("tap_front", "idle_front"), ("talk_front", "idle_front"),
                   ("sing_front", "talk_front")):
        if k not in rows:
            rows[k] = rows[alt]
            print(f"  沒有 {k}，用 {alt} 代替")
    sheet = Image.new("RGBA", (fw * COLS, fh * len(ROW_KEYS)), (0, 0, 0, 0))
    for r, k in enumerate(ROW_KEYS):
        for i, im in enumerate(rows[k][:COLS]):
            sheet.alpha_composite(im, (i * fw, r * fh))
    OUT.mkdir(parents=True, exist_ok=True)
    path = OUT / f"{name}.png"
    sheet.save(path, optimize=True)
    return path


def build(name: str) -> Path:
    folder = SRC / f"{name}_frames"
    if folder.is_dir():
        return build_from_frames(name, folder)
    rows = load_rows(name)
    missing = [k for k in ("tug", "jump", "shake") if k not in rows]
    if missing:
        raise SystemExit("缺少動作：" + "、".join(missing))
    tallest = max(h for _, h in rows.values())
    scale = (CH - 4) / tallest                  # 全部同一個比例：最高的那列剛好塞滿
    imgs = {k: [to_cell(c, scale) for c in cells] for k, (cells, _) in rows.items()}
    return save_sheet(name, imgs, CW, CH, (0, 0, -1, -2, -2, -1, 0, 0))


if __name__ == "__main__":
    if len(sys.argv) < 2:
        sys.exit("用法：python3 tools/import_sheet.py <名字>")
    for n in sys.argv[1:]:
        print(n)
        print("✓", build(n).relative_to(ROOT))
