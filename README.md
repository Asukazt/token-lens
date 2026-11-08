# Token Lens · 用量账本

<p>
  <a href="https://github.com/zhutao2006/token-lens/releases/latest"><img alt="Release" src="https://img.shields.io/github/v/release/zhutao2006/token-lens?sort=semver&display_name=tag&color=2ea44f"></a>
  <a href="LICENSE"><img alt="License" src="https://img.shields.io/github/license/zhutao2006/token-lens?color=blue"></a>
  <img alt="Platform" src="https://img.shields.io/badge/platform-Windows-0078D4?logo=windows">
  <img alt="Python" src="https://img.shields.io/badge/python-3.10%2B-3776AB?logo=python&logoColor=white">
  <a href="https://github.com/zhutao2006/token-lens/releases"><img alt="Downloads" src="https://img.shields.io/github/downloads/zhutao2006/token-lens/total?color=orange"></a>
</p>

<img src="logo.png" width="88" align="right" alt="Token Lens">

Windows 上运行的 Codex 插件（`api-balance@local`）：余额悬浮窗、逐会话 token 账本、模型费用估算与分时计价。

## 下载

安装包以 ZIP 形式发布在本仓库的 **Releases** 页：

- 最新版本：[v2.0.1 · Token Lens 2.0.1](https://github.com/zhutao2006/token-lens/releases/latest)
- 全部版本：[Releases](https://github.com/zhutao2006/token-lens/releases)

## 安装

1. 下载并解压 `token-lens-2.0.1.zip` 到准备长期保留的目录（安装后请不要移动该目录）。
2. 双击 `install.cmd`，或在 PowerShell 中运行：

   ```powershell
   powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\install.ps1
   ```

3. 重启 Codex，在设置的插件页启用 **Token Lens · 用量账本**。

安装脚本会优先查找 Codex 自带的 Python，其次使用 PATH 中的 Python；随后按你的电脑生成 `.mcp.json`，注册名为 `local` 的本地市场并安装 `api-balance@local`。生成的路径文件不受 Git 跟踪。

> 若你已经有其他名为 `local` 的本地市场，请先确认是否需要把来源切换到此目录。本包需要先运行安装脚本，不支持直接把 GitHub 地址当作已准备好的插件市场安装。

## 环境要求

- Windows
- Python 3.10+
- `tkinter`、`Pillow`（缺少 Pillow 时执行 `python -m pip install -r requirements.txt`）

## 使用

- 在对话中询问余额、用量，或打开／关闭悬浮窗。
- 悬浮窗包含总览、对话、用量和设置四个页签。
- 在设置中填写自己的 API 地址和密钥，按需配置阿里云或火山引擎账单权限。
- 支持自定义模型单价、倍率、闲时时段和周末计价；显示费用是估算值。
- 本机配置由设置页保存，也可以把 `config.example.json` 复制为同目录下的 `config.json` 后自行填写。

默认会从本机 `~/.codex/config.toml` 读取中转接口配置，并读取本机 Codex 会话日志统计 token。若希望关闭配置继承，可设置 `relay.inherit` 为 `false`，同时清空 `relay.base_url` 和 `relay.api_key`。

## 检查与卸载

```powershell
# 只生成本地启动配置并检查 Python，暂不注册到 Codex
.\install.ps1 -PrepareOnly

# 离线自检，不需要填写密钥
python -B .\check.py

# 卸载插件
.\install.ps1 -Uninstall
```

## 数据与隐私

发布包使用白名单打包，**不包含** API Key、AccessKey、本地 `config.json`、Codex 配置、账单缓存、余额历史、会话日志、窗口状态、运行日志或截图。

运行时，余额查询会向你配置的 API／云厂商发送相应认证信息；价格同步会访问厂商公开价格页，节假日同步会访问 `timor.tech`。会话统计在本机计算。凭据以明文保存在本机配置文件中，请妥善保护文件权限。

`.gitignore` 会忽略本地配置和运行数据；对外分享时仍应检查实际文件清单。

## 许可

[MIT](LICENSE)