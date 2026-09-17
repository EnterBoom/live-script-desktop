# 直播稿拆解工具

将直播视频拆成带时间节点的话术 Excel。支持阿里云百炼 + OSS 云端转写，以及 whisper.cpp 本地转写；两种模式都需要 FFmpeg。仓库不包含任何 API 密钥、用户视频、模型或转写结果。

## 安装和使用

- macOS: 从 Releases 下载 DMG；Windows: 下载 NSIS `-setup.exe`。首次启动若系统提示未签名，请自行确认来源。本项目当前没有 Apple/Windows 签名证书。
- 先安装 Python 3.10+、FFmpeg，并确保 `python3` (macOS) 或 `py -3` (Windows) 和 `ffmpeg` 在 PATH 中。Windows Python 安装时勾选 Python Launcher。
- 云端模式另需 `python3 -m pip install -r requirements-cloud.txt` (Windows 使用 `py -3 -m pip install -r requirements-cloud.txt`)，并在应用中自行填写百炼和 OSS 凭据。只有点击开始转写才会产生实际的云端调用费用。
- 本地模式另需安装 whisper.cpp 的 `whisper-cli` 并放入 PATH，下载兼容的 ggml `.bin` 模型，在应用中选取模型文件。仓库不提供模型或 whisper-cli。
- 转写结果和视频保存在你选定的位置；设置和最近记录保存在用户目录 `.codex/live-script-desktop/`，其中设置包含你填写的凭据，请勿分享该文件。

## 开发和构建

需要 Node.js 20.19+、Rust 和 [Tauri 2 系统依赖](https://v2.tauri.app/start/prerequisites/)。

```sh
npm ci
npm run build
npm run tauri build -- --bundles dmg   # macOS
npm run tauri build -- --bundles nsis  # Windows
```

GitHub Actions 在各自系统原生构建并上传安装包。CI 仅验证源码、Python 模块导入及构建，不调用付费接口；Windows 实机转写尚需在安装了上述依赖的机器上验收。
