# 小洄的蛇（Python 版）

Battlesnake 协议的蛇，纯标准库、零依赖，逻辑与 Cloudflare Worker 版 0.2.1 对齐。
可以本地跑，也可以直接丢到 Render（仓库里带 `render.yaml`，Blueprint 一键起）。

- 端口：读环境变量 `PORT`，没有就用 8095（Render / Heroku 都会注入 `PORT`）
- 接口：`GET /` 返回蛇的信息；`POST /start`、`POST /move`、`POST /end`
- 决策：`choose_move(data)`，只吃 Battlesnake 的标准 payload

## 本地跑

```bash
python -X utf8 snake.py --port 8095
curl http://127.0.0.1:8095/          # {"apiversion":"1","author":"caomei",...}
python -X utf8 snake.py --selftest   # 喂一个假局面，看四个方向各打多少分
```

## Render 部署

1. Render → New → **Blueprint**，选这个仓库，它会读 `render.yaml` 自动建服务；
   或者 New → **Web Service** 手动填：
   - Runtime: Python
   - Build Command: `pip install -r requirements.txt`
   - Start Command: `python snake.py`
   - Health Check Path: `/`
   - Plan: Free
2. 等构建完成，地址形如 `https://xiaohui-snake.onrender.com`。
3. 验证：`curl https://xiaohui-snake.onrender.com/` 能拿到 `apiversion` 就通了。
4. 去 Battlesnake 后台把它的 URL 填成 `https://xiaohui-snake.onrender.com`。

免费实例**闲置 15 分钟会休眠**，下一局开局要等它冷启动（通常 30-50 秒），
而 Battlesnake 的 `/move` 只给 500ms，所以正式比赛前一定要先打一局把它叫醒。

## 本地对战台

配套的本地对战台在 `snake/duel/`（另一个仓库/目录），可以打：

```bash
python -m duel.run --a py:..\core.py --b http:https://xiaohui-snake.onrender.com --games 100
```

它支持 `py:`（直接 import 本地文件）、`http:`（线上实例）、`replay:`（官方回放逐帧重放）三种对手。
