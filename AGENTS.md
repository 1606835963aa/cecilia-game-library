# AGENTS.md

给接手这个项目的 AI 编程助手看的说明。面向用户的说明在 [README.md](README.md)。

## 项目是什么

塞西莉亚的游戏库：Windows 上的本地游戏管理软件，由两个独立进程组成。

| 进程 | 入口 | 作用 |
| --- | --- | --- |
| 自动解压 | `auto_extract.py`（`启动自动解压.bat`） | 常驻监视 `downloads`，等文件下完后解压、逐层试密码，全部成功后放进 `games/Game_编号` 并按设置删原包 |
| 游戏库 | `game_manager.py`（`打开游戏库.bat`，用 `pythonw` 启动） | FastAPI 本机小网站 + pywebview 窗口（WebView2），扫描入库、找资料、启动计时、手动导入 |

两个常驻进程共用 `config.json`（设置）和文件系统。各自每轮重新读 `config.json`，所以在游戏库设置页改的东西，自动解压下一轮就会用上。游戏库点「刷新」时另外启动一次性解压子进程（`auto_extract.py --once --events`），通过标准输出读取进度，完成后扫描入库。刷新和独立监视通过 `data/extract.lock` 互斥，避免同时处理同一个压缩包。

## 环境

- Windows 10 / 11，Python 3.11，7-Zip 装在 `C:\Program Files\7-Zip\`
- 依赖：`pip install cryptography fastapi uvicorn psutil pywin32 Pillow pywebview certifi`
- 接口测试另需 `pip install httpx`
- 源码无需构建；绿色版用 PyInstaller 打包，更新检查可运行 `python -m unittest discover -s tests`，没有 lint 配置

## 文件分工

| 文件 | 内容 |
| --- | --- |
| `auto_extract.py` | 解压引擎和基础工具：`load_config` / `save_config`、`DEFAULT_CONFIG`、`ROOT`、分卷识别（`archive_group`）、试密码（`choose_password`，一次把所有候选都试完）、GBK 密码 zip 自解密、套娃解压、安卓版分流、`scan_once` / `watch_loop` |
| `library.py` | SQLite 数据库（`data/library.db`，表 `games` 和 `sessions`）、引擎识别、主程序挑选、扫描入库、图标提取、低完整性标签修复、回收站删除 |
| `scraper.py` | 找资料：游戏文件本地读取（Ren'Py rpa、Godot pck、RPG Maker、Unity）+ Steam / VNDB / DLsite / itch.io / SubscribeStar 公开主页；`Worker` 是后台排队线程 |
| `itch_account.py` | itch.io 官网登录窗口、登录和成人内容状态检测、同一浏览器会话中的搜索、退出清理；登录状态由 WebView2 保存在 `data/webview` |
| `wishlist.py` | 无本地文件的愿望单、加入时版本和官网版本历史、与已下载游戏的关联 |
| `importer.py` | 手动导入：识别拖进来的路径，挪 / 复制 / 原地登记 / 解压；`Importer` 是后台排队线程，复用 `auto_extract.process_group` |
| `game_manager.py` | FastAPI 路由（`/api/*`）、`PlayTracker` 计时、`RefreshRunner` 刷新解压、`Shell` 窗口生命周期、单实例、皮肤的标题栏颜色 |
| `window_frame.py` | 自绘标题栏：Win32 子类化去掉系统标题栏，保留系统边框 |
| `app_version.py` | 软件发布版本，更新检查和打包共用 |
| `updates.py` | GitHub 正式版检查、下载校验、空闲更新调度；源码只检查，不覆盖源码 |
| `update_helper.py` | 独立更新助手、更新包清单校验、程序替换、启动确认和失败还原 |
| `web/index.html` | 页面骨架，含占位符 `__TOKEN__` `__THEME__` `__FRAME__` `__VERSION__`，由 `game_manager.index()` 替换 |
| `web/app.js` | 全部前端逻辑，原生 JS，无框架、无打包。路由用 hash：`#/`、`#/game/<id>`、`#/settings` |
| `web/style.css` | 默认皮肤「经典深色」（`steam`），颜色都是 `:root` 上的 CSS 变量 |
| `web/theme-paper.css` | 皮肤「浪子的真情」（`paper`），全部写在 `[data-theme="paper"]` 下，覆盖变量和少量组件样式 |
| `web/icon.ico` | 软件图标：窗口、任务栏、favicon、经典深色皮肤左上角 |
| `docs/` | README 用的图片 |
| `packaging/` | 绿色版打包：`cecilia.spec`（PyInstaller 配置，两个 exe 共用 `_internal`）和 `build.py`（打包、带上 7-Zip、压成 zip） |

模块依赖方向：`auto_extract` ← `library` ← `scraper` / `importer` ← `game_manager`。`auto_extract.py` 不能反过来 import 其他模块，它要能单独运行。

运行时生成、不进版本库：`downloads/`、`games/`、`android/`、`data/`（数据库、封面、解压状态、窗口状态、WebView2 数据、中转文件夹）、`logs/`（`extract.log`、`game_manager.log`）。

## 运行和调试

```bash
python game_manager.py --browser
```

用默认浏览器打开游戏库，日志直接打在命令行；不开窗口，自绘标题栏按钮不显示。正常用 `pythonw` 启动时日志在 `logs/game_manager.log`。

```bash
python auto_extract.py --once
```

扫一遍就退出。`--file <路径>` 只处理一个文件，`--dir <路径>` 临时换监视目录。

```bash
python packaging/build.py
```

打包绿色版，需要 PyInstaller 和装在默认位置的 7-Zip。结果在 `dist/塞西莉亚的游戏库/`（`塞西莉亚的游戏库.exe` 打开游戏库，`自动解压.exe` 常驻监视，`7-Zip/` 是自带的 7-Zip）和 `dist/CeciliaGameLibrary-portable.zip`（压缩包里的文件夹和 exe 仍是中文名）。`dist/` 不进版本库。发布时把这个 zip 传到 GitHub Releases。

游戏库是单实例：端口 8765 上已经有游戏库时，新进程只调 `/show` 把旧窗口叫到前台就退出。改了 Python 代码要先完全关掉旧的游戏库进程（窗口藏起来时进程还在），否则看到的还是旧版。

## 验证改动

更新流程用 `python -m unittest discover -s tests`，其他改动按范围自己验：

- **接口**：用 `fastapi.testclient.TestClient(game_manager.app)`，请求要带 `Host: 127.0.0.1` 和 `X-Token: game_manager.TOKEN` 头。
- **页面**：`--browser` 模式开起来，用 Playwright 之类的工具截图，两套皮肤都要看（设置页切换，或改 `config.json` 的 `theme`）。
- **解压**：用 7-Zip 现做带密码的测试压缩包。
- **和正在用的游戏库同时开**：设环境变量 `GAME_LIBRARY_PORT` 换个端口，否则新开的游戏库会认为已经有一个在运行，只把旧窗口叫到前台就退出。
- **绿色版**：把 `dist/塞西莉亚的游戏库` 复制到 `tmp/` 下，放好 `config.json` 和测试压缩包再运行两个 exe，数据都写在复制出来的文件夹里。
- **隔离**：`ROOT` 取的是脚本所在文件夹，把项目复制到 `tmp/` 下的沙盒里跑，就有独立的 `config.json`、`games/`、`data/`，不会碰用户的真实数据和密码。
- 临时文件放项目里的 `tmp/`（已在 `.gitignore`），用完删掉。

## 改代码时要注意

- **接口鉴权**：中间件 `guard` 只放行 Host 是 `127.0.0.1` / `localhost` 的请求；`/api/*` 还要带 `X-Token`，口令每次启动随机生成，写进页面的 `<meta name="token">`。新接口放在 `/api/` 下就自动受保护。
- **页面缓存**：`/` 和 `/web/*` 都返回 `Cache-Control: no-cache`，静态文件地址带 `?v=<web 文件最新修改时间>`。新加的 CSS / JS 文件也要在 `index.html` 里带 `?v=__VERSION__`。
- **WebView2 线程**：碰 `window.native` 下的 WebView2 对象必须走 `Shell.on_ui_thread`，在别的线程直接读写会让两边卡死。
- **itch.io 登录**：官网窗口不挂本机 `js_api`；登录信息不进设置接口、日志或 `config.json`。搜索通过官网窗口内同源请求执行，公开作品详情仍用普通 HTTP 请求。退出只清理 itch.io 的 Cookie，不清理游戏库页面数据；关主窗口时要销毁隐藏的官网窗口。
- **打包成 exe**：`auto_extract.ROOT` 是可写的数据位置（源码运行时是项目文件夹，打包后是 exe 所在文件夹），`APP_DIR` 是程序自带文件的位置（打包后是 `_internal`）。设置、数据、游戏用 `ROOT`，页面这类随程序发布的文件用 `APP_DIR`。打包后「刷新」调用旁边的 `自动解压.exe`，源码运行时调用 `python.exe auto_extract.py`。新加的随程序发布的文件要写进 `cecilia.spec` 的 `datas`；按名字动态加载的模块要写进 `hiddenimports`。
- **软件更新**：`app_version.VERSION` 和 GitHub 正式发布标签须一致。构建输出 zip、SHA-256 文件和包内 `update-manifest.json`，发布时上传 zip 和校验文件。更新只接受本仓库正式版且带 GitHub SHA-256 的 zip；助手是独立 exe，不依赖待替换的 `_internal`。只替换 `update_helper.OWNED` 列出的程序项，用户数据不在更新清单里。主程序阻止新写入并等待已有任务；自动解压在两轮之间响应更新标记并退出，助手完成后重开它。新版确认窗口能打开前不启动后台写入，失败则还原程序和升级前的数据库。更新任务放 `tmp/updates`，助手退出后清理，无法还原的备份保留。
- **拖放导入**：完整文件路径只有 Python 这边拿得到（`Shell.on_drop` 里的 `pywebviewFullPath`），再通过 `window.importDropped(...)` 交给页面。
- **改设置**：`save_config(updates)` 只合并给出的键。密码列表的增删接口（`/api/passwords`、`/api/passwords/remove`）每次在当下的文件上加减一个，不整份覆盖，因为导入线程也会往里写密码。
- **目录分工**：`download_dirs` 是待解压来源，`games_dir` 是 PC 成品目录，`watch_dirs` 是额外的游戏扫描目录。下载目录不能与 PC、安卓成品目录重叠。完整解压和套娃检查在中转目录完成，内层失败保留来源原包。`publish_game` 去掉单一包装层并原子放进 `Game_编号`，`.game-library.json` 保存原名供入库和资料匹配。
- **两边同时解压**：导入用 `data/import` 当中转文件夹（`importer.py` 里改了 `auto_extract.WORK_DIR`），自动解压用 `data/`；导入时在 `games` 里用的中转文件夹叫 `_正在导入`，它在 `SKIP_DIRS` 里，游戏库扫描和自动解压都跳过；自动解压还会跳过所有以 `_` 开头的文件夹。
- **数据库迁移**：新字段加进 `library.EXTRA_COLUMNS`，`init_db` 启动时会给老数据库自动补列。路径在库里存相对 `ROOT` 的形式（`to_rel` / `to_abs`）。
- **愿望单**：`games.is_wishlist=1` 的记录不需要物理路径，扫描、未处理队列和文件操作必须跳过。`wish_version` 只在加入或用户手动填写时改，之后获取官网资料只更新最近版本和历史；官网没标注或联网失败保留旧记录。关联本地游戏保留愿望单编号、版本和备注，合并记录，不移动游戏文件。
- **手动改过的不覆盖**：`exe_locked=1` 的主程序扫描不改；文件名带 `-custom` 的封面、背景是手动换的，找资料时不覆盖。
- **皮肤**：新组件先在 `style.css` 用变量写，浪子的真情皮肤需要不同样子时在 `theme-paper.css` 的 `[data-theme="paper"]` 下覆盖。加皮肤还要在 `game_manager.THEMES` 里配标题栏和底色。
- **资料来源**：DLsite 和 itch.io 用的不是公开接口，抓取失败先怀疑网站改版。只读游戏文件里的数据，不运行游戏里的任何东西（`scraper._BasicTypesOnly` 限制了 pickle 能还原的类型）。
- **资料补查**：刷新会给未匹配或上次失败的本地记录补查资料。`scrape_status=cleared` 是用户主动清除，自动补查跳过；手动点「自动获取」仍可重新匹配。已匹配的记录优先按原官网地址更新，联网失败保留已有内容。订阅平台只读取作者主页的公开介绍、头像和横幅，不把帖子日期当版本号。
- **界面文字**：全部中文，口语、简短，面向不懂电脑的用户。错误信息用 `HTTPException(状态码, "中文说明")`，前端会直接弹出来。

## 规矩

- **`config.json` 绝不能提交**。里面有用户的解压密码，已在 `.gitignore`。提交时逐个 `git add` 文件，不要用 `git add -A` 或 `git add .`。
- **用户没有自测通过、没有明确说提交，不要 commit，更不要 push。**
- 示例、注释、文档里的游戏名、作品编号、网站名都用中性的虚构例子（比如 `SummerMemories`），不写真实作品。
- 注释和文档只描述代码现在的样子，不写修改历史、不写开发阶段的字眼。
- 代码注释用中文，说明「为什么这样做」，密度和现有代码保持一致。
- README 面向普通用户，只写这是什么、怎么下载、怎么用和常见问题，不写实现细节、配置项和文件结构；用户能感知到的用法变了，同步改 README 对应的段落。全部配置项和默认值见 `auto_extract.DEFAULT_CONFIG`。
- 仓库已公开开源，不要改仓库的公开状态。

## 和用户协作

- 用户是编程新手：用中文回复，说法简单直白，专业词后面用括号补一句解释。
- 用户的全局要求：先检查用户思路里的问题，指出没考虑到的风险和成本；不要加免责声明一类的套话。
