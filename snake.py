# -*- coding: utf-8 -*-
"""
小洄的蛇（Battlesnake）—— Python 版。

0.3.0 相对 0.2.1：食物按「实际步数」找（曼哈顿距离会被墙和身体骗），
饿了按饥饿程度重罚绕路；并给「出口在对手脚下」的空腔加一点封口惩罚。

跑起来：
    python -X utf8 snake.py --port 8095
    (然后 cloudflared tunnel --url http://127.0.0.1:8095，或用你本地的公网方案)

自测（不联网，喂一个假局面看它选哪步）：
    python -X utf8 snake.py --selftest

接口：GET / 返回蛇的信息；POST /start /move /end。
"""

import argparse
import json
import os
import sys
from collections import deque
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

VERSION = "0.4.2-py"
SEAL_HARD = 260.0   # 「出口在对手脚下」且空间已经不够自己用的硬惩罚
SEAL_W = 3.0        # 空间紧张时，每差一格的惩罚
DIRS = {"up": (0, 1), "down": (0, -1), "left": (-1, 0), "right": (1, 0)}


def key(x, y):
    return x * 100 + y


def in_bounds(x, y, w, h):
    return 0 <= x < w and 0 <= y < h


def flood_size(x, y, blocked, w, h):
    """从 (x,y) 能走到的格子数（不含自身被占的情况，调用方已保证可走）"""
    if key(x, y) in blocked:
        return 0
    seen = {key(x, y)}
    q = deque([(x, y)])
    n = 1
    while q:
        cx, cy = q.popleft()
        for dx, dy in DIRS.values():
            nx, ny = cx + dx, cy + dy
            k = key(nx, ny)
            if not in_bounds(nx, ny, w, h) or k in blocked or k in seen:
                continue
            seen.add(k)
            n += 1
            q.append((nx, ny))
    return n


def opp_seal(opp_heads, w, h):
    """对手下一步能踩到的格子 = 可能被封死的出口。
    静态 flood fill 会把这些出口当成永久通道：一个 25 格的空腔，出口却在对手
    下一步的落点上，实际可用空间会在几回合内缩到只剩脚下。"""
    z = set()
    for oh in opp_heads:
        for dx, dy in DIRS.values():
            x, y = oh[0] + dx, oh[1] + dy
            if in_bounds(x, y, w, h):
                z.add(key(x, y))
    return z


def food_dist(start, blocked, food, w, h):
    """到最近食物的实际步数（绕开身体走），没有食物则返回 99。"""
    if not food:
        return 99
    goals = {key(x, y) for x, y in food}
    if key(start[0], start[1]) in goals:
        return 0
    seen = {key(start[0], start[1])}
    q = deque([(start[0], start[1], 0)])
    while q:
        cx, cy, d = q.popleft()
        for dx, dy in DIRS.values():
            nx, ny = cx + dx, cy + dy
            k = key(nx, ny)
            if not in_bounds(nx, ny, w, h) or k in seen:
                continue
            if k in goals:
                return d + 1
            if k in blocked:
                continue
            seen.add(k)
            q.append((nx, ny, d + 1))
    return 99


def evaluate_moves(data):
    """返回 4 个方向的打分（含不可行走法），按分数降序。"""
    board = data.get("board") or {}
    you = data.get("you") or {}
    w = board.get("width") or 11
    h = board.get("height") or 11
    snakes = board.get("snakes") or []
    food = [(f["x"], f["y"]) for f in (board.get("food") or [])]
    hazards = {key(z["x"], z["y"]) for z in (board.get("hazards") or [])}

    my_body = [(p["x"], p["y"]) for p in (you.get("body") or [])]
    if not my_body:
        return []
    my_head = my_body[0]
    my_len = len(my_body)
    my_health = you.get("health", 100)

    blocked = set()
    threats = []
    for s in snakes:
        body = [(p["x"], p["y"]) for p in (s.get("body") or [])]
        if not body:
            continue
        self_ = body == my_body
        for seg in body:
            blocked.add(key(*seg))
        blocked.discard(key(*body[-1]))  # 尾巴这一步会让开（除非它吃到了，见 blocked_after）
        nb = {key(body[0][0] + dx, body[0][1] + dy) for dx, dy in DIRS.values()}
        threats.append({"set": nb, "len": len(body), "head": body[0], "self": self_})
    # 毒格不是墙（Royale）：走进去会掉血，但路是通的。把「安全可走空间」单独算，
    # 真正的墙 (=blocked) 和「会被毒缩掉的空间」(=blocked_hz) 分开评估。
    has_hz = bool(hazards)
    blocked_hz = blocked | hazards

    opp_lead = None
    for info in threats:
        if info["self"]:
            continue
        before = flood_size(info["head"][0], info["head"][1], blocked, w, h)
        if opp_lead is None or before > opp_lead["before"]:
            opp_lead = {"before": before, "info": info}

    cx, cy = (w - 1) / 2, (h - 1) / 2
    opp_heads = [i["head"] for i in threats if not i["self"]]
    seal = opp_seal(opp_heads, w, h)
    blocked_seal = blocked | seal
    out = []

    for name, (dx, dy) in DIRS.items():
        nx, ny = my_head[0] + dx, my_head[1] + dy
        row = {"move": name, "feasible": True, "score": 0.0, "space": 0, "seal": 0, "fdist": 99,
               "dfood": 99, "risky": False, "wall": 0, "choke": 0}

        if not in_bounds(nx, ny, w, h) or key(nx, ny) in blocked:
            row["feasible"] = False
            row["score"] = -1e9
            out.append(row)
            continue

        ate = (nx, ny) in food
        space = flood_size(nx, ny, blocked, w, h)
        # 有毒圈时，「空间」只算不进毒的那部分：毒圈会继续缩，站在毒里的地不属于我
        space = flood_size(nx, ny, blocked_hz, w, h) if has_hz else space
        space_seal = flood_size(nx, ny, blocked_seal, w, h)
        dfood = min((abs(nx - fx) + abs(ny - fy) for fx, fy in food), default=10)
        fdist = food_dist((nx, ny), blocked, food, w, h)

        risky = False
        for info in threats:
            if info["self"]:
                continue
            if info["len"] >= my_len and key(nx, ny) in info["set"]:
                risky = True

        # 迎头惩罚已撤（0.3.1）：原来的「离对手 2-4 格就扣分」会让蛇在食物旁边
        # 一直后退，反而饿死。真正该怕的只有「下一步撞进对手头」，那件事交给 risky。
        head_risk = 0

        # 贴边：贴墙/贴角是真陷阱
        edge = min(nx, w - 1 - nx, ny, h - 1 - ny)
        wall = 40 if edge <= 0 else (12 if edge == 1 else 0)

        # 逃生口：落点下一步还能走几格
        escape = 0
        for ex, ey in DIRS.values():
            tx, ty = nx + ex, ny + ey
            if in_bounds(tx, ty, w, h) and key(tx, ty) not in blocked and (tx, ty) != my_head:
                escape += 1

        choke = 0
        if opp_lead:
            oh = opp_lead["info"]["head"]
            b2 = set(blocked)
            b2.add(key(nx, ny))
            after = flood_size(oh[0], oh[1], b2, w, h)
            choke = max(0, opp_lead["before"] - after)

        # 两步活口：这一步落下去之后，下一回合还有没有路可走。
        # 只看当前空间会被骗——t245 那局四个方向都像 93，实际落点是个 13 格的死袋，
        # 因为「门口」就是我自己的身体，动一步就关上了。
        body_after = ([ (nx, ny) ] + my_body) if ate else ([ (nx, ny) ] + my_body[:-1])
        blocked_after = set(blocked)
        blocked_after.discard(key(*my_body[-1]))
        blocked_after.add(key(nx, ny))
        if ate:
            blocked_after.add(key(*my_body[-1]))
        best2 = 0
        for ex2, ey2 in DIRS.values():
            tx, ty = nx + ex2, ny + ey2
            if not in_bounds(tx, ty, w, h) or key(tx, ty) in blocked_after:
                continue
            cand = flood_size(tx, ty, blocked_after, w, h)
            if cand > best2:
                best2 = cand

        if key(nx, ny) in hazards and my_health <= 15:
            row["feasible"] = False
            row["score"] = -1e9
            out.append(row)
            continue

        score = space * 1.6
        if key(nx, ny) in hazards:
            # 踩毒：血多还能扛，血少就是送命
            score -= 600 if my_health <= 40 else 150
        if risky:
            score -= 900
        score += head_risk
        score += 22 if (ate and edge <= 0) else (40 if (ate and edge == 1) else (60 if ate else 0))
        # 饿的时候按「实际步数」重罚绕路——曼哈顿距离会被墙和身体骗过去
        if my_health < 60:
            hunger = 60 - my_health
            score += hunger * 6 - fdist * (6 + hunger * 0.8)
        else:
            score -= fdist * 0.6
        score -= (abs(nx - cx) + abs(ny - cy)) * 0.5
        if space < my_len:
            score -= 1e6
        elif space < my_len * 2:
            score -= (my_len * 2 - space) * 14
        # 下一回合就没路的落点，等于提前进场，重罚
        if best2 < my_len:
            score -= 3000
        elif best2 < my_len * 2:
            score -= (my_len * 2 - best2) * 10
        # 封口：出口一旦在对手下一步能踩到的位置，这块空腔随时会缩水
        # 出口就在对手下一步能踩的位置，而这块空腔还装不下我 —— 这不是扣分能表达的，
        # 直接标记成「禁止」，除非整盘棋没有别的选择（见 choose_move 的兜底）
        # space_seal == 0 表示落点本身就是「门」——那是 risky 的事，不算被封；
        # 只有 0 < space_seal < my_len 才是「进了个装不下自己的袋子」
        seal_bad = 0 < space_seal < my_len
        if seal_bad:
            score -= SEAL_HARD
        elif 0 < space_seal < my_len * 2:
            score -= (my_len * 2 - space_seal) * SEAL_W
        score -= wall
        if escape <= 1:
            score -= 45
        elif escape == 2:
            score -= 12
        score += choke * 2.5

        row.update(space=space, seal=space_seal, dfood=dfood, fdist=fdist,
                   risky=risky, wall=wall, choke=choke, score=score, seal_bad=seal_bad)
        out.append(row)

    # —— 一层半：我走完之后，对手挑最伤我的一手，会把我的余地压成什么样
    cands = sorted([r for r in out if r["feasible"]], key=lambda r: -r["score"])[:3]
    for row in cands:
        dx, dy = DIRS[row["move"]]
        nhx, nhy = my_head[0] + dx, my_head[1] + dy
        b2 = set(blocked)
        b2.discard(key(*my_body[-1]))
        b2.add(key(nhx, nhy))

        worst, worst_safe = float("inf"), 4
        for info in threats:
            if info["self"]:
                continue
            oh = info["head"]
            for ox, oy in DIRS.values():
                tx, ty = oh[0] + ox, oh[1] + oy
                if not in_bounds(tx, ty, w, h) or key(tx, ty) in b2:
                    continue
                b3 = set(b2)
                b3.add(key(tx, ty))
                mine = sum(1 for ex, ey in DIRS.values()
                           if in_bounds(nhx + ex, nhy + ey, w, h)
                           and key(nhx + ex, nhy + ey) not in b3)
                sp = flood_size(nhx, nhy, b3, w, h)
                v = min(mine * 45, 200) + sp * 1.2
                if v < worst:
                    worst, worst_safe = v, mine
        if worst != float("inf"):
            row["look"], row["look_safe"] = worst, worst_safe
            row["score"] = row["score"] * 0.4 + worst

        # 围猎：只有「我比它长」才值得压；对着更长的蛇去压等于把自己送进死角
        opp_best = 4
        for info in threats:
            if info["self"]:
                continue
            oh = info["head"]
            n = sum(1 for ox, oy in DIRS.values()
                    if in_bounds(oh[0] + ox, oh[1] + oy, w, h)
                    and key(oh[0] + ox, oh[1] + oy) not in b2)
            opp_best = min(opp_best, n)
        row["opp_safe"] = opp_best
        can_hunt = any((not i["self"]) and my_len > i["len"] for i in threats)
        if can_hunt:
            row["score"] += {0: 1500, 1: 110, 2: 45, 3: 10}.get(opp_best, 0)

    def center_dist(r):
        dx, dy = DIRS[r["move"]]
        return abs(my_head[0] + dx - cx) + abs(my_head[1] + dy - cy)

    out.sort(key=lambda r: (not r["feasible"], -r["score"], center_dist(r), -r["choke"]))
    return out


def choose_move(data):
    pool = evaluate_moves(data)
    ok = [m for m in pool if m["feasible"]]
    if not ok:
        return "up"
    # 「出口在对手脚下」的选项垫底：只有在没有别的活路时才用它
    safe = [m for m in ok if not m.get("seal_bad")]
    return (safe or ok)[0]["move"]


INFO = {
    "apiversion": "1", "author": "caomei", "color": "#7C9CF5",
    "head": "default", "tail": "default", "version": VERSION,
}


class Handler(BaseHTTPRequestHandler):
    # HTTP/1.1 + Content-Length 才能保持长连接，省掉每次握手的往返
    protocol_version = "HTTP/1.1"

    def _send(self, obj, code=200):
        body = json.dumps(obj).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(body)

    def do_OPTIONS(self):
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Headers", "*")
        self.end_headers()

    def do_GET(self):
        self._send(INFO)

    def do_POST(self):
        n = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(n) if n else b"{}"
        try:
            data = json.loads(raw or b"{}")
        except Exception:
            data = {}
        if self.path.rstrip("/").endswith("/move"):
            mv = choose_move(data)
            print(f"[move] turn={data.get('turn')} -> {mv}", flush=True)
            self._send({"move": mv, "shout": ""})
        else:
            self._send({})

    def log_message(self, *a):
        pass


def selftest():
    """开局局面：两条蛇分别在左下/右上，看它选什么。"""
    data = {
        "turn": 0,
        "board": {
            "width": 11, "height": 11,
            "food": [{"x": 5, "y": 5}],
            "hazards": [],
            "snakes": [
                {"name": "xiaohui", "health": 100,
                 "body": [{"x": 1, "y": 1}, {"x": 1, "y": 0}, {"x": 0, "y": 0}]},
                {"name": "soft", "health": 100,
                 "body": [{"x": 9, "y": 9}, {"x": 9, "y": 10}, {"x": 10, "y": 10}]},
            ],
        },
        "you": {"name": "xiaohui", "health": 100,
                "body": [{"x": 1, "y": 1}, {"x": 1, "y": 0}, {"x": 0, "y": 0}]},
    }
    for r in evaluate_moves(data):
        print("%-6s 空间%4d 封口%4d 食步%3d 分 %8.1f%s" % (
            r["move"], r["space"], r.get("seal", 0), r.get("fdist", 99), r["score"],
            "" if r["feasible"] else "  (不可走)"))
    print("选择：", choose_move(data))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=int(os.environ.get("PORT", "8095")))
    ap.add_argument("--selftest", action="store_true")
    a = ap.parse_args()
    if a.selftest:
        selftest()
        return
    srv = ThreadingHTTPServer(("0.0.0.0", a.port), Handler)
    print(f"小洄的蛇 {VERSION} 在 http://0.0.0.0:{a.port}", flush=True)
    srv.serve_forever()


if __name__ == "__main__":
    main()
