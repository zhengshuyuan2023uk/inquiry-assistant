# 数据输入与知识契约

本版接受明确提供的文件，也支持由管理员预先配置的 WhatsApp 桥接只读来源。网页的“更新聊天”只读取已授权的固定会话名单，不接受任意文件路径或自行扩大账号范围。真实来源与演练来源分开，桥接记录不保证完整历史。

规范化 JSON 数组、含 `messages` 数组的 JSON 对象，或一行一条记录的 JSONL：

```json
{
  "project_id": "your-company",
  "account_id": "authorized-source-01",
  "conversation_id": "buyer-001",
  "message_id": "stable-source-message-001",
  "direction": "inbound",
  "body": "原始正文",
  "sent_at": "2026-09-25T01:00:00Z",
  "received_at": "2026-09-25T01:01:00Z",
  "mode": "customer"
}
```

`direction` 为 inbound/outbound；两个时间必须含时区；正文非空。消息身份由公司+账号+会话+消息 ID 组成。相同 ID 的正文、方向或发送时间变化会冲突；不同接收时间的重复消息不新增。当前不实现渠道的编辑/撤回事件，遇到差异不能悄悄覆盖。

工作区只接受与自身公司及模式一致的消息。所有记录先验证、再整批写入，批次来源标签、摘要、记录数和结果与消息同事务记录。批次导入时间是真实 UTC 系统时间；消息接收时间可以是获准导出的历史来源时间，用于每日统计，不应伪造。

Chatwoot 输入为 `message_created` 事件数组，需要明确 `account.id`、`inbox.id`、`conversation.id`、消息 ID、created_at、private、content_type。命令显式设置账户/收件箱白名单；`message_type` 接受 incoming/outgoing 或 0/1。私有备注、活动和不支持的媒体输出 ignored 原因。映射后的账号为 `cw:<account>:<inbox>`、会话为 `cw:<conversation>`、消息为 `cw:<message>`。

该转换不检验文件确实来自 Chatwoot，也不提供 Webhook 签名或网络身份认证。必须由受控操作者提供获准来源文件；不能把文件适配器直接暴露成公共 Webhook 端点。

企业配置字段：id、name、mode、industry、required_fields、rules、knowledge。每份 knowledge 有唯一 id、version、title、content、valid_from、valid_until。有效期含首尾日。资料需包含实际负责人已确认的事实；未提供价格、资质或承运能力时，生成器应明确待核实。

当前知识包由操作者发布。网页支持用表单新增、修改、移除资料，以及维护自然语言回复策略；尚无多人审批和文档自动解析服务。客户对价格的要求不能作为企业真实报价。可选 `reply_strategy` 为最多12000字的沟通偏好，非空内容计入资料版本新鲜度。

日常维护从“企业资料”进入：新增资料时填写标题、正文和有效期，编号自动生成；修改或移除后统一保存。移除只影响当前知识，历史版本及旧草稿引用仍保留。资料变化后，旧回复需要重新核查并生成，不能直接采用。更新历史默认折叠。

“回复设置”集中维护三个部分：“怎么说”保存沟通偏好，“需要问什么”选择或添加询盘必备信息，“哪些情况先确认”维护企业接待要求。三部分统一保存，现有资料正文不随之改写。中文名称映射保留原有字段标识，已有未知或自定义字段也保留，不自动转换旧数据。

普通回复设置会保留已知的系统保障与演练声明，不把它们混进可编辑的业务要求；未知自定义规则不会被猜测为系统规则而隐藏。来源隔离、事实依据和禁止擅自发送仍由模型提示及程序边界约束，企业设置不能解除这些限制。

表单保留未保存提醒、失败输入和跨窗口版本冲突保护。“更多 → 负责人设置 → 高级资料维护”仍可维护完整企业 JSON，适合实施人员使用；该入口只是操作分组，不构成账号权限控制。

模型基础结果为 `summary/facts/missing_fields/uncertainties/next_action/reply/citations/needs_human`；新版回复任务另含 `rationale/reply_language/warnings`。业务员原文与调整要求在 request 快照中独立保存，不能写成客户消息。润色不推导报价字段。结构、消息引用、有效资料版本均校验；校验不能证明每句话语义正确。对客正文仍需人工核对。
