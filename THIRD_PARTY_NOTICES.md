# 许可范围与第三方组件

根目录 [LICENSE](LICENSE) 是询盘助手自有源码与文档的 MIT 许可，包括 Python 程序、网页、测试、安装/构建脚本及本项目原创的桥接控制文件。它不替代 `third_party/`、外部依赖和打包组件自身的版权及许可。

| 范围 | 来源与许可 | 对应源码/声明 |
| --- | --- | --- |
| `third_party/whatsapp-mcp/` 上游代码 | [lharries/whatsapp-mcp](https://github.com/lharries/whatsapp-mcp)，MIT | 该目录保留上游 LICENSE 和经过验证的构建输入；修改及输入版本见该目录说明 |
| WhatsApp 桥接的 Go 依赖 | 各模块各自许可，包括 whatsmeow/util 的 MPL 2.0、libsignal 的 GPL v3，以及其他 MIT/BSD/Apache 组件 | `go.mod`、`go.sum` 锁定依赖；构建脚本将完整 vendor 源码和各模块原许可放入桥接输出 |
| Codex 后台可执行文件 | [openai/codex](https://github.com/openai/codex)，Apache 2.0 及随包 NOTICE | 二进制不进入本源码仓库；Mac 套件保留官方发布包来源、许可证与 NOTICE |
| Python macOS 安装器 | [Python](https://www.python.org/)，Python 及随附第三方许可 | 安装器不进入本源码仓库；Mac 套件包含官方安装器与许可 |
| Go 编译工具链及运行库 | [Go](https://go.dev/)，Go 所附 BSD 许可及声明 | 桥接构建输出保留 Go LICENSE/PATENTS（存在时） |

桥接是单独运行的可执行程序，包含上述不同许可的依赖；不能把完整桥接二进制或整个 Mac 安装包标为“全部 MIT”。分发修改版本时应保留各组件声明及其要求的对应源码。构建产物中的 `source/whatsapp-bridge-source.tar.gz` 含桥接与 vendor 对应源码；`LICENSES/` 含许可，`source/*.json` 记录版本、来源与哈希。

`templates/`、`examples/` 与测试中的企业、客户和消息都是合成演示素材，不是获准公开的客户资料。发布时不包括工作区、聊天数据库、AI/WhatsApp 登录、日志或密钥。

现有下载仓库的 r2 安装包是较早的分发快照。此源码仓库及从它构建的新版包随附自有 MIT 许可，保留第三方许可；不要用一句总括说明覆盖 r2 包内已有的第三方条款。
