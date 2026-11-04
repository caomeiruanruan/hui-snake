# -*- coding: utf-8 -*-
"""
小洄的蛇（Battlesnake）—— Python 版，逻辑与 Cloudflare Worker 版 0.2.1 对齐。

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

VERSION = "0.2.1-py"
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
        blocked.discard(key(*body[-1]))  # 尾巴下一步会让开
        nb = {key(body[0][0] + dx, body[0][1] + dy) for dx, dy in DIRS.values()}
        threats.append({"set": nb, "len": len(body), "head": body[0], "self": self_})
    blocked |= hazards

    opp_lead = None
    for info in threats:
        if info["self"]:
            continue
        before = flood_size(info["head"][0], info["head"][1], blocked, w, h)
        if opp_lead is None or before > opp_lead["before"]:
            opp_lead = {"before": before, "info": info}

    cx, cy = (w - 1) / 2, (h - 1) / 2
    out = []

    for name, (dx, dy) in DIRS.items():
        nx, ny = my_head[0] + dx, my_head[1] + dy
        row = {"move": name, "feasible": True, "score": 0.0, "space": 0,
               "dfood": 99, "risky": False, "wall": 0, "choke": 0}

        if not in_bounds(nx, ny, w, h) or key(nx, ny) in blocked:
            row["feasible"] = False
            row["score"] = -1e9
            out.append(row)
            continue

        ate = (nx, ny) in food
        space = flood_size(nx, ny, blocked, w, h)
        dfood = min((abs(nx - fx) + abs(ny - fy) for fx, fy in food), default=10)

        risky = False
        for info in threats:
            if info["self"]:
                continue
            if info["len"] >= my_len and key(nx, ny) in info["set"]:
                risky = True

        # 迎头危险区：按「离对手头几步」给梯度惩罚
        head_risk = 0
        for info in threats:
            if info["self"]:
                continue
            d = abs(nx - info["head"][0]) + abs(ny - info["head"][1])
            if d <= 1:
                head_risk += 40 if my_len > info["len"] else -1200
            elif d <= 4 and info["len"] >= my_len:
                head_risk -= {2: 260, 3: 80, 4: 20}[d]

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

        score = space * 1.6
        if risky:
            score -= 900
        score += head_risk
        score += 22 if (ate and edge <= 0) else (40 if (ate and edge == 1) else (60 if ate else 0))
        if my_health < 45:
            score += 90 - dfood * 6
        else:
            score -= dfood * 0.6
        score -= (abs(nx - cx) + abs(ny - cy)) * 0.5
        if space < my_len:
            score -= 1e6
        elif space < my_len * 2:
            score -= (my_len * 2 - space) * 14
        score -= wall
        if escape <= 1:
            score -= 45
        elif escape == 2:
            score -= 12
        score += choke * 2.5

        row.update(space=space, dfood=dfood, risky=risky, wall=wall, choke=choke, score=score)
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
    for m in pool:
        if m["feasible"]:
            return m["move"]
    return "up"


INFO = {
    "apiversion": "1", "author": "caomei", "color": "#7C9CF5",
    "head": "default", "tail": "default", "version": VERSION,
}


class Handler(BaseHTTPRequestHandler):
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
        print("%-6s 空间%4d 食距%3d 分 %8.1f%s" % (
            r["move"], r["space"], r["dfood"], r["score"], "" if r["feasible"] else "  (不可走)"))
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
