# Live Translate

Live Translate 是一个 Windows 实时翻译工具。它采集电脑当前播放的声音，通过 Gemini Live API 将内容翻译为简体中文，并在窗口中持续显示译文。适合观看外语视频、直播、会议等场景。

## 使用方法

1. 在 GitHub 的 Releases 页面下载最新的 `LiveTranslate-*-windows-x64.zip`，解压整个压缩包，在解压后的 `LiveTranslate` 文件夹中运行 `LiveTranslate.exe`。请保留文件夹内的其他文件。
2. 准备 Google AI Studio API Key，在程序顶部输入。使用时需要能够连接 Gemini Live API；如需 HTTP 代理，可勾选“HTTP 代理”并填写地址和端口。
3. 点击“启动”，播放需要翻译的电脑音频，译文会显示在窗口中。点击“停止”结束翻译，“清空”可清除当前文本；图钉按钮可切换窗口置顶。

程序采集的是 Windows 默认扬声器的播放声音，不是麦克风输入。API Key 会保存在当前 Windows 用户的配置中，并使用 Windows DPAPI 加密。