# 构建 Mac WhatsApp 连接组件

本项目的 Apple Silicon 现场安装包使用独立的 WhatsApp 连接进程。它将账号同步到的聊天保存在安装目录，工作台再按用户选中的客户手动导入。它不是 WhatsApp 官方 Business API，也不保证历史聊天全部可获取。

## 开发条件与命令

在 Apple Silicon Mac 上构建，需要 Python 3.10 或更新版本、Go 1.26 或更新版本，以及 Xcode Command Line Tools 提供的 C 编译工具链。产物最低目标为 macOS 14.0，启用 CGO。首次构建需要网络下载 `go.mod` / `go.sum` 固定的 Go 依赖；根据本机 Go 设置也可能下载所需工具链。业务员无需安装 Go。

在本仓库根目录执行：

```sh
python3 -B scripts/build_mac_bridge.py --output output/mac-bridge
```

输出目录必须尚不存在，脚本不会覆盖已有构建。如果需要另一份构建，换一个输出目录。`--source` 可显式选择另一份符合相同结构、带正确 `PROVENANCE.json` 的源码快照；默认使用本仓库的 `third_party/whatsapp-mcp`，不依赖任何开发者本机路径或已登录的 WhatsApp。

脚本会校验导入源码与上游许可证哈希，建立临时源码目录，移除旧 REST 服务与 `main`，加入 `deploy/macos/bridge_main.go` 和相应测试，然后运行 `go mod vendor`、Go 测试及编译。测试与编译使用已生成的 vendor，关闭模块代理下载。最后只执行生成程序的 `--version`；这一验证不会启动 WhatsApp、创建聊天库或请求扫码。

## 来源与修改范围

上游为 [lharries/whatsapp-mcp](https://github.com/lharries/whatsapp-mcp)，基于提交 `7d6a06dcdce1f01dfb24f60e1030d5efba9f3b88`。本仓库保存的是经过连接兼容性修补及依赖升级的构建输入，不能将它称为该提交的原样副本。固定来源说明与逐文件哈希见 [PROVENANCE.json](../third_party/whatsapp-mcp/PROVENANCE.json)。构建元数据从该文件取原始上游提交，不会将本项目自身的 Git 提交误记为上游提交。

原始上游入口具有发送及媒体下载 REST API。本项目构建时移除整段原 REST 服务和旧主函数，采用自己的本机控制入口：只有带令牌保护的回环地址 `/status` 和 `/shutdown`，并校验 Host / Origin。产品不暴露发送和媒体下载接口。账号配对二维码在本地生成；消息接收与必要的 WhatsApp 协议通信由 WhatsMeow 完成，“只读采集”不表示进程完全没有网络通信。

这里的快照只导入三个 Go 构建文件及上游许可，没有复制上游账号、聊天、媒体缓存或令牌。正常配对和使用通过安装套件的连接入口进行，不应直接运行未配置的裸程序。

## 构建结果与许可证

输出结构：

- `bin/whatsapp-bridge`：Apple Silicon 连接程序。
- `bridge-build.json`：来源、输入哈希、Go 版本、测试输出和产物 SHA-256。
- `source/whatsapp-bridge-source.tar.gz`：本次编译对应的转换后源码、项目控制入口、测试、Go 模块文件、来源记录及完整 vendor 源码。
- `LICENSES/`：上游 MIT、项目新增代码的 MIT、Go 许可及 vendor 中各组件许可和模块清单。

本项目自有新增代码适用根目录 MIT 许可；上游 `whatsapp-mcp` 的 MIT 与版权声明单独保留。依赖继续按各自条款授权，其中包括 GPL-3.0、MPL-2.0 及其他许可，**整个连接程序不能统一标成仅 MIT**。分发该二进制时，应同时保留对应源码归档、来源及各组件许可证，不要只复制可执行文件。整体第三方说明见 [THIRD_PARTY_NOTICES.md](../THIRD_PARTY_NOTICES.md)。

源码归档内的 `INQUIRY-MODIFICATIONS.md` 也提供离线 vendor 重编译命令。不同工具链或归档时间可能改变产物哈希；`bridge-build.json` 是该次实际构建的校验记录，不承诺跨机器二进制或归档字节完全一致。
