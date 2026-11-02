# 更新日志

## v0.1.1 — 2026-11-02

工程与可维护性更新，拉黑功能与 v0.1 完全一致。主要用于把发布流程自动化，并补上开源项目该有的基础设施。

### 新增

- `--version` 参数，GUI 标题栏也会显示版本号（`DouyinClean v0.1.1 · 抖音批量拉黑`）。
- `DouyinCleanConsole.exe` 正式由 `gui_console.py` 入口构建：与 GUI 同一套界面，但保留控制台与 stdout/stderr，便于在终端里排查报错。
- `LICENSE`：MIT 许可证。
- `CHANGELOG.md`、`.gitattributes`（统一 LF、标记二进制文件）。
- `DouyinClean.spec`：本地与 CI 共用的 PyInstaller 单文件打包配置。
- `requirements-dev.txt`：`playwright` + `pyinstaller`，供打包使用。
- `.github/workflows/release.yml`：推送 `v*` tag 自动在 `windows-latest` 上构建 exe、生成 SHA256 并上传到 Release；支持手动重建已有 tag。

### 变更

- README 增加 CI / 版本 / License 徽章，新增“构建 exe”“许可”章节，项目结构同步更新。
- Release 资产由人工上传改为 CI 构建，附带 `DouyinClean.exe.sha256` 便于校验。

## v0.1 — 2026-11-02

首个公开版本，提供独立 GUI 与 CLI 两种用法。

### 新增

- `DouyinClean.exe`：单文件 GUI，双击即用，无需 Python 环境。
- GUI 自动循环：跑完一轮后按 30 秒 / 10 分钟节奏继续；遇到 502 自动等待 180 秒重试。
- 断点续跑：成功拉黑的账号写入 `history.csv`，重跑自动跳过。
- CLI：`main.py` 支持 `--check`、`--execute`、`--limit`、`--confirm-each`、`--silent`、`--no-resume`、`--screenshots` 等参数。
- 只按用户名匹配关键词（默认 `短剧`、`剧场`，可通过 `keywords.txt` 修改）。
- 默认 DRY RUN；执行前二次确认；任一账号失败即停止。

### 安全边界

- 不读取、不打印、不写入 Cookie / Token / 账号密码，只复用本机 Chrome 的登录态。
- 只通过网页 UI 的“更多 → 拉黑 → 确认”操作，不调用私有接口。
- 识别到安全验证 / 滑块 / 访问频繁即停止，不尝试绕过。
- CDP 只允许连接 `127.0.0.1` / `localhost`。

## 计划中

- 抖音页面结构变化时的选择器自动探测。
- GUI 内直接编辑关键词、查看历史记录。
