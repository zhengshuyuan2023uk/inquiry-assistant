# Mac 交付套件的组件来源

询盘助手业务版本：0.7.0a1。本源码构建的 Mac 套件为 r3，补齐自有 MIT 许可及源码来源；已发布的 r2 是较早快照。当前构建继续使用经过校验的相同官方运行组件，安装流程保持一致。

| 组件 | 版本与来源 | 套件内说明 |
| --- | --- | --- |
| 询盘助手 | 0.7.0a1 | `app/` 为产品程序，`deploy/macos/` 为安装与启动程序 |
| Codex | 0.145.0，官方 `@openai/codex` 的 darwin-arm64 发布包 | 后台可执行文件位于 `bin/codex`，对应许可/NOTICE 位于 `LICENSES/` |
| Python | 3.13.15，[官方发布页](https://www.python.org/downloads/release/python-31315/) | 官方 macOS 安装器位于 `runtime/python-macos.pkg`，许可位于 `LICENSES/` |
| WhatsApp 连接程序 | 基于 [lharries/whatsapp-mcp](https://github.com/lharries/whatsapp-mcp) 的受控构建 | 可执行文件位于 `bin/whatsapp-bridge`；已移除发送和下载接口，仅接收缓存及本机状态/停止接口 |

`source/components.json` 保存官方组件下载地址、版本与校验值。`source/bridge-build.json` 保存桥接对应源码版本、构建结果与哈希；`source/whatsapp-bridge-source.tar.gz` 包含对应修改及 vendor 依赖源码。第三方许可保存在 `LICENSES/`，具体适用条款以各组件随附文件为准。

AI 授权由客户在自己的电脑上完成。当前安装流程采用 ChatGPT/Codex 登录，模型仍在云端运行；可用模型与额度以客户账号为准。[官方认证说明](https://learn.chatgpt.com/docs/auth)

安装包不包含任何登录会话、客户数据库或密钥。自有源码采用 MIT，安装包的 `LICENSES/InquiryAssistant-MIT.txt` 保存许可，`LICENSES/InquiryAssistant-THIRD-PARTY.md` 说明适用范围。第三方组件继续采用各自许可，不能将完整混合安装包标为纯 MIT。
