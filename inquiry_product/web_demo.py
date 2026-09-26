"""Deterministic, entirely synthetic workbench examples; never calls a model.

Display labels are an optional, validated sidecar. They are deliberately not
customer identity data and never apply to customer-mode workspaces.
"""
from copy import deepcopy
import hashlib
import json
import os
from pathlib import Path
import tempfile

from .core.engine import validate_result
from .workspace import Workspace


COMPANY_ID = "logistics-demo"
ACCOUNT_ID = "workbench:demo-sales"
AS_OF = "2026-09-25"
DEFAULT_CONVERSATION = "workbench:general-ocean"
MARKER_FILE = "workbench-demo.json"
_FIXTURE = "synthetic_workbench_v1"
_FIELDS = ["origin", "destination", "goods", "quantity", "weight_kg",
           "volume_cbm", "transport_mode", "deadline"]


def _config():
    template = Path(__file__).resolve().parent.parent / "templates" / "logistics-demo.json"
    config = json.loads(template.read_text(encoding="utf-8"))
    config["name"] = "远帆物流（虚构演练）"
    config["industry"] = "物流询盘演练"
    config["rules"][0] = (
        "全部企业、客户、询盘与业务资料为虚构演练；地名仅用于模拟询盘，"
        "不表示路线已经确认可承运。所有回复只供人工审核。")
    config["rules"][6] = "回复不得称已经发送、已经接单、已成交或已预订；审核只保存演练结果，不发送消息。"
    for document in config["knowledge"]:
        document["content"] = document["content"].replace("演练物流 A", "远帆物流（虚构演练）")
    config["knowledge"][0]["content"] = (
        "simulation：远帆物流是工作台中的虚构企业。可以收集客户海运或空运询盘，"
        "但接收询盘不表示任一路线、货物或运输方式已确认可承运。"
        "先收集起运地、目的地、货物、件数、毛重、体积、运输方式及期望交付日期，"
        "再由人工核实承运条件与报价。客户给出的截止日是期望，不是交付承诺。"
        "当前没有可直接对客采用的有效运价、时效表或舱位表。")
    config["knowledge"].append({
        "id": "LOG-SPECIAL", "version": "1", "title": "含电池与 DDP 询盘核实清单（演练）",
        "content": (
            "simulation：本条是虚构企业的演练处理流程，不是运输法规或承运确认。"
            "客户询问含电池货物时，先收集电池类型、安装方式、规格及可提供的运输安全资料。"
            "DDP 或目的地派送询盘另收集完整收货地址和邮编，再交人工核实承运、清关及税费范围。"
            "没有核实结果时，应明确尚无法确认承运、DDP 或包税服务，不得自行报价。"),
        "valid_from": AS_OF, "valid_until": "2027-09-30",
    })
    return config


_SCENARIOS = [
    {
        "slug": "general-ocean", "title": "Ethan Cole",
        "subtitle": "Willow & Vale · 英国 · 虚构客户", "minute": 28,
        "messages": [
            ("inbound", "Hi, we’re planning a sea shipment of ceramic mugs from Ningbo to Felixstowe. Could you help with a quote?"),
            ("outbound", "Thanks, Ethan. Please share the number of cartons, gross weight, total volume and preferred delivery date."),
            ("inbound", "120 cartons, 960 kg gross. We hope to receive them by 30 November 2026. I’m still waiting for the final packing dimensions."),
        ],
        "facts": [
            ("origin", "宁波", 1), ("destination", "英国费利克斯托港", 1),
            ("goods", "陶瓷马克杯", 1), ("quantity", "120 箱", 3),
            ("weight_kg", "960 kg 毛重", 3), ("transport_mode", "海运", 1),
            ("deadline", "期望 2026-11-30 前收到", 3),
        ],
        "summary": "客户询问宁波至英国的陶瓷杯海运，已提供 120 箱和 960 kg。包装尺寸仍待供应商确认，尚不能核算完整报价。",
        "uncertainties": ["客户希望 11 月 30 日前收到，尚未核实能否满足。", "运价、承运条件和舱位需人工核实。"],
        "next_action": "优先取得总体积，或每种箱型的外箱尺寸与对应箱数，再交人工核价。",
        "reply": "Thanks, Ethan. I have 120 cartons / 960 kg for sea freight from Ningbo to Felixstowe, with your preferred arrival by 30 November. Could you share the total CBM, or the dimensions and carton count for each box size? Once we have those details, our team can check the shipping options and quotation. The rate and arrival date still need to be confirmed.",
        "citations": ["LOG-PROCESS", "LOG-INPUTS"],
    },
    {
        "slug": "battery-ddp", "title": "Sofia Reed",
        "subtitle": "Northbeam Device · 加拿大 · 虚构客户", "minute": 22,
        "messages": [
            ("inbound", "We have 200 portable speakers with built-in lithium batteries in Shenzhen. Can you send them by air to Toronto, DDP including tax?"),
            ("outbound", "Please share the carton count, gross weight, volume and the delivery details so the requirements can be checked."),
            ("inbound", "20 cartons, 140 kg gross, 1.2 CBM. I’ll get the battery specifications and postal code from our team. Can you confirm DDP first?"),
        ],
        "facts": [
            ("origin", "深圳", 1), ("destination", "加拿大多伦多（详细地址待补）", 1),
            ("goods", "内置锂电池便携音箱", 1), ("quantity", "200 台 / 20 箱", 3),
            ("weight_kg", "140 kg 毛重", 3), ("volume_cbm", "1.2 CBM", 3),
            ("transport_mode", "空运", 1),
        ],
        "summary": "客户询问含锂电池音箱空运至多伦多，并要求先确认 DDP。基础货量已齐，但电池规格、详细地址及交付日期仍待补充。",
        "uncertainties": ["未确认含电池货物的承运条件。", "DDP、清关及税费范围没有已确认依据。", "目的地完整地址和邮编待补。"],
        "next_action": "先收集电池规格、现有运输安全资料、完整地址与期望交付日，再交人工核实 DDP 和承运条件。",
        "reply": "Thanks, Sofia. I’ve noted 20 cartons, 140 kg and 1.2 CBM. We cannot confirm battery acceptance or DDP / tax coverage yet. Please send the battery specifications and any available transport safety documents, the full delivery address with postal code, and your preferred delivery date. Our team will review those details before confirming whether a suitable service is available.",
        "citations": ["LOG-INPUTS", "LOG-SPECIAL"],
    },
    {
        "slug": "details-needed", "title": "Luis Vega",
        "subtitle": "Orion House · 西班牙 · 虚构客户", "minute": 16,
        "messages": [
            ("inbound", "Hello, what is your shipping price from China to Europe?"),
            ("outbound", "Happy to collect the details. Which collection city and final destination are you considering, and what will the goods be?"),
            ("inbound", "I’m still choosing a supplier and haven’t decided on the items. What information should I prepare first?"),
        ],
        "facts": [],
        "summary": "客户处于采购前期，尚未确定供应商和商品。当前只有宽泛的出发与目的地区域，无法形成具体物流报价。",
        "uncertainties": ["China / Europe 不足以确认具体起运地和目的地。", "没有可用于本次询盘的有效运价。"],
        "next_action": "先确认起运城市、目的城市及货物类别，再引导客户向供应商索取装箱资料。",
        "reply": "Hi Luis, to start, please share the collection city, destination city and the items you are considering. Once you choose a supplier, ask them for the carton count, gross weight and total volume or packing dimensions. Your preferred shipping method and delivery date will also help. A price can only be checked after we have the shipment details.",
        "citations": ["LOG-PROCESS", "LOG-INPUTS"],
    },
    {
        "slug": "reviewed-air", "title": "Mira Stone",
        "subtitle": "Cedar Arc · 荷兰 · 虚构客户", "minute": 10,
        "messages": [
            ("inbound", "Could you check air freight for cotton tote bags from Guangzhou to Amsterdam? There are 8 cartons, 96 kg gross and 0.64 CBM."),
            ("outbound", "Thank you. What delivery date would you prefer?"),
            ("inbound", "Ideally before 15 October 2026. Please let me know what needs to be checked before you can quote."),
        ],
        "facts": [
            ("origin", "广州", 1), ("destination", "荷兰阿姆斯特丹", 1),
            ("goods", "棉质手提袋", 1), ("quantity", "8 箱", 1),
            ("weight_kg", "96 kg 毛重", 1), ("volume_cbm", "0.64 CBM", 1),
            ("transport_mode", "空运", 1), ("deadline", "期望 2026-10-15 前收到", 3),
        ],
        "summary": "客户已提供棉质手提袋空运的八项基础要素。可交人工核对具体承运、计费及交付条件；期望日期不能当作承诺。",
        "uncertainties": ["具体运价、承运条件及能否满足期望日期仍待人工核实。"],
        "next_action": "由人工核对路线、计费条件与可行交付安排，再准备报价。",
        "reply": "Thanks, Mira. We have the details: cotton tote bags, 8 cartons, 96 kg and 0.64 CBM, by air from Guangzhou to Amsterdam, with your preferred arrival before 15 October. Our team needs to verify the shipping options, chargeable details and timing before a quotation can be confirmed.",
        "citations": ["LOG-PROCESS", "LOG-INPUTS"], "approve": True,
    },
    {
        "slug": "new-paper", "title": "Noah Lane",
        "subtitle": "Harbour Form · 英国 · 虚构客户", "minute": 4,
        "messages": [
            ("inbound", "We’re preparing 60 cartons of paper display stands in Yiwu. Delivery is to Manchester, and sea freight is preferred."),
            ("outbound", "Please share the gross weight, packed volume and your preferred delivery date when available."),
            ("inbound", "The supplier is measuring the shipment now. Could you check what else you’ll need for the quote?"),
        ],
    },
]


def _messages(scenario):
    return [{
        "project_id": COMPANY_ID, "account_id": ACCOUNT_ID,
        "conversation_id": "workbench:" + scenario["slug"],
        "message_id": f"wb-{scenario['slug']}-{index:02d}", "mode": "simulation",
        "direction": direction, "body": body,
        "sent_at": f"{AS_OF}T00:{scenario['minute'] + index - 1:02d}:00Z",
        "received_at": f"{AS_OF}T00:{scenario['minute'] + index - 1:02d}:01Z",
    } for index, (direction, body) in enumerate(scenario["messages"], 1)]


class _DemoFixtureRunner:
    """Only recognizes the exact synthetic scenarios; not a model substitute."""
    name = "demo_fixture"

    def analyze(self, context):
        if context.get("mode") != "simulation" or context.get("project_id") != COMPANY_ID:
            raise ValueError("示例草稿运行器仅用于指定合成演练")
        scenario = next((item for item in _SCENARIOS
                         if "facts" in item and context.get("messages") == _messages(item)), None)
        if scenario is None:
            raise ValueError("示例草稿只适用于未修改的固定样本；新消息请重新进行模型分析")
        facts = [{"field": field, "value": value,
                  "message_ids": [f"wb-{scenario['slug']}-{index:02d}"]}
                 for field, value, index in scenario["facts"]]
        # The quantity in this case combines unit count and carton count.
        if scenario["slug"] == "battery-ddp":
            next(item for item in facts if item["field"] == "quantity")["message_ids"] = [
                "wb-battery-ddp-01", "wb-battery-ddp-03"]
        result = {key: deepcopy(scenario[key]) for key in ("summary", "uncertainties", "next_action", "reply")}
        result.update({"facts": facts,
                       "missing_fields": [field for field in _FIELDS if field not in {item["field"] for item in facts}],
                       "citations": [{"id": identity, "version": "1"} for identity in scenario["citations"]],
                       "needs_human": True})
        return validate_result(result, context)


def _digest(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
                                     separators=(",", ":")).encode("utf-8")).hexdigest()


def _labels():
    return [{"account_id": ACCOUNT_ID, "conversation_id": "workbench:" + item["slug"],
             "title": item["title"], "subtitle": item["subtitle"],
             "seed_messages_sha256": _digest(_messages(item))} for item in _SCENARIOS]


def _new_target(root):
    target = Path(root).expanduser().absolute()
    if target.is_symlink() or (target.exists() and (not target.is_dir() or any(target.iterdir()))):
        raise ValueError("演练目标必须是不存在的路径或空目录；不会覆盖现有工作区")
    return target


def seed_workspace(root):
    """Create and return a complete new Workspace containing synthetic records."""
    target = _new_target(root)
    target.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=".workbench-demo-", dir=target.parent) as temporary:
        workspace = Workspace.init(Path(temporary) / "workspace", COMPANY_ID, _config(), mode="simulation")
        records = [record for item in _SCENARIOS for record in _messages(item)]
        workspace.import_messages(records, source_label="工作台固定合成样本（非真实聊天）")
        service = workspace.service(_DemoFixtureRunner())
        try:
            for item in _SCENARIOS:
                if "facts" not in item:
                    continue
                draft = service.analyze(COMPANY_ID, ACCOUNT_ID, "workbench:" + item["slug"], AS_OF)
                if item.get("approve"):
                    draft = service.review(draft["id"], "approve", "demo:示例审核员（演练）",
                                           draft["result"]["reply"], AS_OF)
        finally:
            service.store.close()
        marker = {"schema_version": 1, "kind": _FIXTURE, "mode": "simulation",
                  "company_id": COMPANY_ID, "workspace_created_at": workspace.manifest["created_at"],
                  "initial_release": workspace.manifest["knowledge_history"][0]["release_id"],
                  "seed_as_of": AS_OF, "draft_source": "demo_fixture", "conversations": _labels()}
        marker_path = workspace.root / MARKER_FILE
        with marker_path.open("x", encoding="utf-8") as output:
            json.dump(marker, output, ensure_ascii=False, indent=2)
            output.write("\n")
        marker_path.chmod(0o600)
        if len(conversation_labels(workspace)) != len(_SCENARIOS):
            raise ValueError("工作台演练展示资料校验失败")
        _new_target(target)
        os.rename(workspace.root, target)
    return Workspace.load(target)


def demo_summary(workspace):
    """Summarize a validated demo after seeding, keeping fixture origin explicit."""
    if len(conversation_labels(workspace)) != len(_SCENARIOS):
        raise ValueError("不是已校验的工作台演练工作区")
    store = workspace.open_store()
    try:
        conversations = store.conversations(COMPANY_ID)
        draft_rows = list(store.connection.execute("SELECT status,conversation_id FROM drafts"))
        messages = sum(item["message_count"] for item in conversations)
    finally:
        store.close()
    return {"workspace": str(workspace.root), "company_id": COMPANY_ID, "mode": "simulation",
            "conversations": len(conversations), "messages": messages, "drafts": len(draft_rows),
            "pending": sum(item[0] == "pending" for item in draft_rows),
            "approved": sum(item[0] == "approved" for item in draft_rows),
            "unanalysed": len(conversations) - len({item[1] for item in draft_rows}),
            "default_account": ACCOUNT_ID, "default_conversation": DEFAULT_CONVERSATION,
            "draft_source": "demo_fixture", "real_model_calls": 0, "external_messages_sent": 0,
            "notice": "企业、客户、聊天、示例草稿与审核记录均为合成演练；未调用 AI 模型，未发送消息。"}


def conversation_labels(workspace):
    """Fail closed to IDs when a demo sidecar is absent, copied or inconsistent."""
    if workspace.mode != "simulation" or workspace.company_id != COMPANY_ID:
        return {}
    path = workspace.root / MARKER_FILE
    if path.is_symlink() or not path.is_file():
        return {}
    try:
        if path.stat().st_size > 64_000:
            return {}
        marker = json.loads(path.read_text(encoding="utf-8"))
        expected = {"schema_version": 1, "kind": _FIXTURE, "mode": "simulation",
                    "company_id": COMPANY_ID, "workspace_created_at": workspace.manifest["created_at"],
                    "initial_release": workspace.manifest["knowledge_history"][0]["release_id"],
                    "seed_as_of": AS_OF, "draft_source": "demo_fixture", "conversations": _labels()}
        if marker != expected:
            return {}
        store = workspace.open_store()
        try:
            for scenario in _SCENARIOS:
                stored = {row["message_id"]: row for row in store.messages(
                    COMPANY_ID, ACCOUNT_ID, "workbench:" + scenario["slug"])}
                if any(stored.get(row["message_id"]) != row for row in _messages(scenario)):
                    return {}
        finally:
            store.close()
        return {(item["account_id"], item["conversation_id"]):
                {"title": item["title"], "subtitle": item["subtitle"]} for item in marker["conversations"]}
    except (OSError, ValueError, KeyError, TypeError):
        return {}
