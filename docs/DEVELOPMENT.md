# 开发、测试与构建

所有命令在仓库根目录执行。应用使用 Python 3.10+ 标准库和 SQLite，无需安装 pip 依赖；支持 POSIX 环境，主要交付目标为 macOS。前端为原生 HTML / CSS / JavaScript，不需要前端打包。Node 18+ 仅用于运行可选的前端离线检查。

## 运行与数据目录

```sh
python3 -B create_workbench_demo.py --workspace workspaces/demo
python3 -B start_workbench.py --workspace workspaces/demo --port 0 --open
```

演示创建只写入合成数据，不连接 WhatsApp 或模型。已有目录不要再次初始化；需要全新演示时换一个工作区路径。终端前台运行按 Ctrl+C 停止。

需要后台运行时可用以下入口；停止命令先验证实例身份，不要用模糊进程名终止其他服务：

```sh
python3 -B start_workbench.py --workspace workspaces/demo --port 0 --background --open
python3 -B start_workbench.py --workspace workspaces/demo --stop
```

同一工作区重复启动复用已记录的后台实例；一个进程只服务一个工作区。工作区主要文件如下：

| 位置 | 内容 |
| --- | --- |
| `workspace.json` | 公司、模式与当前知识版本指针 |
| `knowledge/releases/` | 按内容摘要保存的知识历史 |
| `data/inquiries.sqlite3` | 消息、草稿、审核、批次与运行记录 |
| `connections/` | 本机数据源配置与客户扫描状态，不随备份导出 |
| `logs/`、`reports/` | 运行诊断与工作区报告 |

Mac 套件默认安装在当前用户的 `~/Library/Application Support/InquiryAssistant/`；其 `workspace/`、`codex-home/` 和 `bridge_run/store/` 分别承载业务数据、AI 登录和 WhatsApp 登录/同步缓存。不要复制整个安装目录来分发产品或提交故障报告。

## 模型与授权

默认网页运行器在首次生成时启动 `codex app-server --stdio`，复用后台进程，每次请求创建独立临时会话。当前适配基于 Codex 0.145.0；升级后台版本前需要验证协议、模型目录、生成和取消流程。

开发机须让已授权的 `codex` 命令可由 `PATH` 找到。程序从当前 `CODEX_HOME` 指定目录或默认 `~/.codex` 选择登录来源，要求存在文件凭据 `auth.json`。App Server 运行目录仅建立到该凭据的引用，不读取或复制其内容；Codex 自身的刷新可能更新原凭据。仅系统钥匙串认证尚未适配。不要将开发者登录打包给客户。

软件会沿用当前网络代理设置，但隔离外部配置与工具。模型在云端运行，使用授权账号的可用模型和额度。模型目录查询不是推理；真正生成和润色才调用模型。认证、额度和模型可用性由服务账号决定，MIT 许可不包含这些服务。

负责人可显式选择 `--runner exec` 排查兼容问题；程序不会在 App Server 失败后自动切换并重复推理。CLI 的 `analyze` 子命令仍使用 exec 运行器。真实客户资料交给模型前，网页要求确认，CLI 要求 `--allow-customer-model`。

## 核心模块

| 模块 | 职责 |
| --- | --- |
| `inquiry_product/workspace.py` | 公司与模式绑定、知识版本、备份恢复 |
| `inquiry_product/core/` | 输入与配置校验、SQLite、分析和人工审核 |
| `inquiry_product/web_server.py`、`web/` | 本机 HTTP 接口与业务员工作台 |
| `app_server.py`、`jobs.py`、`models.py` | 后台协议、任务状态、模型目录 |
| `manual_sync.py`、`customer_management.py`、`whatsapp_names.py` | 只读来源、客户选择、名称同步 |
| `adapters.py` | JSON / JSONL 和 Chatwoot 文件事件转换 |
| `deploy/macos/` | 现场安装、后台桥接、编号启动入口 |

企业知识格式和消息字段见 [INPUTS.md](INPUTS.md)。所有操作使用明确工作区；CLI 的 `--workspace` 必须放在子命令前：

```sh
python3 -B -m inquiry_product --help
python3 -B -m inquiry_product --workspace workspaces/demo status
python3 -B -m inquiry_product --workspace workspaces/demo health
python3 -B configure_whatsapp.py --help
```

`configure_whatsapp.py` 绑定已有客户工作区与桥接消息库，不执行 WhatsApp 登录。正式工作区需使用已核对的真实企业配置，不能把虚构模板改个模式就用于客户。

## 测试

完整测试还会执行 macOS 安装入口的 shell 契约，要求存在 `/bin/zsh`。macOS 已自带；在 Linux 上运行完整测试前先安装 zsh。普通合成演示不依赖它。

```sh
python3 -B -m unittest discover -s tests
python3 -B verify_delivery.py
node --test tests/test_sales_ui.js tests/test_customer_ui.js tests/test_stream_ui.js tests/test_knowledge_ui.js
```

这些测试默认使用临时目录、合成数据和离线替身，不需要 AI 授权，也不连接真实 WhatsApp。`verify_delivery.py` 检查工作区隔离、导入、知识变化、审核和恢复；Node 检查使用离线 UI 环境，不等于真实浏览器视觉验收。真实渠道覆盖和回复质量需要另行在授权范围内验证。

## 备份与恢复

```sh
python3 -B -m inquiry_product --workspace workspaces/demo backup --output backups/demo-001.zip
python3 -B -m inquiry_product restore --archive backups/demo-001.zip --destination workspaces/restored-demo
python3 -B -m inquiry_product --workspace workspaces/restored-demo health
```

备份与恢复均不覆盖已有目标。备份包含已导入聊天、知识和采用记录，不包含桥接登录、AI 登录或本机来源绑定；恢复后重新绑定来源。它不是加密备份。真实工作区应按客户约定保护，升级先在恢复副本上验证。

## 发布与 Mac 构建

源码 ZIP 使用白名单打包，排除运行工作区、数据库、凭据和报告：

```sh
python3 -B package_release.py --output dist/inquiry-assistant-source-0.7.0a1.zip
```

目标文件已存在时脚本会拒绝覆盖。源码 ZIP 供具备运行环境的开发者使用；它不等于带 Python、Codex 和桥接二进制的现场安装套件。

Mac 套件需要 Apple 芯片 Mac、Go 和 Xcode Command Line Tools，目标 macOS 14+。以下命令从已发布 r2 套件提取官方 Codex / Python 组件，再从本仓库的固定桥接输入构建，最后生成新的 r3 套件：

```sh
mkdir -p dist
curl --fail --location \
  https://github.com/zhengshuyuan2023uk/inquiry-assistant-downloads/releases/download/macos-arm64-0.7.0a1-r2/inquiry-assistant-0.7.0a1-macos-arm64-r2.zip \
  --output dist/inquiry-assistant-0.7.0a1-macos-arm64-r2.zip
python3 -B scripts/prepare_mac_components.py \
  --bundle dist/inquiry-assistant-0.7.0a1-macos-arm64-r2.zip \
  --output output/mac-components
python3 -B scripts/build_mac_bridge.py --output output/mac-bridge
python3 -B scripts/package_mac_onsite.py \
  --components output/mac-components \
  --bridge output/mac-bridge \
  --output dist/inquiry-assistant-0.7.0a1-macos-arm64-r3.zip
```

已有 r2 下载文件时跳过下载命令；其余输出应使用新目录或新文件名。`prepare_mac_components.py` 核对固定发布包 SHA-256，只提取官方程序、安装器和对应许可，不恢复任何运行状态。

桥接源自 [lharries/whatsapp-mcp](https://github.com/lharries/whatsapp-mcp)，默认输入为仓库中的 `third_party/whatsapp-mcp`，不依赖开发者私有目录。产品构建移除了发送、下载 HTTP 接口，只保留接收缓存与受控本机状态/停止接口。构建脚本会测试并附带对应 vendor 源码和许可，不需要 WhatsApp 登录；首次获取 Go 依赖需要网络。固定输入与修改方式见 [BRIDGE_BUILD.md](BRIDGE_BUILD.md)。

新构建的 r3 增加自有代码 MIT 许可文件，不会自动上传或覆盖已发布 r2。官方组件信息见 [MAC_COMPONENTS.md](MAC_COMPONENTS.md)，第三方许可见 [THIRD_PARTY_NOTICES.md](../THIRD_PARTY_NOTICES.md)。不要将运行中的桥接目录、登录状态或客户数据作为构建输入打包。

公开源码与测试允许独立检查业务逻辑；安装套件是否能用于某位客户，仍要按[现场安装手册](MAC_ONSITE.md)完成实机验收。
