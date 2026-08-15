from __future__ import annotations

import json
import sqlite3
import threading
import uuid
from pathlib import Path
from typing import Any

from .core import iso_now, json_dumps
from .manager import ProjectManager


REPORT_FIELDS = {
    "总可用资金变化": ("capital", "available_funds"),
    "现货数量变化": ("spot", "qty"),
    "现货可用资金变化": ("spot", "available_funds"),
    "现货市值变化": ("spot", "market_value"),
    "现货持仓收益变化": ("spot", "unrealized"),
    "现货总收益变化": ("spot", "total_return"),
    "合约多单数量变化": ("contracts", "long_qty"),
    "合约空单数量变化": ("contracts", "short_qty"),
    "合约净持仓变化": ("contracts", "net_qty"),
    "合约可用资金变化": ("contracts", "available_funds"),
    "合约已实现变化": ("contracts", "realized"),
    "合约未实现变化": ("contracts", "unrealized"),
    "合约总收益变化": ("contracts", "total_return"),
    "项目总收益变化": ("project", "total_return"),
}


def nested_number(payload: dict, path: tuple[str, str]) -> float:
    try:
        return float(payload[path[0]][path[1]] or 0)
    except (KeyError, TypeError, ValueError):
        return 0.0


def account_map(snapshot: dict) -> dict[tuple[str, str], dict]:
    result = {}
    for kind, key in (("spot", "spot"), ("contract", "contracts")):
        for item in snapshot.get(key, {}).get("accounts", []):
            result[(kind, str(item.get("account_name", "")))] = item
    return result


class DetectionService:
    def __init__(self, path: Path, projects: ProjectManager):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.projects = projects
        self.lock = threading.RLock()
        self.conn = sqlite3.connect(path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(
            """
            PRAGMA journal_mode=WAL;
            CREATE TABLE IF NOT EXISTS detection_tasks (
              task_id TEXT PRIMARY KEY, project_id TEXT NOT NULL, name TEXT NOT NULL,
              created_at TEXT NOT NULL, ended_at TEXT, status TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS detection_segments (
              segment_id TEXT PRIMARY KEY, task_id TEXT NOT NULL, ordinal INTEGER NOT NULL,
              started_at TEXT NOT NULL, ended_at TEXT, status TEXT NOT NULL,
              baseline_json TEXT NOT NULL, final_json TEXT, report_json TEXT,
              has_gap INTEGER NOT NULL DEFAULT 0, gap_details TEXT
            );
            CREATE INDEX IF NOT EXISTS idx_detection_task ON detection_segments(task_id, ordinal);
            """
        )
        self.conn.commit()

    def _row(self, sql: str, params: tuple = ()) -> sqlite3.Row | None:
        with self.lock:
            return self.conn.execute(sql, params).fetchone()

    def _rows(self, sql: str, params: tuple = ()) -> list[sqlite3.Row]:
        with self.lock:
            return list(self.conn.execute(sql, params).fetchall())

    def _execute(self, sql: str, params: tuple = ()) -> None:
        with self.lock:
            self.conn.execute(sql, params)
            self.conn.commit()

    def create_task(self, project_id: str, name: str | None = None) -> dict:
        self.projects.engine(project_id)
        task_id = str(uuid.uuid4())
        created = iso_now()
        display = (name or f"检测任务 {created[5:16].replace('T', ' ')}").strip()
        self._execute("INSERT INTO detection_tasks VALUES (?,?,?,?,?,?)", (task_id, project_id, display, created, None, "OPEN"))
        return self.get_task(task_id)

    def start_segment(self, project_id: str, task_id: str | None = None, task_name: str | None = None) -> dict:
        if task_id is None:
            task_id = self.create_task(project_id, task_name)["task_id"]
        task = self._row("SELECT * FROM detection_tasks WHERE task_id=?", (task_id,))
        if not task or task["project_id"] != project_id:
            raise ValueError("检测任务不存在或不属于当前项目")
        if task["status"] != "OPEN":
            raise ValueError("检测任务已经结束")
        running = self._row("SELECT segment_id FROM detection_segments WHERE task_id=? AND status='RUNNING'", (task_id,))
        if running:
            raise ValueError("该任务已经有正在检测的时段")
        ordinal_row = self._row("SELECT COALESCE(MAX(ordinal),0)+1 n FROM detection_segments WHERE task_id=?", (task_id,))
        baseline = self.projects.engine(project_id).aggregate()
        segment_id = str(uuid.uuid4())
        started = iso_now()
        self._execute(
            "INSERT INTO detection_segments VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (segment_id, task_id, ordinal_row["n"], started, None, "RUNNING", json_dumps(baseline), None, None, 0, None),
        )
        return self.get_segment(segment_id)

    def stop_segment(self, segment_id: str) -> dict:
        row = self._row(
            "SELECT s.*,t.project_id FROM detection_segments s JOIN detection_tasks t ON t.task_id=s.task_id WHERE s.segment_id=?",
            (segment_id,),
        )
        if not row:
            raise ValueError("检测时段不存在")
        if row["status"] != "RUNNING":
            return self.get_segment(segment_id)
        ended = iso_now()
        engine = self.projects.engine(row["project_id"])
        final = engine.aggregate()
        has_gap, gap_details = self._gap_status(engine, row["started_at"], ended)
        report = self._build_report(dict(row), json.loads(row["baseline_json"]), final, ended, has_gap, engine)
        self._execute(
            "UPDATE detection_segments SET ended_at=?,status='STOPPED',final_json=?,report_json=?,has_gap=?,gap_details=? WHERE segment_id=?",
            (ended, json_dumps(final), json_dumps(report), int(has_gap), gap_details, segment_id),
        )
        return self.get_segment(segment_id)

    def end_task(self, task_id: str) -> dict:
        running = self._row("SELECT segment_id FROM detection_segments WHERE task_id=? AND status='RUNNING'", (task_id,))
        if running:
            self.stop_segment(running["segment_id"])
        self._execute("UPDATE detection_tasks SET status='ENDED',ended_at=? WHERE task_id=?", (iso_now(), task_id))
        return self.get_task(task_id)

    def _gap_status(self, engine, started: str, ended: str) -> tuple[bool, str | None]:
        rows = engine.db.rows(
            "SELECT * FROM monitor_health WHERE event_type='DISK_MONITOR_GAP' "
            "AND started_at<=? AND (ended_at IS NULL OR ended_at>=?)",
            (ended, started),
        )
        health = engine.health()
        has_gap = bool(rows) or bool(health["has_gap"])
        details = [dict(x) for x in rows]
        if health["has_gap"]:
            details.append({"event_type": "DISK_MONITOR_NOT_POLLING", "details": health["warnings"]})
        return has_gap, json_dumps(details) if details else None

    def _build_report(self, segment: dict, baseline: dict, final: dict, ended: str, has_gap: bool, engine) -> dict:
        metrics = []
        for label, path in REPORT_FIELDS.items():
            before, after = nested_number(baseline, path), nested_number(final, path)
            metrics.append({"label": label, "start": before, "end": after, "delta": after - before})
        before_accounts, after_accounts = account_map(baseline), account_map(final)
        account_changes = []
        for key in sorted(set(before_accounts) | set(after_accounts)):
            before, after = before_accounts.get(key, {}), after_accounts.get(key, {})
            fields = {}
            for field in ("current_funds", "position_qty", "spot_realized_pnl", "spot_unrealized_pnl", "contract_realized_pnl", "contract_unrealized_pnl"):
                old, new = float(before.get(field) or 0), float(after.get(field) or 0)
                if abs(new - old) > 1e-9:
                    fields[field] = {"start": old, "end": new, "delta": new - old}
            if fields:
                account_changes.append({"account_type": key[0], "account_name": key[1], "changes": fields})
        raw_events = [dict(x) for x in engine.db.rows(
            "SELECT captured_at,account_type,account_name,field_key,old_value,new_value,change_type_cell,source "
            "FROM raw_events WHERE captured_at BETWEEN ? AND ? ORDER BY captured_at",
            (segment["started_at"], ended),
        )]
        project = self.projects.projects[segment["project_id"]]
        return {
            "segment_id": segment["segment_id"], "task_id": segment["task_id"], "ordinal": segment["ordinal"],
            "started_at": segment["started_at"], "ended_at": ended, "has_gap": has_gap,
            "target": {
                "project_id": segment["project_id"], "project_name": project["name"],
                "workbook_name": project["workbook_name"], "workbook_path": project["workbook_path"],
                "sheet_name": project["sheet_name"],
            },
            "metrics": metrics, "spot_cost_analysis": self._spot_cost_analysis(
                engine, baseline, final, segment["started_at"], ended
            ),
            "account_changes": account_changes, "events": raw_events,
        }

    @staticmethod
    def _spot_cost_analysis(engine, baseline: dict, final: dict, started: str, ended: str) -> dict:
        previous = {
            str(item.get("account_name", "")): item
            for item in baseline.get("spot", {}).get("accounts", [])
        }
        purchases, sales = [], []
        rows = engine.db.rows(
            "SELECT state_id,captured_at,payload,quality_status FROM stable_account_states "
            "WHERE account_type='spot' AND is_valid=1 AND captured_at BETWEEN ? AND ? "
            "ORDER BY captured_at,state_id",
            (started, ended),
        )
        for row in rows:
            state = json.loads(row["payload"])
            name = str(state.get("account_name", ""))
            before = previous.get(name)
            previous[name] = state
            if not before:
                continue
            qty_delta = float(state.get("position_qty") or 0) - float(before.get("position_qty") or 0)
            cost_delta = float(state.get("spot_cost_basis") or 0) - float(before.get("spot_cost_basis") or 0)
            funds_delta = float(state.get("current_funds") or 0) - float(before.get("current_funds") or 0)
            capital_delta = float(state.get("cumulative_capital") or 0) - float(before.get("cumulative_capital") or 0)
            if qty_delta > 1e-9:
                purchase_cost = cost_delta if cost_delta > 1e-9 else None
                purchases.append({
                    "stage": len(purchases) + 1, "captured_at": row["captured_at"], "account_name": name,
                    "bought_qty": qty_delta, "purchase_cost": purchase_cost,
                    "purchase_avg_cost": purchase_cost / qty_delta if purchase_cost is not None else None,
                    "funds_change_excluding_added_capital": funds_delta - capital_delta,
                    "quality_status": row["quality_status"],
                })
            elif qty_delta < -1e-9:
                sold_qty = -qty_delta
                released_cost = -cost_delta if cost_delta < -1e-9 else None
                sales.append({
                    "stage": len(sales) + 1, "captured_at": row["captured_at"], "account_name": name,
                    "sold_qty": sold_qty, "released_position_cost": released_cost,
                    "released_avg_cost": released_cost / sold_qty if released_cost is not None else None,
                    "funds_change_excluding_added_capital": funds_delta - capital_delta,
                    "quality_status": row["quality_status"],
                })
        known_purchases = [item for item in purchases if item["purchase_cost"] is not None]
        bought_qty = sum(item["bought_qty"] for item in known_purchases)
        purchase_cost = sum(item["purchase_cost"] for item in known_purchases)
        return {
            "definition": "持仓成本由账户资金与现货数量的稳定变动推导，不使用现货市价作为成本均价",
            "starting_position_cost": nested_number(baseline, ("spot", "cost_basis")),
            "ending_position_cost": nested_number(final, ("spot", "cost_basis")),
            "position_cost_change": nested_number(final, ("spot", "cost_basis")) - nested_number(baseline, ("spot", "cost_basis")),
            "starting_avg_cost": baseline.get("spot", {}).get("avg_cost"),
            "ending_avg_cost": final.get("spot", {}).get("avg_cost"),
            "ending_position_qty": nested_number(final, ("spot", "qty")),
            "ending_holding_total_avg_cost": final.get("spot", {}).get("holding_total_avg_cost"),
            "known_purchase_qty": bought_qty,
            "known_purchase_cost": purchase_cost,
            "known_purchase_avg_cost": purchase_cost / bought_qty if bought_qty > 1e-9 else None,
            "incomplete_purchase_stages": len(purchases) - len(known_purchases),
            "purchases": purchases,
            "sales": sales,
            "ending_accounts": [
                {
                    "account_name": item.get("account_name"), "position_qty": item.get("position_qty"),
                    "position_cost": item.get("spot_cost_basis"), "avg_cost": item.get("spot_avg_cost"),
                    "quality_status": item.get("quality_status"),
                }
                for item in final.get("spot", {}).get("accounts", [])
            ],
        }

    def get_segment(self, segment_id: str) -> dict:
        row = self._row(
            "SELECT s.*,t.project_id,t.name task_name FROM detection_segments s "
            "JOIN detection_tasks t ON t.task_id=s.task_id WHERE s.segment_id=?", (segment_id,)
        )
        if not row:
            raise ValueError("检测时段不存在")
        result = dict(row)
        for key in ("baseline_json", "final_json", "report_json", "gap_details"):
            value = result.pop(key)
            result[key.removesuffix("_json")] = json.loads(value) if value else None
        result["has_gap"] = bool(result["has_gap"])
        report = result.get("report")
        if report:
            old_labels = {
                "总可用资金": "总可用资金变化", "现货数量": "现货数量变化",
                "现货可用资金": "现货可用资金变化", "现货市值": "现货市值变化",
                "现货持仓收益": "现货持仓收益变化", "现货总收益": "现货总收益变化",
                "合约多单数量": "合约多单数量变化", "合约空单数量": "合约空单数量变化",
                "合约净持仓": "合约净持仓变化", "合约可用资金": "合约可用资金变化",
                "合约已实现": "合约已实现变化", "合约未实现": "合约未实现变化",
                "合约总收益": "合约总收益变化", "项目总收益": "项目总收益变化",
            }
            report["metrics"] = [
                {**metric, "label": old_labels.get(metric["label"], metric["label"])}
                for metric in report.get("metrics", []) if metric.get("label") not in {"累计投入", "现货价格", "合约价格"}
            ]
            project = self.projects.projects.get(result["project_id"])
            if project and not report.get("target"):
                report["target"] = {
                    "project_id": result["project_id"], "project_name": project["name"],
                    "workbook_name": project["workbook_name"], "workbook_path": project["workbook_path"],
                    "sheet_name": project["sheet_name"],
                }
            if not report.get("spot_cost_analysis") and result.get("baseline") and result.get("final"):
                try:
                    report["spot_cost_analysis"] = self._spot_cost_analysis(
                        self.projects.engine(result["project_id"]), result["baseline"], result["final"],
                        result["started_at"], result["ended_at"],
                    )
                except KeyError:
                    pass
            cost_analysis = report.get("spot_cost_analysis")
            if cost_analysis is not None and result.get("final"):
                final_spot = result["final"].get("spot", {})
                cost_analysis.setdefault("ending_position_qty", nested_number(result["final"], ("spot", "qty")))
                cost_analysis.setdefault("ending_holding_total_avg_cost", final_spot.get("holding_total_avg_cost"))
        return result

    def get_task(self, task_id: str) -> dict:
        row = self._row("SELECT * FROM detection_tasks WHERE task_id=?", (task_id,))
        if not row:
            raise ValueError("检测任务不存在")
        result = dict(row)
        result["segments"] = [self.get_segment(x["segment_id"]) for x in self._rows(
            "SELECT segment_id FROM detection_segments WHERE task_id=? ORDER BY ordinal", (task_id,)
        )]
        return result

    def list_tasks(self, project_id: str | None = None) -> list[dict]:
        rows = self._rows(
            "SELECT task_id FROM detection_tasks WHERE project_id=? ORDER BY created_at DESC" if project_id else
            "SELECT task_id FROM detection_tasks ORDER BY created_at DESC",
            (project_id,) if project_id else (),
        )
        return [self.get_task(x["task_id"]) for x in rows]

    def running(self, project_id: str) -> dict | None:
        row = self._row(
            "SELECT s.segment_id FROM detection_segments s JOIN detection_tasks t ON t.task_id=s.task_id "
            "WHERE t.project_id=? AND s.status='RUNNING' ORDER BY s.started_at DESC LIMIT 1", (project_id,),
        )
        return self.get_segment(row["segment_id"]) if row else None

    def combine(self, segment_ids: list[str]) -> dict:
        segments = [self.get_segment(segment_id) for segment_id in segment_ids]
        project_ids = {segment["project_id"] for segment in segments}
        if len(project_ids) > 1:
            raise ValueError("不能合并不同项目的检测时段")
        reports = [x["report"] for x in segments if x.get("report")]
        if not reports:
            raise ValueError("所选时段尚无可合并报告")
        metric_totals: dict[str, float] = {}
        accounts: dict[tuple[str, str, str], float] = {}
        for report in reports:
            for metric in report["metrics"]:
                metric_totals[metric["label"]] = metric_totals.get(metric["label"], 0) + float(metric["delta"])
            for account in report["account_changes"]:
                for field, values in account["changes"].items():
                    key = (account["account_type"], account["account_name"], field)
                    accounts[key] = accounts.get(key, 0) + float(values["delta"])
        purchases = [
            {**purchase, "segment_id": report["segment_id"], "segment_ordinal": report["ordinal"]}
            for report in reports for purchase in report.get("spot_cost_analysis", {}).get("purchases", [])
        ]
        known_purchases = [item for item in purchases if item.get("purchase_cost") is not None]
        known_qty = sum(float(item["bought_qty"]) for item in known_purchases)
        known_cost = sum(float(item["purchase_cost"]) for item in known_purchases)
        latest_report = max(reports, key=lambda item: item.get("ended_at") or "")
        latest_cost = latest_report.get("spot_cost_analysis", {})
        return {
            "segment_ids": segment_ids,
            "has_gap": any(x["has_gap"] for x in segments),
            "target": reports[0].get("target"),
            "metrics": [{"label": key, "delta": value} for key, value in metric_totals.items()],
            "spot_cost_analysis": {
                "definition": "各时段买入成本由资金与数量稳定变动推导，不使用市价",
                "position_cost_change": sum(float(report.get("spot_cost_analysis", {}).get("position_cost_change") or 0) for report in reports),
                "ending_position_qty": latest_cost.get("ending_position_qty"),
                "ending_holding_total_avg_cost": latest_cost.get("ending_holding_total_avg_cost"),
                "known_purchase_qty": known_qty, "known_purchase_cost": known_cost,
                "known_purchase_avg_cost": known_cost / known_qty if known_qty > 1e-9 else None,
                "incomplete_purchase_stages": sum(int(report.get("spot_cost_analysis", {}).get("incomplete_purchase_stages") or 0) for report in reports),
                "purchases": purchases,
            },
            "account_changes": [
                {"account_type": key[0], "account_name": key[1], "field": key[2], "delta": value}
                for key, value in accounts.items()
            ],
        }
