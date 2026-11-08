# Token Lens · 用量账本

Windows 上运行的 Codex 插件（`api-balance@local`）：余额悬浮窗、逐会话 token 账本、模型费用估算与分时计价。

## 下载

安装包以 ZIP 形式发布在本仓库的 **Releases** 页：

- 最新版本：[v2.0.1 · Token Lens 2.0.1](../../releases/latest)

解压后双击 `install.cmd`（或运行 `install.ps1`）完成安装，随后重启 Codex 并在插件页启用。

## 环境要求

- Windows
- Python 3.10+
- `tkinter`、`Pillow`（缺少 Pillow 时执行 `python -m pip install -r requirements.txt`）

## 数据与隐私

发布包使用白名单打包，不包含 API Key、AccessKey、本地 `config.json`、Codex 配置、账单缓存、余额历史、会话日志、窗口状态或运行日志。

## 许可

MIT，见 [LICENSE](LICENSE)。