from __future__ import annotations

import json
import threading
import uuid
from pathlib import Path
from typing import Any


DATA_SOURCES = {"CURRENT", "LATEST_SEGMENT", "SELECTED_SEGMENTS"}


DEFAULT_TEMPLATES = [
    {
        "template_id": "current_brief",
        "name": "当前盘面简报",
        "description": "直接读取当前快照，适合快速了解当前情况。",
        "data_source": "CURRENT",
        "instructions": (
            "严格按以下顺序输出，不要增加其他章节：\n"
            "1. 数据时间与完整性\n2. 总可用资金\n3. 现货：数量、价格、总收益\n"
            "4. 合约：多空数量、价格、总收益\n5. 项目总收益\n6. 一句风险提示。\n"
            "所有均价统一保留小数点后5位。"
        ),
        "include_accounts": False,
        "include_events": False,
        "max_output_tokens": 700,
    },
    {
        "template_id": "latest_detection_report",
        "name": "最近检测汇报",
        "description": "直接读取最近一个已停止时段，按变化量汇报。",
        "data_source": "LATEST_SEGMENT",
        "instructions": (
            "输出一份可直接转发的做市检测汇报，固定格式：\n"
            "【检测时间】\n【核心变化】总可用资金、现货数量、现货总收益、合约净持仓、合约总收益、项目总收益\n"
            "【账户变化】只列出有变化的账户\n【结论】不超过3句。\n"
            "所有变化量必须带正负号；数据不完整时在第一行显著警告。\n"
            "所有均价统一保留小数点后5位。"
        ),
        "include_accounts": True,
        "include_events": False,
        "max_output_tokens": 1100,
    },
    {
        "template_id": "selected_segments_report",
        "name": "勾选时段合并汇报",
        "description": "合并检测列表中勾选的多个时段，后端先精确求和。",
        "data_source": "SELECTED_SEGMENTS",
        "instructions": (
            "这是多个检测时段的合并数据。请生成可直接转发的简明汇报：\n"
            "1. 时段数量与数据完整性\n2. 资金总变化\n3. 现货数量与收益总变化\n"
            "4. 合约持仓与收益总变化\n5. 变化最大的账户\n6. 不超过3句的结论。\n"
            "不得自行重新计算或补齐缺失数据。\n所有均价统一保留小数点后5位。"
        ),
        "include_accounts": True,
        "include_events": False,
        "max_output_tokens": 1200,
    },
    {
        "template_id": "latest_spot_cost_report",
        "name": "最近时段现货成本",
        "description": "汇报最近检测时段的持仓成本变化与每一阶段买入成本。",
        "data_source": "LATEST_SEGMENT",
        "instructions": (
            "只输出以下现货成本内容：\n1. 检测项目、工作簿和时间\n"
            "2. 开始持仓成本、结束持仓成本、持仓成本变化\n3. 结束时持仓成本均价\n"
            "4. 按时间列出每一个买入阶段的账户、买入数量、买入成本和买入均价。\n"
            "成本只能使用 spot_cost_analysis，禁止使用现货市价代替；无法推导的阶段明确标记数据不完整。\n"
            "所有均价统一保留小数点后5位。"
        ),
        "include_accounts": True,
        "include_events": False,
        "max_output_tokens": 1100,
    },
]


class ChatTemplateStore:
    def __init__(self, path: Path):
        self.path = path
        self.lock = threading.RLock()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if not self.path.exists():
            self._write(DEFAULT_TEMPLATES)
        else:
            items = self._read()
            existing = {item.get("template_id") for item in items}
            missing = [item for item in DEFAULT_TEMPLATES if item["template_id"] not in existing]
            if missing:
                self._write(items + missing)

    def _read(self) -> list[dict[str, Any]]:
        with self.lock:
            try:
                data = json.loads(self.path.read_text(encoding="utf-8"))
                return data if isinstance(data, list) else []
            except (OSError, json.JSONDecodeError):
                return []

    def _write(self, items: list[dict[str, Any]]) -> None:
        with self.lock:
            temporary = self.path.with_suffix(".tmp")
            temporary.write_text(json.dumps(items, ensure_ascii=False, indent=2), encoding="utf-8")
            temporary.replace(self.path)

    @staticmethod
    def _validated(payload: dict[str, Any], template_id: str | None = None) -> dict[str, Any]:
        name = str(payload.get("name") or "").strip()
        instructions = str(payload.get("instructions") or "").strip()
        source = str(payload.get("data_source") or "CURRENT").upper()
        if not name or len(name) > 60:
            raise ValueError("模板名称必须为 1-60 个字符")
        if not instructions or len(instructions) > 6000:
            raise ValueError("输出要求必须为 1-6000 个字符")
        if source not in DATA_SOURCES:
            raise ValueError("不支持的数据范围")
        try:
            tokens = min(max(int(payload.get("max_output_tokens") or 1000), 300), 3000)
        except (TypeError, ValueError):
            tokens = 1000
        return {
            "template_id": template_id or str(uuid.uuid4()),
            "name": name,
            "description": str(payload.get("description") or "").strip()[:300],
            "data_source": source,
            "instructions": instructions,
            "include_accounts": bool(payload.get("include_accounts")),
            "include_events": bool(payload.get("include_events")),
            "max_output_tokens": tokens,
        }

    def list(self) -> list[dict[str, Any]]:
        return self._read()

    def get(self, template_id: str) -> dict[str, Any]:
        item = next((x for x in self._read() if x.get("template_id") == template_id), None)
        if not item:
            raise ValueError("回答模板不存在")
        return item

    def save(self, payload: dict[str, Any], template_id: str | None = None) -> dict[str, Any]:
        items = self._read()
        item = self._validated(payload, template_id)
        if template_id:
            index = next((i for i, x in enumerate(items) if x.get("template_id") == template_id), None)
            if index is None:
                raise ValueError("回答模板不存在")
            items[index] = item
        else:
            items.append(item)
        self._write(items)
        return item

    def delete(self, template_id: str) -> None:
        items = self._read()
        filtered = [x for x in items if x.get("template_id") != template_id]
        if len(filtered) == len(items):
            raise ValueError("回答模板不存在")
        self._write(filtered)
