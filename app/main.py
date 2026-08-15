from __future__ import annotations

from contextlib import asynccontextmanager
import json
from pathlib import Path
from typing import Optional
import uuid

from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from .chat_templates import ChatTemplateStore
from .core import Settings, iso_now
from .detection import DetectionService
from .gpt_client import DataAssistant
from .manager import ProjectManager


ROOT = Path(__file__).resolve().parent.parent
settings = Settings.load(ROOT / "config.json")
projects = ProjectManager(ROOT, settings)
detections = DetectionService(ROOT / "data" / "control.db", projects)
assistant = DataAssistant(projects, detections, settings.data.get("openai_model", "gpt-5-mini"))
chat_templates = ChatTemplateStore(ROOT / "data" / "chat_templates.json")


@asynccontextmanager
async def lifespan(_: FastAPI):
    projects.start()
    yield
    projects.stop()


app = FastAPI(title="做市表格实时监控系统", version="3.2.0", lifespan=lifespan)
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["GET", "POST"], allow_headers=["Content-Type"])
app.mount("/static", StaticFiles(directory=ROOT / "static"), name="static")


class ChatRequest(BaseModel):
    question: str = ""
    project_id: Optional[str] = None
    template_id: Optional[str] = None
    segment_ids: list[str] = Field(default_factory=list)


class ChatTemplateInput(BaseModel):
    name: str
    description: str = ""
    data_source: str = "CURRENT"
    instructions: str
    include_accounts: bool = False
    include_events: bool = False
    max_output_tokens: int = 1000


class ProjectRegistration(BaseModel):
    workbook_path: str
    name: Optional[str] = None
    sheet_name: str = "APR实时表"


class ProjectSelection(BaseModel):
    project_id: str


class BridgeSnapshot(BaseModel):
    project: str = "APR"
    spot_price: float = 0
    contract_price: float = 0
    leverage: float = 2
    spots: list[dict] = Field(default_factory=list)
    contracts: list[dict] = Field(default_factory=list)
    workbook_name: Optional[str] = None
    workbook_path: Optional[str] = None
    sheet_name: Optional[str] = None
    bridge_session_id: Optional[str] = None
    bridge_telemetry: dict = Field(default_factory=dict)


class BridgeHeartbeat(BaseModel):
    bridge_session_id: Optional[str] = None
    started_at: Optional[str] = None
    last_snapshot_at: Optional[str] = None
    last_event_at: Optional[str] = None
    last_error: Optional[str] = None
    event_registered: bool = False
    workbook_name: Optional[str] = None


class BridgeCatalog(BaseModel):
    bridge_session_id: Optional[str] = None
    workbooks: list[dict] = Field(default_factory=list)


class TaskCreate(BaseModel):
    project_id: str
    name: Optional[str] = None


class SegmentStart(BaseModel):
    project_id: str
    task_id: Optional[str] = None
    task_name: Optional[str] = None


class SegmentCombine(BaseModel):
    segment_ids: list[str]


class ApiKeyInput(BaseModel):
    api_key: str


def get_engine(project_id: str | None):
    try:
        return projects.engine(project_id)
    except KeyError as exc:
        raise HTTPException(404, str(exc)) from exc


@app.get("/", include_in_schema=False)
def dashboard():
    return FileResponse(ROOT / "static" / "index.html")


@app.get("/api/v1/projects")
def list_projects():
    return {"active_project_id": projects.active_project_id, "items": projects.list_projects()}


@app.post("/api/v1/projects")
def register_project(body: ProjectRegistration):
    try:
        return projects.register(body.workbook_path, body.name, body.sheet_name)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc


@app.post("/api/v1/projects/select")
def select_project(body: ProjectSelection):
    try:
        return projects.select(body.project_id)
    except KeyError as exc:
        raise HTTPException(404, str(exc)) from exc


@app.post("/api/v1/projects/{project_id}/remove")
def remove_project(project_id: str):
    if detections.running(project_id):
        raise HTTPException(409, "该项目正在检测，请先停止当前时段再移除")
    try:
        return projects.remove(project_id)
    except KeyError as exc:
        raise HTTPException(404, str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc


@app.get("/api/v1/projects/open-workbooks")
def open_workbooks():
    return {"items": projects.open_workbooks()}


@app.get("/api/v1/files")
def browse_files(directory: Optional[str] = None):
    try:
        return projects.browse(directory)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc


@app.get("/api/v1/summary/current")
def current_summary(project_id: Optional[str] = None):
    engine = get_engine(project_id)
    result = engine.aggregate()
    result["health"] = engine.health()
    result["project_id"] = project_id or projects.active_project_id
    return result


@app.get("/api/v1/summary/delta")
def summary_delta(window: str = Query(pattern="^(1m|5m|10m)$"), project_id: Optional[str] = None):
    try:
        return get_engine(project_id).delta(window)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc


@app.get("/api/v1/spot/current")
def spot_current(project_id: Optional[str] = None):
    data = get_engine(project_id).aggregate()
    return {"as_of": data["as_of"], "price": data["prices"]["spot"], **data["spot"], "data_quality": data["data_quality"]}


@app.get("/api/v1/contracts/current")
def contracts_current(project_id: Optional[str] = None):
    data = get_engine(project_id).aggregate()
    return {"as_of": data["as_of"], "price": data["prices"]["contract"], "leverage": data["prices"]["leverage"], **data["contracts"], "data_quality": data["data_quality"]}


@app.get("/api/v1/funds/current")
def funds_current(project_id: Optional[str] = None):
    data = get_engine(project_id).aggregate()
    accounts = [{k: x.get(k) for k in ("account_type", "account_name", "initial_capital", "added_capital", "cumulative_capital", "current_funds", "status")}
                for x in data["spot"]["accounts"] + data["contracts"]["accounts"]]
    return {"as_of": data["as_of"], **data["capital"], "accounts": accounts,
            "definition": "available_funds = 启用现货与合约账户的‘现有资金’合计"}


@app.get("/api/v1/breakeven/current")
def breakeven_current(project_id: Optional[str] = None):
    data = get_engine(project_id).aggregate()
    return {"as_of": data["as_of"], "spot_avg": data["spot"]["avg_cost"], "long_avg": data["contracts"]["long_avg"],
            "short_avg": data["contracts"]["short_avg"], "project_break_even": data["project"]["break_even"],
            "status": data["project"]["break_even_status"]}


@app.get("/api/v1/health")
def health(project_id: Optional[str] = None):
    return get_engine(project_id).health()


@app.get("/api/v1/history")
def history(from_: Optional[str] = Query(None, alias="from"), to: Optional[str] = None, limit: int = 200, project_id: Optional[str] = None):
    return {"items": get_engine(project_id).history(from_, to, limit)}


@app.post("/api/v1/chat")
def chat(body: ChatRequest):
    project_id = body.project_id or projects.active_project_id
    if not project_id:
        raise HTTPException(400, "尚未选择项目")
    try:
        template = chat_templates.get(body.template_id) if body.template_id else None
        result = assistant.ask(body.question, project_id, template, body.segment_ids)
        projects.engine(project_id).db.execute(
            "INSERT INTO chat_interactions VALUES (?,?,?,?,?,?,?,?)",
            (
                str(uuid.uuid4()), iso_now(), body.question, result.get("answer", ""),
                result.get("provider"), result.get("model"), body.template_id,
                json.dumps(body.segment_ids, ensure_ascii=False),
            ),
        )
        return result
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(502, str(exc)) from exc


@app.get("/api/v1/chat/history")
def chat_history(project_id: Optional[str] = None, limit: Optional[int] = None):
    engine = get_engine(project_id)
    bounded = min(max(limit, 1), 50000) if limit is not None else None
    suffix = " LIMIT ?" if bounded is not None else ""
    params = (bounded,) if bounded is not None else ()
    rows = engine.db.rows("SELECT * FROM chat_interactions ORDER BY captured_at DESC" + suffix, params)
    items = [{**dict(row), "legacy": False} for row in rows]
    legacy = engine.db.rows(
        "SELECT q.captured_at,q.question FROM chat_queries q "
        "WHERE NOT EXISTS (SELECT 1 FROM chat_interactions i WHERE i.question=q.question "
        "AND ABS((julianday(i.captured_at)-julianday(q.captured_at))*86400)<5) "
        "ORDER BY q.captured_at DESC" + suffix,
        params,
    )
    items.extend({
        "interaction_id": None, "captured_at": row["captured_at"],
        "question": row["question"], "answer": "旧版本只保存了问题，当时的回答内容无法恢复。",
        "provider": "LEGACY", "model": None, "template_id": None,
        "segment_ids": "[]", "legacy": True,
    } for row in legacy)
    items.sort(key=lambda item: item["captured_at"], reverse=True)
    visible = items[:bounded] if bounded is not None else items
    return {"items": visible, "count": len(visible)}


@app.get("/api/v1/chat/templates")
def list_chat_templates():
    return {"items": chat_templates.list()}


@app.post("/api/v1/chat/templates")
def create_chat_template(body: ChatTemplateInput):
    try:
        return chat_templates.save(body.model_dump())
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc


@app.post("/api/v1/chat/templates/{template_id}")
def update_chat_template(template_id: str, body: ChatTemplateInput):
    try:
        return chat_templates.save(body.model_dump(), template_id)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc


@app.post("/api/v1/chat/templates/{template_id}/delete")
def delete_chat_template(template_id: str):
    try:
        chat_templates.delete(template_id)
        return {"ok": True}
    except ValueError as exc:
        raise HTTPException(404, str(exc)) from exc


@app.get("/api/v1/openai/status")
def openai_status():
    return assistant.status()


@app.post("/api/v1/openai/key")
def set_openai_key(body: ApiKeyInput):
    try:
        return assistant.set_key(body.api_key)
    except (ValueError, RuntimeError) as exc:
        raise HTTPException(400, str(exc)) from exc


@app.get("/api/v1/bridge/status")
def bridge_status(project_id: Optional[str] = None):
    return get_engine(project_id).bridge_status()


@app.post("/api/v1/bridge/catalog")
def bridge_catalog(body: BridgeCatalog):
    projects.update_catalog(body.workbooks)
    return {"ok": True, "registered": len(projects.projects)}


@app.post("/api/v1/bridge/heartbeat")
def bridge_heartbeat(body: BridgeHeartbeat):
    # Backward-compatible heartbeat. Per-project online state is driven by each workbook snapshot.
    return {"ok": True}


@app.post("/api/v1/bridge/snapshot")
def bridge_snapshot(body: BridgeSnapshot):
    payload = body.model_dump()
    session_id = payload.pop("bridge_session_id", None)
    telemetry = payload.pop("bridge_telemetry", {})
    matched = projects.match_snapshot(payload)
    if not matched:
        return {"ok": True, "registered": False, "workbook_name": payload.get("workbook_name")}
    project_id, engine = matched
    engine.observe(payload, "WPS_MEMORY", session_id=session_id)
    engine.bridge_heartbeat(session_id, telemetry)
    return {"ok": True, "registered": True, "project_id": project_id, "debounce_ms": settings.debounce_ms}


@app.get("/api/v1/detections/tasks")
def detection_tasks(project_id: Optional[str] = None):
    target = project_id or projects.active_project_id
    return {"items": detections.list_tasks(target), "running": detections.running(target) if target else None}


@app.post("/api/v1/detections/tasks")
def create_detection_task(body: TaskCreate):
    try:
        return detections.create_task(body.project_id, body.name)
    except (KeyError, ValueError) as exc:
        raise HTTPException(400, str(exc)) from exc


@app.post("/api/v1/detections/segments/start")
def start_detection_segment(body: SegmentStart):
    try:
        return detections.start_segment(body.project_id, body.task_id, body.task_name)
    except (KeyError, ValueError) as exc:
        raise HTTPException(400, str(exc)) from exc


@app.post("/api/v1/detections/segments/{segment_id}/stop")
def stop_detection_segment(segment_id: str):
    try:
        return detections.stop_segment(segment_id)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc


@app.post("/api/v1/detections/tasks/{task_id}/end")
def end_detection_task(task_id: str):
    try:
        return detections.end_task(task_id)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc


@app.get("/api/v1/detections/segments/{segment_id}")
def detection_segment(segment_id: str):
    try:
        return detections.get_segment(segment_id)
    except ValueError as exc:
        raise HTTPException(404, str(exc)) from exc


@app.post("/api/v1/detections/combine")
def combine_detection_segments(body: SegmentCombine):
    try:
        return detections.combine(body.segment_ids)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc


@app.post("/api/v1/admin/backup")
def force_backup(project_id: Optional[str] = None):
    return get_engine(project_id).maybe_backup(force=True)
