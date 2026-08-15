from __future__ import annotations

import json
import os
import re
import subprocess
import urllib.error
import urllib.request
from typing import Any, Callable

from .detection import DetectionService
from .manager import ProjectManager


KEYCHAIN_SERVICE = "APRMonitorOpenAI"


class ApiKeyStore:
    def get(self) -> str | None:
        value = os.environ.get("OPENAI_API_KEY", "").strip()
        if value:
            return value
        try:
            result = subprocess.run(
                ["security", "find-generic-password", "-s", KEYCHAIN_SERVICE, "-w"],
                capture_output=True, text=True, timeout=5, check=False,
            )
            return result.stdout.strip() or None if result.returncode == 0 else None
        except (FileNotFoundError, subprocess.SubprocessError):
            return None

    def set(self, value: str) -> None:
        key = value.strip()
        if not key.startswith("sk-") or len(key) < 20:
            raise ValueError("API Key 格式不正确")
        account = os.environ.get("USER", "apr-monitor")
        result = subprocess.run(
            ["security", "add-generic-password", "-U", "-a", account, "-s", KEYCHAIN_SERVICE, "-w", key],
            capture_output=True, text=True, timeout=10, check=False,
        )
        if result.returncode != 0:
            raise RuntimeError("写入 macOS 钥匙串失败")


TOOLS = [
    {
        "type": "function", "name": "get_workbook_cells",
        "description": "读取项目已保存 Excel 的指定单元格。用户指定 G7/H7 等单元格时必须使用；合并单元格会返回左上角锚点的真实值。",
        "parameters": {
            "type": "object",
            "properties": {
                "project_id": {"type": "string"},
                "addresses": {"type": "array", "items": {"type": "string"}, "maxItems": 20},
            },
            "required": ["project_id", "addresses"], "additionalProperties": False,
        },
        "strict": True,
    },
    {
        "type": "function", "name": "get_spot_cost_analysis",
        "description": "获取现货当前持仓成本、成本均价以及每个检测阶段的买入数量和推导买入成本。询问现货成本或均价时必须使用，不得用市价代替。",
        "parameters": {"type": "object", "properties": {"project_id": {"type": "string"}}, "required": ["project_id"], "additionalProperties": False},
        "strict": True,
    },
    {
        "type": "function", "name": "get_current_summary",
        "description": "获取某个做市项目当前完整汇总和账户明细。",
        "parameters": {"type": "object", "properties": {"project_id": {"type": "string"}}, "required": ["project_id"], "additionalProperties": False},
        "strict": True,
    },
    {
        "type": "function", "name": "list_detection_tasks",
        "description": "列出项目的检测任务及全部时段，用于理解第几次检测、开始停止时间。",
        "parameters": {"type": "object", "properties": {"project_id": {"type": "string"}}, "required": ["project_id"], "additionalProperties": False},
        "strict": True,
    },
    {
        "type": "function", "name": "get_detection_report",
        "description": "获取一个检测时段的确定性报告、账户变化和全部中间事件。",
        "parameters": {"type": "object", "properties": {"segment_id": {"type": "string"}}, "required": ["segment_id"], "additionalProperties": False},
        "strict": True,
    },
    {
        "type": "function", "name": "combine_detection_segments",
        "description": "精确合并多个检测时段，所有数值由后端计算，不要自行相加。",
        "parameters": {
            "type": "object", "properties": {"segment_ids": {"type": "array", "items": {"type": "string"}}},
            "required": ["segment_ids"], "additionalProperties": False,
        },
        "strict": True,
    },
    {
        "type": "function", "name": "get_project_history",
        "description": "获取项目指定时间范围的历史快照。",
        "parameters": {
            "type": "object",
            "properties": {"project_id": {"type": "string"}, "from_time": {"type": ["string", "null"]}, "to_time": {"type": ["string", "null"]}, "limit": {"type": "integer"}},
            "required": ["project_id", "from_time", "to_time", "limit"], "additionalProperties": False,
        },
        "strict": True,
    },
]


class DataAssistant:
    def __init__(self, projects: ProjectManager, detections: DetectionService, model: str = "gpt-5.6-terra"):
        self.projects = projects
        self.detections = detections
        self.model = model
        self.keys = ApiKeyStore()

    def status(self) -> dict:
        return {"configured": bool(self.keys.get()), "model": self.model, "storage": "macOS Keychain or OPENAI_API_KEY"}

    def set_key(self, key: str) -> dict:
        self.keys.set(key)
        return self.status()

    def _call_api(self, payload: dict) -> dict:
        key = self.keys.get()
        if not key:
            raise RuntimeError("OPENAI_API_KEY_NOT_CONFIGURED")
        request = urllib.request.Request(
            "https://api.openai.com/v1/responses",
            data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=60) as response:
                return json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", "replace")[:1000]
            raise RuntimeError(f"OpenAI API {exc.code}: {detail}") from exc
        except urllib.error.URLError as exc:
            raise RuntimeError(f"OpenAI API 连接失败: {exc.reason}") from exc

    def _tool(self, name: str, args: dict) -> Any:
        if name == "get_workbook_cells":
            addresses = [str(value).upper() for value in args["addresses"]]
            if any(not re.fullmatch(r"[A-Z]{1,3}[1-9][0-9]{0,5}", value) for value in addresses):
                raise ValueError("单元格地址格式不正确")
            return self.projects.engine(args["project_id"]).parser.read_cells(addresses)
        if name == "get_spot_cost_analysis":
            return self._spot_cost_context(args["project_id"])
        if name == "get_current_summary":
            data = self.projects.engine(args["project_id"]).aggregate()
            data["health"] = self.projects.engine(args["project_id"]).health()
            return data
        if name == "list_detection_tasks":
            return self.detections.list_tasks(args["project_id"])
        if name == "get_detection_report":
            return self.detections.get_segment(args["segment_id"])
        if name == "combine_detection_segments":
            return self.detections.combine(args["segment_ids"])
        if name == "get_project_history":
            return self.projects.engine(args["project_id"]).history(args["from_time"], args["to_time"], min(max(args["limit"], 1), 500))
        raise ValueError(f"未知数据工具: {name}")

    def _spot_cost_context(self, project_id: str) -> dict:
        summary = self.projects.engine(project_id).aggregate()
        stages = []
        for task in self.detections.list_tasks(project_id):
            for segment in task.get("segments", []):
                report = segment.get("report")
                if not report:
                    continue
                stages.append({
                    "task_id": task["task_id"], "task_name": task["name"],
                    "segment_id": segment["segment_id"], "ordinal": segment["ordinal"],
                    "started_at": segment["started_at"], "ended_at": segment["ended_at"],
                    "has_gap": segment["has_gap"],
                    "spot_cost_analysis": report.get("spot_cost_analysis"),
                })
        return {
            "definition": "现货持仓成本和买入均价由资金与数量的稳定变动推导，现货市价只用于计算市值和持仓收益",
            "project_id": project_id,
            "current": {
                "as_of": summary["as_of"], "position_qty": summary["spot"]["qty"],
                "position_cost": summary["spot"]["cost_basis"], "avg_cost": summary["spot"]["avg_cost"],
                "holding_total_avg_cost_from_sheet": summary["spot"]["holding_total_avg_cost"],
                "holding_total_avg_cost_source": summary["spot"]["holding_total_avg_cost_source"],
                "market_price_for_reference_only": summary["prices"]["spot"],
                "accounts": [
                    {
                        "account_name": item.get("account_name"), "position_qty": item.get("position_qty"),
                        "position_cost": item.get("spot_cost_basis"), "avg_cost": item.get("spot_avg_cost"),
                        "quality_status": item.get("quality_status"),
                    }
                    for item in summary["spot"].get("accounts", [])
                ],
            },
            "detection_stages": stages,
        }

    def _sheet_metric_context(self, project_id: str, text: str = "") -> dict:
        refs = {value.upper() for value in re.findall(r"(?<![A-Z0-9])([A-Za-z]{1,3}[1-9][0-9]{0,5})(?![A-Z0-9])", text)}
        refs.update({"G7", "H7"})
        cells = self.projects.engine(project_id).parser.read_cells(sorted(refs))
        return {
            "definition": "现货持仓总均价只能取 APR实时表合并单元格 G7:H7 的锚点 G7 保存值，不得使用内部成本引擎 avg_cost。",
            "spot_holding_total_avg_cost": next((item["value"] for item in cells if item["requested_address"] == "G7"), None),
            "source_cell": "APR实时表!G7:H7 (anchor G7)",
            "cells": cells,
        }

    def _enforce_sheet_metrics(self, answer: str, project_id: str) -> str:
        context = self._sheet_metric_context(project_id)
        value = context["spot_holding_total_avg_cost"]
        if value is None:
            return answer
        shown = f"{float(value):.12g}"
        pattern = re.compile(
            r"(?m)^([ \t]*(?:[-*•][ \t]*)?(?:\*{0,2})现货持仓(?:总)?均价(?:\*{0,2})\s*(?:[:：=]|是|为)\s*)"
            r"[-+]?\d[\d,]*(?:\.\d+)?"
        )
        return pattern.sub(lambda match: f"{match.group(1)}{shown}", answer)

    @staticmethod
    def _enforce_stage_purchase_avg(answer: str, context: dict) -> str:
        data = context.get("data") or {}
        cost = data.get("spot_cost_analysis") or {}
        value = cost.get("known_purchase_avg_cost")
        if value is None:
            return answer
        shown = f"{float(value):.12g}"
        pattern = re.compile(
            r"(?m)^([ \t]*(?:[-*•][ \t]*)?(?:\*{0,2})阶段买入现货(?:到成本|持仓)?均价"
            r"(?:\*{0,2})(?:（[^\n）]*）)?\s*(?:[:：=]|是|为)\s*)[-+]?\d[\d,]*(?:\.\d+)?"
        )
        return pattern.sub(lambda match: f"{match.group(1)}{shown}", answer)

    @staticmethod
    def _output_text(response: dict) -> str:
        if response.get("output_text"):
            return str(response["output_text"])
        chunks = []
        for item in response.get("output", []):
            if item.get("type") == "message":
                for content in item.get("content", []):
                    if content.get("type") == "output_text":
                        chunks.append(content.get("text", ""))
        return "\n".join(chunks).strip()

    def _template_context(self, project_id: str, template: dict, segment_ids: list[str]) -> dict:
        source = template["data_source"]
        if source == "CURRENT":
            data = self.projects.engine(project_id).aggregate()
            data["health"] = self.projects.engine(project_id).health()
            if not template.get("include_accounts"):
                data.get("spot", {}).pop("accounts", None)
                data.get("contracts", {}).pop("accounts", None)
            return {"type": "current_snapshot", "data": data}

        tasks = self.detections.list_tasks(project_id)
        project_segments = {
            segment["segment_id"]: (task, segment)
            for task in tasks for segment in task.get("segments", [])
        }
        if source == "LATEST_SEGMENT":
            selected = next(
                ((task, segment) for task in tasks for segment in reversed(task.get("segments", [])) if segment.get("report")),
                None,
            )
            if not selected:
                raise RuntimeError("当前项目还没有已停止的检测时段")
            task, segment = selected
            report = json.loads(json.dumps(segment["report"], ensure_ascii=False))
            if not template.get("include_accounts"):
                report.pop("account_changes", None)
            if not template.get("include_events"):
                report.pop("events", None)
            return {"type": "latest_detection_segment", "task_name": task["name"], "data": report}

        if source == "SELECTED_SEGMENTS":
            if not segment_ids:
                raise RuntimeError("请先在检测列表中勾选要汇报的时段")
            unknown = [segment_id for segment_id in segment_ids if segment_id not in project_segments]
            if unknown:
                raise RuntimeError("所选检测时段不属于当前项目")
            combined = self.detections.combine(segment_ids)
            if not template.get("include_accounts"):
                combined.pop("account_changes", None)
            if template.get("include_events"):
                combined["events"] = [
                    event
                    for segment_id in segment_ids
                    for event in (project_segments[segment_id][1].get("report") or {}).get("events", [])
                ]
            combined["segments"] = [
                {
                    "segment_id": segment_id,
                    "task_name": project_segments[segment_id][0]["name"],
                    "ordinal": project_segments[segment_id][1]["ordinal"],
                    "started_at": project_segments[segment_id][1]["started_at"],
                    "ended_at": project_segments[segment_id][1]["ended_at"],
                    "has_gap": project_segments[segment_id][1]["has_gap"],
                }
                for segment_id in segment_ids
            ]
            return {"type": "combined_detection_segments", "data": combined}
        raise RuntimeError("不支持的模板数据范围")

    def _ask_with_template(self, question: str, project_id: str, template: dict, segment_ids: list[str]) -> dict:
        project = self.projects.projects[project_id]
        context = self._template_context(project_id, template, segment_ids)
        sheet_metrics = self._sheet_metric_context(project_id, f"{template['instructions']}\n{question}")
        instructions = (
            "你是做市监控汇报生成器。后端已经完成所有数据查询和计算，禁止自行补数、重新计算或改变口径。"
            "只能使用输入 JSON 中的事实。has_gap=true 或 health.has_gap=true 时必须明确警告数据不完整。"
            "字段定义是强制口径，不得自行解释或改名。展示时使用模板中的中文名称和合适单位，禁止显示 JSON 字段路径。"
            "只输出模板明确点名的指标，不要补充未要求的数字。"
            "严格遵循用户保存的输出要求，不要输出思考过程、数据工具说明或额外建议。"
        )
        prompt = {
            "project_id": project_id,
            "project_name": project["name"],
            "template_name": template["name"],
            "saved_output_requirements": template["instructions"],
            "additional_user_request": question.strip() or "无，完全按保存模板输出",
            "mandatory_metric_definitions": {
                "capital.available_funds": "启用的现货与合约账户‘现有资金’之和",
                "spot.total_return": "现货可用资金 + 现货市值 - 现货累计投入；不等于已实现+未实现",
                "contracts.total_return": "合约现有资金 - 合约累计投入 + 合约未实现收益",
                "project.total_return": "现货总收益 + 合约总收益",
                "spot.unrealized": "当前现货持仓收益，不是现货总收益",
                "spot.cost_basis": "现货当前持仓成本，由历史资金和数量变动推导，不是市值",
                "spot.avg_cost": "现货当前持仓成本 / 当前现货数量，不是现货市价",
                "spot.holding_total_avg_cost": "现货持仓总均价；只能使用 APR实时表!G7:H7 的锚点 G7 保存值",
                "spot_cost_analysis.purchases": "各检测阶段根据稳定资金与数量变动推导的买入数量、买入成本和买入均价",
            },
            "verified_sheet_metrics": sheet_metrics,
            "verified_context": context,
        }
        response = self._call_api({
            "model": self.model,
            "instructions": instructions,
            "input": json.dumps(prompt, ensure_ascii=False, separators=(",", ":")),
            # Reasoning tokens share the output budget. Reports use medium reasoning and
            # reserve enough headroom while retaining the user's selected answer length.
            "max_output_tokens": int(template.get("max_output_tokens") or 1000) + 1600,
            "reasoning": {"effort": "medium"},
            "store": False,
        })
        answer = self._enforce_sheet_metrics(self._output_text(response), project_id)
        answer = self._enforce_stage_purchase_avg(answer, context)
        if not answer:
            detail = response.get("incomplete_details") or response.get("status") or "unknown"
            raise RuntimeError(f"OpenAI API 未返回文本答案：{detail}")
        return {
            "answer": answer,
            "provider": "OPENAI_TEMPLATE",
            "model": self.model,
            "template_id": template["template_id"],
            "template_name": template["name"],
            "response_id": response.get("id"),
        }

    def ask(
        self,
        question: str,
        project_id: str,
        template: dict | None = None,
        segment_ids: list[str] | None = None,
    ) -> dict:
        asks_total_avg = any(label in question for label in ("现货持仓总均价", "现货持仓均价")) or (
            "G7" in question.upper() and "H7" in question.upper()
        )
        complex_request = any(word in question for word in (
            "报告", "汇报", "阶段", "买入", "卖出", "时段", "变化",
            "项目总收益", "现货总收益", "合约总收益", "可用资金",
        ))
        if asks_total_avg and not template and not complex_request:
            metric = self._sheet_metric_context(project_id, question)
            value = metric["spot_holding_total_avg_cost"]
            shown = "--" if value is None else f"{float(value):.12g}"
            return {
                "answer": f"现货持仓总均价：{shown}\n数据来源：{metric['source_cell']}",
                "provider": "LOCAL_VERIFIED_CELL",
                "model": None,
            }
        key = self.keys.get()
        if not key:
            fallback = self.projects.engine(project_id).chat(question)
            fallback.update({"provider": "LOCAL_RULES", "warning": "尚未配置 OpenAI API Key"})
            return fallback
        if template:
            return self._ask_with_template(question, project_id, template, segment_ids or [])
        project = self.projects.projects[project_id]
        cost_focus = any(word in question for word in ("现货成本", "持仓成本", "买入成本", "现货均价", "买入均价", "成本均价", "持仓总均价", "总均价"))
        sheet_focus = bool(re.search(r"\b[A-Za-z]{1,3}[1-9][0-9]{0,5}\b", question)) or "现货持仓总均价" in question
        verified_cost = self._spot_cost_context(project_id) if cost_focus else None
        fact_rule = (
            "输入中已附带 verified_spot_cost_context，该数据由本地成本引擎确定性计算，直接使用且不要重复调用成本工具。"
            if cost_focus else "必须先调用数据工具取得事实，再回答。"
        )
        instructions = (
            "你是做市监控数据分析助手。" + fact_rule +
            "涉及多个检测时段时必须调用 combine_detection_segments，禁止自己心算相加。"
            "询问现货持仓成本、成本均价或分阶段买入成本时，必须使用 get_spot_cost_analysis 的确定性结果。"
            "‘现货持仓总均价’或‘现货持仓均价’是同一表格口径，只能取 verified_sheet_metrics 中 APR实时表!G7:H7 的 G7 值，不得输出成本引擎 avg_cost。"
            "现货市价绝对不是持仓成本均价或买入均价，不得用市价代替任何成本。"
            "回答必须写明项目、时间范围、数据完整性；has_gap=true 时明确警告。"
            "不得编造未由工具返回的数字。用户允许查看完整账户名称、备注和事件。"
            "使用简洁准确的中文。"
        )
        payload: dict[str, Any] = {
            "model": self.model,
            "reasoning": {"effort": "low"},
            "instructions": instructions,
            "input": json.dumps({
                "project_id": project_id, "project_name": project["name"], "user_question": question,
                "verified_spot_cost_context": verified_cost,
                "verified_sheet_metrics": self._sheet_metric_context(project_id, question) if sheet_focus else None,
            }, ensure_ascii=False, separators=(",", ":")),
            "tools": TOOLS,
            "tool_choice": "auto",
            "max_output_tokens": 1800,
        }
        response = self._call_api(payload)
        for _ in range(5):
            calls = [x for x in response.get("output", []) if x.get("type") == "function_call"]
            if not calls:
                answer = self._output_text(response)
                if not answer:
                    raise RuntimeError("OpenAI API 未返回文本答案")
                return {"answer": self._enforce_sheet_metrics(answer, project_id), "provider": "OPENAI", "model": self.model, "response_id": response.get("id")}
            outputs = []
            for call in calls:
                try:
                    arguments = json.loads(call.get("arguments") or "{}")
                    result = self._tool(call["name"], arguments)
                    output = json.dumps(result, ensure_ascii=False, separators=(",", ":"))
                except Exception as exc:
                    output = json.dumps({"error": str(exc)}, ensure_ascii=False)
                outputs.append({"type": "function_call_output", "call_id": call["call_id"], "output": output})
            response = self._call_api({
                "model": self.model, "previous_response_id": response["id"], "input": outputs,
                "tools": TOOLS, "instructions": instructions, "max_output_tokens": 1800,
                "reasoning": {"effort": "low"},
            })
        raise RuntimeError("OpenAI 工具调用轮次过多")
