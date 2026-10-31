# DouyinClean

抖音网页版批量拉黑工具。复用本机 Chrome 的已登录会话，按**用户名**中的关键词筛选账号，先预览、再由用户确认后，通过网页上正常的“更多 → 拉黑”入口逐个处理。

> 默认 **DRY RUN**，不会执行任何拉黑。只有显式传入 `--execute` 并在终端确认后才会操作。
>
> 工具默认只连接**已经运行**的 Chrome，复用当前抖音标签页，**不会启动浏览器、不会新建窗口或标签页**。`--silent`（别名 `--background`）可完全后台运行，只写日志，不弹确认框。
>
> `--launch-chrome` 会打开可见的 Chrome 窗口，属于显式弹窗行为，必须由你主动执行；不要在游戏或全屏应用运行时使用。
>
> 本项目不获取账号密码、不读取或输出 Cookie/Token、不调用抖音私有接口、不绕过验证码或风控。

## 0. 独立 GUI / exe（推荐）

直接双击项目根目录的 **`DouyinClean.exe`**：

- 默认勾选“启动后自动开始”和“开始后最小化窗口”，打开后自动连接 `http://127.0.0.1:9229` 并开始循环搜索/拉黑，窗口最小化到任务栏。
- 搜索词、间隔、单轮上限、滚动次数都可在界面修改；匹配关键词仍来自 `keywords.txt`。
- 自动循环：跑完一轮后按 30 秒/10 分钟节奏继续；遇到 502 自动等待 180 秒并重试。
- 断点续跑：已成功拉黑的账号写入 `history.csv`，重跑自动跳过。
- 需要先有一个带调试端口的 Chrome。点界面上的“启动/连接 Chrome”会复用 `.chrome-profile` 启动；首次启动 exe 会解压到 `.pyi-tmp`，可能慢几秒，黑色控制台窗口会自动最小化。
- “关闭工具 Chrome”只结束带 9229 调试端口/`.chrome-profile` 的 Chrome，不会动你日常用的 Chrome；会先弹确认。
- 需要人工登录/验证码时，任务会暂停并写日志，不会绕过。

GUI 源码在 `gui.py`，可用 `python gui.py` 直接运行；`python gui.py --selftest` 做无窗口自检。

## 1. 功能

- 通过 Playwright `connect_over_cdp` 连接本机已登录的 Chrome，复用现有登录状态。
- 从当前抖音页面（关注列表、粉丝列表、用户搜索结果等）读取可见的 `/user/` 链接。
- 只按**用户名**匹配关键词，默认关键词为 `短剧`、`剧场`，可在 `keywords.txt` 中修改。
- 展示待处理账号列表，默认不执行；执行前再次要求确认。
- 通过网页 UI 的“更多 → 拉黑 → 确认”完成操作，不伪造请求。
- 成功、失败、跳过数量统计，逐条写入 `history.csv`。
- 成功记录自动用于断点续跑；失败时立即停止，不继续批量操作。
- 操作间隔默认 2.5–5 秒，单次默认最多 30 个账号，避免高频操作。
- 默认复用当前抖音标签页；静默模式下不启动 Chrome、不新建标签页、不弹确认框。

## 2. 安全设计与边界

| 项目 | 做法 |
| --- | --- |
| 登录凭证 | 不读取、不打印、不写入文件、不提交 Git；只复用 Chrome 自身的登录态 |
| 屏蔽方式 | 网页 UI 正常点击，不构造私有 API 请求 |
| 验证码/风控 | 识别到“安全验证 / 滑块 / 访问频繁”等提示即停止并报告，不尝试绕过 |
| 默认行为 | DRY RUN，仅预览 |
| 失败处理 | 任一账号失败即停止后续操作，并在日志和 `history.csv` 中记录原因 |
| 操作频率 | 串行执行，默认 2.5–5 秒/个，单次上限 30 个（可调但最大 100） |
| 数据外传 | 无网络上传逻辑；只在本机读写日志与 CSV |
| 调试端口 | 只允许连接 `127.0.0.1/localhost`，不暴露到局域网 |
| 窗口/弹窗 | 默认不新建标签页；静默模式拒绝启动 Chrome、拒绝新建标签页；原生弹窗自动关闭并停止 |

**调试端口的安全提醒**：Chrome 的 CDP 端口开启期间，本机其他程序也可以控制这个浏览器。使用完成后请关闭 Chrome；不要将 9222 端口转发到局域网或公网。

## 3. 环境要求

- Windows 10/11
- Python 3.11+
- Google Chrome（已登录抖音网页版）
- Playwright Python 包（连接现有 Chrome 即可，**不需要**执行 `playwright install` 下载浏览器）

## 4. 安装

```powershell
cd DouyinClean
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
```

如果使用本机已经准备好的项目虚拟环境（`.venv`），直接运行即可：

```powershell
.\.venv\Scripts\python.exe main.py --check
```

## 5. 一次性准备 Chrome 调试会话

Playwright 只能连接**启动时带了调试端口**的 Chrome。当前已经打开的普通 Chrome 无法在不重启的情况下被附加，这是 Chrome 的限制，不是工具能绕过的。

准备步骤：

1. 保存工作，**完全退出 Chrome**（包括托盘图标、后台进程）。
2. 用现有配置目录启动 Chrome，并临时开启本机调试端口：

```powershell
& "C:\Program Files\Google\Chrome\Application\chrome.exe" `
  --remote-debugging-port=9222 `
  --remote-debugging-address=127.0.0.1 `
  --user-data-dir="$env:LOCALAPPDATA\Google\Chrome\User Data" `
  --profile-directory="Default" `
  --restore-last-session
```

也可以让工具代启动（同样要求 Chrome 已完全退出）：

```powershell
.\.venv\Scripts\python.exe main.py --launch-chrome --url "https://www.douyin.com/"
```

`--launch-chrome` 的行为：

- 复用现有 `User Data` 目录和 `Default` 配置，因此保留登录状态；
- 只对本次启动附加 `--remote-debugging-port`，**不修改** Chrome 首选项或配置文件；
- 发现 Chrome 仍在运行时会拒绝启动，**不会主动结束**任何 Chrome 进程；
- 调试端口只绑定 `127.0.0.1`。

如果你使用的是 `Profile 1`，把 `--profile-directory` 改成 `Profile 1`。

> **重要：不要在不方便被打断时执行 `--launch-chrome`。** 它会打开可见的 Chrome 窗口并可能抢焦点。更安全的方式是你自己先启动带调试端口的 Chrome，然后让工具只连接、不启动。工具自身永远不会主动结束 Chrome 进程。

## 6. 使用

### 6.1 先预览（默认 DRY RUN）

先在 Chrome 中手动打开目标页面，例如：

- 抖音“我的 → 关注”列表；
- 用户搜索结果页；
- 其他能看到目标账号的列表页。

然后运行：

```powershell
.\.venv\Scripts\python.exe main.py
```

工具会连接当前 Chrome，使用最后一个抖音标签页，滚动采集可见账号，按关键词筛选并打印列表。**不会新建标签页，也不会执行拉黑。**

也可以显式指定页面：

```powershell
.\.venv\Scripts\python.exe main.py --url "https://www.douyin.com/search/短剧?type=user"
```

`--url` 会在当前抖音标签页中依次打开（不会新开标签）。如果确实需要新标签页，显式加上 `--new-tab`；这是唯一会新建标签页的开关。

### 6.2 检查环境

```powershell
.\.venv\Scripts\python.exe main.py --check
```

输出 Python、Playwright、Chrome 版本、Chrome 进程数、CDP 端口状态、用户数据目录和关键词。就绪时返回 0，未就绪时返回 3。

### 6.3 执行拉黑

```powershell
.\.venv\Scripts\python.exe main.py --execute
```

流程：

1. 采集并匹配；
2. 打印待处理列表；
3. 询问是否继续（静默模式必须用 `--yes` 代替）；
4. 逐个打开账号主页，点击“更多 → 拉黑”，在确认弹窗中确认；
5. 校验页面出现“已拉黑/解除拉黑/拉黑成功”等状态；
6. 每处理一个账号写入一次 `history.csv`。

小批量试跑：

```powershell
.\.venv\Scripts\python.exe main.py --execute --limit 3 --confirm-each
```

- `--limit 3`：本次最多处理 3 个；
- `--confirm-each`：每个账号执行前单独确认；
- `--yes`：跳过批次确认（不建议首次使用）；
- `--new-tab`：允许新建标签页（默认复用当前抖音标签页；这是唯一会新建标签页的开关，需你明确确认）；
- `--silent` / `--background`：后台静默，不弹确认框、不询问、只写日志（显式加 `--new-tab` 时仍会新建标签页）；
- `--screenshots`：失败时保存本地截图到 `artifacts/`；
- `--keep-tab`：结束后保留自动化标签页，便于检查。

### 6.4 断点续跑

每成功拉黑一个账号，`history.csv` 会追加一条 `status=blocked` 的记录。再次运行时：

- `status=blocked` 的账号自动跳过；
- `status=failed` 的账号会重试；
- 中断（Ctrl+C）后已成功的记录不会丢失；
- 使用 `--no-resume` 可以忽略历史记录。

`history.csv` 包含账号用户名等本地数据，已在 `.gitignore` 中排除，请勿提交。

### 6.5 后台静默运行（不弹窗）

前提：Chrome **已经**带调试端口运行，并且已经打开抖音目标页面。静默模式不会代启动 Chrome，也不会新建标签页。

预览（只写日志，不执行拉黑）：

```powershell
Start-Process -WindowStyle Hidden `
  -FilePath ".\.venv\Scripts\pythonw.exe" `
  -ArgumentList "main.py --silent" `
  -WorkingDirectory "C:\Users\paw_C\Documents\deepseek-harness\default-workspace\DouyinClean"
```

执行（必须同时给 `--execute --yes`）：

```powershell
Start-Process -WindowStyle Hidden `
  -FilePath ".\.venv\Scripts\pythonw.exe" `
  -ArgumentList "main.py --silent --execute --yes --limit 10" `
  -WorkingDirectory "C:\Users\paw_C\Documents\deepseek-harness\default-workspace\DouyinClean"
```

静默模式的规则：

- 不输出控制台、不调用 `input()`、不主动聚焦浏览器；默认不新建窗口/标签页；
- 只有显式加 `--new-tab` 时才会新建标签页（需你确认）；
- 只写 `douyin_clean.log`，成功/失败/跳过数量也在日志中；
- 页面触发原生弹窗或新窗口时自动关闭，并按失败处理；
- 登录失效、验证码、风控提示：停止并写日志，不绕过；
- `--silent --execute` 必须显式带 `--yes`，否则拒绝执行；
- `--silent --launch-chrome` 默认拒绝（会产生可见窗口）；只有再加 `--allow-window` 才允许，仍不建议在游戏/全屏时使用；
- `--confirm-each` 与 `--silent` 互斥；
- 导航会改变当前标签页内容，但不会弹出新窗口。

如果 `pythonw.exe` 不存在，可用 `python.exe` 配合 `-WindowStyle Hidden`，效果相同（会隐藏控制台窗口）。

## 7. 采集与匹配规则

只抓取当前 DOM 中可见的、指向 `/user/<id>` 的链接；用户名取链接/卡片的第一行有效文字，过滤“关注、推荐、更多”等导航词。

匹配逻辑与需求一致：

```python
KEYWORDS = ["短剧", "剧场"]

def should_block(username: str) -> bool:
    return any(keyword in username for keyword in KEYWORDS)
```

- 只匹配用户名，不匹配抖音号、简介、作品标题；
- 不做模糊拼音、不做正则，避免误伤；
- 修改 `keywords.txt` 后直接生效，无需改代码。

## 8. 输出文件

| 文件 | 说明 |
| --- | --- |
| `douyin_clean.log` | 中文运行日志（已脱敏，UTF-8） |
| `history.csv` | 逐账号操作记录，用于统计和断点续跑 |
| `artifacts/` | 仅在 `--screenshots` 时生成失败截图 |

`history.csv` 字段：

```text
timestamp,account_id,username,status,reason,url
```

`status` 取值：`blocked`（成功）、`skipped`（跳过/已拉黑）、`failed`（失败）、`dry_run`（预留）。

## 9. 常见问题

### 9.1 “CDP 调试端口未就绪”

Chrome 没有以调试端口启动，或端口被占用。按第 5 节完全退出 Chrome 后重新启动。工具不会结束你的 Chrome 进程。

### 9.2 “未找到账号主页的‘更多’入口”

抖音前端结构可能已改版。工具会停止并在 `douyin_clean.log` 中报告，不会猜测按钮位置。可更新 `browser.py` 中的 `MORE_BUTTON_SELECTORS`，或在 dry run 中确认账号列表后手动处理。

### 9.3 “未找到‘拉黑’选项”

可能原因：

- 账号状态特殊（如已注销、私密账号）；
- 页面结构与当前选择器不匹配；
- 弹出的不是账号操作菜单。

工具会停止，不会点击“举报/取消关注”等其他按钮。

### 9.4 登录失效或出现验证码

日志会提示“需要人工验证/登录状态失效”。请在 Chrome 中手动完成登录或验证，再重新运行。工具不会绕过验证码、风控或访问控制。

### 9.5 页面没有采集到账号

- 确认当前标签页确实是用户列表/搜索结果页；
- 先手动向下滚动，确认页面能加载更多用户；
- 增大 `--max-scrolls`（默认 8，最大 50）；
- 抖音可能要求登录后才能看到完整列表。

### 9.6 静默模式提示“没有已打开的抖音页面”

静默模式不会新建标签页。请先在 Chrome 中手动打开抖音目标页面；只有在你明确同意时，才加 `--new-tab` 让工具新开标签页。

### 9.7 如何确保运行时不弹窗

- 不要使用 `--launch-chrome`，或先自己启动 Chrome，工具只执行连接；
- 使用 `--silent`，并且不要加 `--new-tab`；
- 工具会关闭页面触发的原生弹窗和新窗口，并在日志中记录；
- 如果页面要求登录/验证码，工具会停止，不会弹出任何登录窗口。

## 10. 限制

- 不处理验证码、滑块、短信验证等风控环节。
- 不保证适配抖音所有页面结构；默认只针对“用户列表 + 账号主页”的常见网页 UI。
- 不调用私有接口，因此采集速度受页面滚动和渲染速度限制。
- 单次上限 100，默认 30；频率受 `config.py` 中的 `action_delay` 控制。
- 本项目只应使用在你自己的账号和有权限管理的数据上。

## 11. 项目结构

```text
DouyinClean/
├── main.py          # CLI 入口、环境检查、确认与统计
├── config.py        # 配置与校验（不含凭证）
├── browser.py       # Chrome/CDP 连接、页面采集、状态识别
├── matcher.py       # 用户名清洗、关键词匹配、去重
├── blocker.py       # 网页 UI 拉黑流程、失败即停
├── logger.py        # 中文日志、脱敏、history.csv
├── keywords.txt     # 关键词（每行一个）
├── history.csv      # 本地操作历史（运行时生成/追加）
├── requirements.txt
├── README.md
└── .gitignore
```

## 12. 开发自检

不依赖浏览器的逻辑自检：

```powershell
.\.venv\Scripts\python.exe main.py --self-test
```

覆盖关键词匹配、用户名清洗、去重、上限、日志脱敏和历史记录读写。
