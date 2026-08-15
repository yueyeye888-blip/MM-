from __future__ import annotations

import copy
import json
import re
import threading
import uuid
from pathlib import Path
from typing import Any

from .core import MonitorEngine, Settings, WorkbookParser, iso_now


def project_slug(value: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", value.casefold()).strip("-")
    return slug or f"project-{uuid.uuid4().hex[:8]}"


class ProjectManager:
    """Owns one isolated MonitorEngine per registered workbook project."""

    def __init__(self, root: Path, base_settings: Settings):
        self.root = root
        self.base_settings = base_settings
        self.registry_path = root / "data" / "projects.json"
        self.lock = threading.RLock()
        self.engines: dict[str, MonitorEngine] = {}
        self.catalog: dict[str, dict[str, Any]] = {}
        self.projects: dict[str, dict[str, Any]] = {}
        self.active_project_id: str | None = None
        self._load_registry()

    def _load_registry(self) -> None:
        self.registry_path.parent.mkdir(parents=True, exist_ok=True)
        if self.registry_path.exists():
            payload = json.loads(self.registry_path.read_text(encoding="utf-8"))
            self.projects = {item["project_id"]: item for item in payload.get("projects", [])}
            self.active_project_id = payload.get("active_project_id")
        if not self.projects:
            source = str(self.base_settings.path("source_workbook_path"))
            default = {
                "project_id": "apr",
                "name": "APR",
                "workbook_path": source,
                "workbook_name": Path(source).name,
                "sheet_name": self.base_settings.sheet_name,
                "database_path": str(self.base_settings.path("database_path")),
                "enabled": True,
                "created_at": iso_now(),
            }
            self.projects["apr"] = default
            self.active_project_id = "apr"
            self._save_registry()
        enabled = [project_id for project_id, spec in self.projects.items() if spec.get("enabled", True)]
        if self.active_project_id not in enabled and enabled:
            self.active_project_id = enabled[0]

    def _save_registry(self) -> None:
        payload = {"active_project_id": self.active_project_id, "projects": list(self.projects.values())}
        temp = self.registry_path.with_suffix(".tmp")
        temp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        temp.replace(self.registry_path)

    def _settings_for(self, spec: dict[str, Any]) -> Settings:
        data = copy.deepcopy(self.base_settings.data)
        data["source_workbook_path"] = spec["workbook_path"]
        data["sheet_name"] = spec["sheet_name"]
        data["database_path"] = spec["database_path"]
        data["backup_dir"] = str(self.root / "backups" / spec["project_id"])
        return Settings(self.root, data)

    def start(self) -> None:
        with self.lock:
            for project_id, spec in self.projects.items():
                if spec.get("enabled", True):
                    self._start_engine(project_id)

    def stop(self) -> None:
        with self.lock:
            for engine in self.engines.values():
                engine.stop()

    def _start_engine(self, project_id: str) -> MonitorEngine:
        if project_id in self.engines:
            return self.engines[project_id]
        engine = MonitorEngine(self._settings_for(self.projects[project_id]))
        engine.start()
        self.engines[project_id] = engine
        return engine

    def engine(self, project_id: str | None = None) -> MonitorEngine:
        target = project_id or self.active_project_id
        if not target or target not in self.projects or not self.projects[target].get("enabled", True):
            raise KeyError("项目不存在")
        return self._start_engine(target)

    def register(self, workbook_path: str, name: str | None = None, sheet_name: str = "APR实时表") -> dict:
        path = Path(workbook_path).expanduser().resolve()
        if path.suffix.casefold() not in {".xlsx", ".xlsm"} or not path.is_file():
            raise ValueError("请选择存在的 .xlsx 或 .xlsm 文件")
        # The workbook path is the durable identity. This lets a removed project
        # recover its original database even when it is re-added under a new name.
        existing = next(
            (
                (project_id, spec)
                for project_id, spec in self.projects.items()
                if Path(spec["workbook_path"]).expanduser().resolve() == path
            ),
            None,
        )
        if existing:
            project_id, spec = existing
            if not spec.get("enabled", True):
                with self.lock:
                    spec["enabled"] = True
                    spec["reactivated_at"] = iso_now()
                    self.active_project_id = project_id
                    self._save_registry()
                    self._start_engine(project_id)
            return spec
        parsed = WorkbookParser(path, sheet_name).parse()
        display_name = (name or str(parsed.get("project") or path.stem)).strip()
        base_id = project_slug(display_name)
        project_id = base_id
        counter = 2
        while project_id in self.projects and Path(self.projects[project_id]["workbook_path"]).resolve() != path:
            project_id = f"{base_id}-{counter}"
            counter += 1
        if project_id in self.projects:
            spec = self.projects[project_id]
            if not spec.get("enabled", True):
                with self.lock:
                    spec["enabled"] = True
                    spec["reactivated_at"] = iso_now()
                    self.active_project_id = project_id
                    self._save_registry()
                    self._start_engine(project_id)
            return spec
        spec = {
            "project_id": project_id,
            "name": display_name,
            "workbook_path": str(path),
            "workbook_name": path.name,
            "sheet_name": sheet_name,
            "database_path": str(self.root / "data" / "projects" / f"{project_id}.db"),
            "enabled": True,
            "created_at": iso_now(),
        }
        with self.lock:
            self.projects[project_id] = spec
            self.active_project_id = project_id
            self._save_registry()
            self._start_engine(project_id)
        return spec

    def select(self, project_id: str) -> dict:
        if project_id not in self.projects or not self.projects[project_id].get("enabled", True):
            raise KeyError("项目不存在")
        with self.lock:
            self.active_project_id = project_id
            self._save_registry()
        return self.projects[project_id]

    def list_projects(self) -> list[dict]:
        result = []
        for project_id, spec in self.projects.items():
            if not spec.get("enabled", True):
                continue
            engine = self.engines.get(project_id)
            health = engine.health() if engine else {"status": "STOPPED", "mode": "NOT_STARTED"}
            result.append({**spec, "active": project_id == self.active_project_id, "health": health})
        return result

    def remove(self, project_id: str) -> dict:
        """Stop and hide a project while retaining its database and backups."""
        if project_id not in self.projects or not self.projects[project_id].get("enabled", True):
            raise KeyError("项目不存在")
        enabled = [key for key, spec in self.projects.items() if spec.get("enabled", True)]
        if len(enabled) <= 1:
            raise ValueError("至少需要保留一个监控项目")
        with self.lock:
            engine = self.engines.pop(project_id, None)
            if engine:
                engine.stop()
            spec = self.projects[project_id]
            spec["enabled"] = False
            spec["removed_at"] = iso_now()
            if self.active_project_id == project_id:
                self.active_project_id = next(
                    key for key, item in self.projects.items() if key != project_id and item.get("enabled", True)
                )
            self._save_registry()
        return {
            "removed_project_id": project_id,
            "active_project_id": self.active_project_id,
            "database_preserved": spec["database_path"],
            "backups_preserved": str(self.root / "backups" / project_id),
        }

    def update_catalog(self, workbooks: list[dict]) -> None:
        now = iso_now()
        with self.lock:
            for book in workbooks:
                key = str(book.get("full_name") or book.get("name") or "").casefold()
                if key:
                    self.catalog[key] = {**book, "seen_at": now}

    def open_workbooks(self) -> list[dict]:
        registered_paths = {str(Path(x["workbook_path"]).resolve()).casefold(): pid for pid, x in self.projects.items() if x.get("enabled", True)}
        registered_names = {x["workbook_name"].casefold(): pid for pid, x in self.projects.items() if x.get("enabled", True)}
        result = []
        for item in self.catalog.values():
            full_name = str(item.get("full_name") or "")
            name = str(item.get("name") or "")
            project_id = registered_paths.get(full_name.casefold()) or registered_names.get(name.casefold())
            result.append({**item, "project_id": project_id, "registered": bool(project_id)})
        return sorted(result, key=lambda x: str(x.get("name", "")).casefold())

    def match_snapshot(self, snapshot: dict) -> tuple[str, MonitorEngine] | None:
        full_name = str(snapshot.get("workbook_path") or "").casefold()
        name = str(snapshot.get("workbook_name") or "").casefold()
        for project_id, spec in self.projects.items():
            if not spec.get("enabled", True):
                continue
            if full_name and str(Path(spec["workbook_path"]).resolve()).casefold() == full_name:
                return project_id, self.engine(project_id)
            if name and spec["workbook_name"].casefold() == name:
                return project_id, self.engine(project_id)
        return None

    def browse(self, directory: str | None = None) -> dict:
        desktop = Path.home() / "Desktop"
        base = Path(directory).expanduser().resolve() if directory else desktop.resolve()
        allowed_roots = [desktop.resolve(), self.root.resolve()]
        if not any(base == root or root in base.parents for root in allowed_roots):
            raise ValueError("只能浏览桌面和项目目录")
        if not base.is_dir():
            raise ValueError("目录不存在")
        directories, files = [], []
        for item in sorted(base.iterdir(), key=lambda p: (not p.is_dir(), p.name.casefold())):
            if item.name.startswith("."):
                continue
            if item.is_dir():
                directories.append({"name": item.name, "path": str(item)})
            elif item.suffix.casefold() in {".xlsx", ".xlsm"} and not item.name.startswith("~$"):
                files.append({"name": item.name, "path": str(item), "size": item.stat().st_size})
        parent = None
        if base != desktop and any(base.parent == root or root in base.parent.parents for root in allowed_roots):
            parent = str(base.parent)
        return {"directory": str(base), "parent": parent, "directories": directories, "files": files}
