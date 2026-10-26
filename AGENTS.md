# AGENTS.md

给接手这个项目的 AI 编程助手看的说明。面向用户的说明在 [README.md](README.md)。

## 项目是什么

塞西莉亚的游戏库：Windows 上的本地游戏管理软件，由两个独立进程组成。

| 进程 | 入口 | 作用 |
| --- | --- | --- |
| 自动解压 | `auto_extract.py`（`启动自动解压.bat`） | 常驻监视 `games` 文件夹，等文件下完后解压、试密码、整理文件夹、删压缩包 |
| 游戏库 | `game_manager.py`（`打开游戏库.bat`，用 `pythonw` 启动） | FastAPI 本机小网站 + pywebview 窗口（WebView2），扫描入库、找资料、启动计时、手动导入 |

两个进程不直接通信，只共用 `config.json`（设置）和文件系统。各自每轮重新读 `config.json`，所以在游戏库设置页改的东西，自动解压下一轮就会用上。

## 环境

- Windows 10 / 11，Python 3.11，7-Zip 装在 `C:\Program Files\7-Zip\`
- 依赖：`pip install cryptography fastapi uvicorn psutil pywin32 Pillow pywebview certifi`
- 没有构建步骤，没有打包，没有自动化测试，也没有 lint 配置

## 文件分工

| 文件 | 内容 |
| --- | --- |
| `auto_extract.py` | 解压引擎和基础工具：`load_config` / `save_config`、`DEFAULT_CONFIG`、`ROOT`、分卷识别（`archive_group`）、试密码（`choose_password`，一次把所有候选都试完）、GBK 密码 zip 自解密、套娃解压、安卓版分流、`scan_once` / `watch_loop` |
| `library.py` | SQLite 数据库（`data/library.db`，表 `games` 和 `sessions`）、引擎识别、主程序挑选、扫描入库、图标提取、低完整性标签修复、回收站删除 |
| `scraper.py` | 找资料：游戏文件本地读取（Ren'Py rpa、Godot pck、RPG Maker、Unity）+ Steam / VNDB / DLsite / itch.io；`Worker` 是后台排队线程 |
| `importer.py` | 手动导入：识别拖进来的路径，挪 / 复制 / 原地登记 / 解压；`Importer` 是后台排队线程，复用 `auto_extract.process_group` |
| `game_manager.py` | FastAPI 路由（`/api/*`）、`PlayTracker` 计时、`Shell` 窗口生命周期、单实例、皮肤的标题栏颜色 |
| `window_frame.py` | 自绘标题栏：Win32 子类化去掉系统标题栏，保留系统边框 |
| `web/index.html` | 页面骨架，含占位符 `__TOKEN__` `__THEME__` `__FRAME__` `__VERSION__`，由 `game_manager.index()` 替换 |
| `web/app.js` | 全部前端逻辑，原生 JS，无框架、无打包。路由用 hash：`#/`、`#/game/<id>`、`#/settings` |
| `web/style.css` | 默认皮肤「经典深色」（`steam`），颜色都是 `:root` 上的 CSS 变量 |
| `web/theme-paper.css` | 皮肤「浪子的真情」（`paper`），全部写在 `[data-theme="paper"]` 下，覆盖变量和少量组件样式 |
| `web/icon.ico` | 软件图标：窗口、任务栏、favicon、经典深色皮肤左上角 |
| `docs/` | README 用的图片 |

模块依赖方向：`auto_extract` ← `library` ← `scraper` / `importer` ← `game_manager`。`auto_extract.py` 不能反过来 import 其他模块，它要能单独运行。

运行时生成、不进版本库：`games/`、`android/`、`data/`（数据库、封面、解压状态、窗口状态、WebView2 数据、中转文件夹）、`logs/`（`extract.log`、`game_manager.log`）。

## 运行和调试

```bash
python game_manager.py --browser
```

用默认浏览器打开游戏库，日志直接打在命令行；不开窗口，自绘标题栏按钮不显示。正常用 `pythonw` 启动时日志在 `logs/game_manager.log`。

```bash
python auto_extract.py --once
```

扫一遍就退出。`--file <路径>` 只处理一个文件，`--dir <路径>` 临时换监视目录。

游戏库是单实例：端口 8765 上已经有游戏库时，新进程只调 `/show` 把旧窗口叫到前台就退出。改了 Python 代码要先完全关掉旧的游戏库进程（窗口藏起来时进程还在），否则看到的还是旧版。

## 验证改动

没有测试套件，按改动范围自己验：

- **接口**：用 `fastapi.testclient.TestClient(game_manager.app)`，请求要带 `Host: 127.0.0.1` 和 `X-Token: game_manager.TOKEN` 头。
- **页面**：`--browser` 模式开起来，用 Playwright 之类的工具截图，两套皮肤都要看（设置页切换，或改 `config.json` 的 `theme`）。
- **解压**：用 7-Zip 现做带密码的测试压缩包。
- **隔离**：`ROOT` 取的是脚本所在文件夹，把项目复制到 `tmp/` 下的沙盒里跑，就有独立的 `config.json`、`games/`、`data/`，不会碰用户的真实数据和密码。
- 临时文件放项目里的 `tmp/`（已在 `.gitignore`），用完删掉。

## 改代码时要注意

- **接口鉴权**：中间件 `guard` 只放行 Host 是 `127.0.0.1` / `localhost` 的请求；`/api/*` 还要带 `X-Token`，口令每次启动随机生成，写进页面的 `<meta name="token">`。新接口放在 `/api/` 下就自动受保护。
- **页面缓存**：`/` 和 `/web/*` 都返回 `Cache-Control: no-cache`，静态文件地址带 `?v=<web 文件最新修改时间>`。新加的 CSS / JS 文件也要在 `index.html` 里带 `?v=__VERSION__`。
- **WebView2 线程**：碰 `window.native` 下的 WebView2 对象必须走 `Shell.on_ui_thread`，在别的线程直接读写会让两边卡死。
- **拖放导入**：完整文件路径只有 Python 这边拿得到（`Shell.on_drop` 里的 `pywebviewFullPath`），再通过 `window.importDropped(...)` 交给页面。
- **改设置**：`save_config(updates)` 只合并给出的键。密码列表的增删接口（`/api/passwords`、`/api/passwords/remove`）每次在当下的文件上加减一个，不整份覆盖，因为导入线程也会往里写密码。
- **两边同时解压**：导入用 `data/import` 当中转文件夹（`importer.py` 里改了 `auto_extract.WORK_DIR`），自动解压用 `data/`；导入时在 `games` 里用的中转文件夹叫 `_正在导入`，它在 `SKIP_DIRS` 里，游戏库扫描和自动解压都跳过；自动解压还会跳过所有以 `_` 开头的文件夹。
- **数据库迁移**：新字段加进 `library.EXTRA_COLUMNS`，`init_db` 启动时会给老数据库自动补列。路径在库里存相对 `ROOT` 的形式（`to_rel` / `to_abs`）。
- **手动改过的不覆盖**：`exe_locked=1` 的主程序扫描不改；文件名带 `-custom` 的封面、背景是手动换的，找资料时不覆盖。
- **皮肤**：新组件先在 `style.css` 用变量写，浪子的真情皮肤需要不同样子时在 `theme-paper.css` 的 `[data-theme="paper"]` 下覆盖。加皮肤还要在 `game_manager.THEMES` 里配标题栏和底色。
- **资料来源**：DLsite 和 itch.io 用的不是公开接口，抓取失败先怀疑网站改版。只读游戏文件里的数据，不运行游戏里的任何东西（`scraper._BasicTypesOnly` 限制了 pickle 能还原的类型）。
- **界面文字**：全部中文，口语、简短，面向不懂电脑的用户。错误信息用 `HTTPException(状态码, "中文说明")`，前端会直接弹出来。

## 规矩

- **`config.json` 绝不能提交**。里面有用户的解压密码，已在 `.gitignore`。提交时逐个 `git add` 文件，不要用 `git add -A` 或 `git add .`。
- **用户没有自测通过、没有明确说提交，不要 commit，更不要 push。**
- 示例、注释、文档里的游戏名、作品编号、网站名都用中性的虚构例子（比如 `SummerMemories`），不写真实作品。
- 注释和文档只描述代码现在的样子，不写修改历史、不写开发阶段的字眼。
- 代码注释用中文，说明「为什么这样做」，密度和现有代码保持一致。
- 改了用户能感知到的行为（设置项、界面、解压规则、配置项），同步改 README 对应的段落。
- 仓库现在是私有的，不要改仓库的公开状态。

## 和用户协作

- 用户是编程新手：用中文回复，说法简单直白，专业词后面用括号补一句解释。
- 用户的全局要求：先检查用户思路里的问题，指出没考虑到的风险和成本；不要加免责声明一类的套话。
