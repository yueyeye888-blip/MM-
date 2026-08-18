from __future__ import annotations

import copy
import json
import sqlite3
import threading
import uuid
from pathlib import Path
from typing import Any

from .core import iso_now, json_dumps, normalize_name, number
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
            CREATE TABLE IF NOT EXISTS detection_report_versions (
              version_id TEXT PRIMARY KEY, segment_id TEXT NOT NULL,
              version_no INTEGER NOT NULL, created_at TEXT NOT NULL,
              source TEXT NOT NULL, note TEXT,
              baseline_json TEXT NOT NULL, final_json TEXT NOT NULL, report_json TEXT NOT NULL,
              is_current INTEGER NOT NULL DEFAULT 1, cascade_from TEXT,
              UNIQUE(segment_id, version_no)
            );
            CREATE INDEX IF NOT EXISTS idx_detection_report_current
              ON detection_report_versions(segment_id, is_current, version_no);
            """
        )
        task_columns = {row["name"] for row in self.conn.execute("PRAGMA table_info(detection_tasks)")}
        if "workflow_version" not in task_columns:
            self.conn.execute("ALTER TABLE detection_tasks ADD COLUMN workflow_version INTEGER NOT NULL DEFAULT 1")
        version_columns = {row["name"] for row in self.conn.execute("PRAGMA table_info(detection_report_versions)")}
        if "baseline_json" not in version_columns:
            self.conn.execute("ALTER TABLE detection_report_versions ADD COLUMN baseline_json TEXT")
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
        self._execute(
            "INSERT INTO detection_tasks(task_id,project_id,name,created_at,ended_at,status,workflow_version) VALUES (?,?,?,?,?,?,2)",
            (task_id, project_id, display, created, None, "OPEN"),
        )
        return self.get_task(task_id)

    def start_segment(self, project_id: str, task_id: str | None = None, task_name: str | None = None) -> dict:
        if task_id is None:
            task_id = self.create_task(project_id, task_name)["task_id"]
        task = self._row("SELECT * FROM detection_tasks WHERE task_id=?", (task_id,))
        if not task or task["project_id"] != project_id:
            raise ValueError("检测任务不存在或不属于当前项目")
        if task["status"] != "OPEN":
            raise ValueError("检测任务已经结束")
        segment_count = self._row("SELECT COUNT(*) n FROM detection_segments WHERE task_id=?", (task_id,))["n"]
        if int(task["workflow_version"] or 1) >= 2 and segment_count:
            raise ValueError("新版检测任务只允许一个检测时段，请新建任务")
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
        self._ensure_original_version(segment_id)
        task = self._row("SELECT workflow_version FROM detection_tasks WHERE task_id=?", (row["task_id"],))
        if task and int(task["workflow_version"] or 1) >= 2:
            self._execute(
                "UPDATE detection_tasks SET status='ENDED',ended_at=? WHERE task_id=?",
                (ended, row["task_id"]),
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

    def _build_report(
        self, segment: dict, baseline: dict, final: dict, ended: str, has_gap: bool, engine,
        *, corrected: bool = False, correction_source: str | None = None,
    ) -> dict:
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
        raw_events = [] if corrected else [dict(x) for x in engine.db.rows(
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
            "metrics": metrics, "spot_cost_analysis": (
                self._spot_cost_analysis_from_snapshots(baseline, final) if corrected else
                self._spot_cost_analysis(engine, baseline, final, segment["started_at"], ended)
            ),
            "account_changes": account_changes, "events": raw_events,
            "corrected": corrected, "correction_source": correction_source,
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

    @staticmethod
    def _spot_cost_analysis_from_snapshots(baseline: dict, final: dict) -> dict:
        before = {normalize_name(x.get("account_name")): x for x in baseline.get("spot", {}).get("accounts", [])}
        purchases, sales = [], []
        for item in final.get("spot", {}).get("accounts", []):
            previous = before.get(normalize_name(item.get("account_name")), {})
            qty_delta = number(item.get("position_qty")) - number(previous.get("position_qty"))
            cost_delta = number(item.get("spot_cost_basis")) - number(previous.get("spot_cost_basis"))
            funds_delta = number(item.get("current_funds")) - number(previous.get("current_funds"))
            capital_delta = number(item.get("cumulative_capital")) - number(previous.get("cumulative_capital"))
            if qty_delta > 1e-9:
                purchase_cost = cost_delta if cost_delta > 1e-9 else None
                purchases.append({
                    "stage": len(purchases) + 1, "captured_at": final.get("as_of"),
                    "account_name": item.get("account_name"), "bought_qty": qty_delta,
                    "purchase_cost": purchase_cost,
                    "purchase_avg_cost": purchase_cost / qty_delta if purchase_cost is not None else None,
                    "funds_change_excluding_added_capital": funds_delta - capital_delta,
                    "quality_status": item.get("quality_status", "OK"),
                })
            elif qty_delta < -1e-9:
                sold_qty = -qty_delta
                released = -cost_delta if cost_delta < -1e-9 else None
                sales.append({
                    "stage": len(sales) + 1, "captured_at": final.get("as_of"),
                    "account_name": item.get("account_name"), "sold_qty": sold_qty,
                    "released_position_cost": released,
                    "released_avg_cost": released / sold_qty if released is not None else None,
                    "funds_change_excluding_added_capital": funds_delta - capital_delta,
                    "quality_status": item.get("quality_status", "OK"),
                })
        known = [item for item in purchases if item["purchase_cost"] is not None]
        known_qty = sum(item["bought_qty"] for item in known)
        known_cost = sum(item["purchase_cost"] for item in known)
        return {
            "definition": "任务纠错后按首尾账户状态重新推导，不使用市价作为成本均价",
            "starting_position_cost": nested_number(baseline, ("spot", "cost_basis")),
            "ending_position_cost": nested_number(final, ("spot", "cost_basis")),
            "position_cost_change": nested_number(final, ("spot", "cost_basis")) - nested_number(baseline, ("spot", "cost_basis")),
            "starting_avg_cost": baseline.get("spot", {}).get("avg_cost"),
            "ending_avg_cost": final.get("spot", {}).get("avg_cost"),
            "ending_position_qty": nested_number(final, ("spot", "qty")),
            "ending_holding_total_avg_cost": final.get("spot", {}).get("holding_total_avg_cost"),
            "known_purchase_qty": known_qty, "known_purchase_cost": known_cost,
            "known_purchase_avg_cost": known_cost / known_qty if known_qty > 1e-9 else None,
            "incomplete_purchase_stages": len(purchases) - len(known),
            "purchases": purchases, "sales": sales,
            "ending_accounts": [
                {"account_name": item.get("account_name"), "position_qty": item.get("position_qty"),
                 "position_cost": item.get("spot_cost_basis"), "avg_cost": item.get("spot_avg_cost"),
                 "quality_status": item.get("quality_status")}
                for item in final.get("spot", {}).get("accounts", [])
            ],
        }

    @staticmethod
    def _recalculate_snapshot(previous: dict, recorded: dict) -> dict:
        """Rebuild all derived account costs in recorded from the previous effective truth."""
        result = copy.deepcopy(recorded)
        prices = result.setdefault("prices", {})
        spot_price = number(prices.get("spot"))
        contract_price = number(prices.get("contract"))
        leverage = number(prices.get("leverage"), 2)
        previous_spots = {normalize_name(x.get("account_name")): x for x in previous.get("spot", {}).get("accounts", [])}
        previous_contracts = {normalize_name(x.get("account_name")): x for x in previous.get("contracts", {}).get("accounts", [])}
        spots = []
        for source in result.get("spot", {}).get("accounts", []):
            item = copy.deepcopy(source)
            prev = previous_spots.get(normalize_name(item.get("account_name")))
            initial, added = number(item.get("initial_capital")), number(item.get("added_capital"))
            item["cumulative_capital"] = initial + added
            item["current_funds"], item["position_qty"] = number(item.get("current_funds")), number(item.get("position_qty"))
            flags = []
            if not prev:
                cost = max(0.0, item["cumulative_capital"] - item["current_funds"])
                realized = 0.0
            else:
                cost, realized = number(prev.get("spot_cost_basis")), number(prev.get("spot_realized_pnl"))
                capital_delta = item["cumulative_capital"] - number(prev.get("cumulative_capital"))
                cash_delta = item["current_funds"] - number(prev.get("current_funds")) - capital_delta
                qty_delta = item["position_qty"] - number(prev.get("position_qty"))
                if qty_delta > 1e-9 and cash_delta < -1e-9:
                    cost += -cash_delta
                elif qty_delta < -1e-9 and cash_delta > 1e-9:
                    sold = min(-qty_delta, number(prev.get("position_qty")))
                    prev_avg = number(prev.get("spot_avg_cost"))
                    realized += cash_delta - prev_avg * sold
                    cost = max(0.0, cost - prev_avg * sold)
                elif abs(qty_delta) <= 1e-9 and abs(cash_delta) <= 1e-9:
                    pass
                elif abs(qty_delta) > 1e-9 and abs(cash_delta) <= 1e-9:
                    flags.append("DATA_INCOMPLETE")
                else:
                    flags.append("DATA_INCONSISTENT")
            qty = item["position_qty"]
            item.update({
                "spot_cost_basis": cost, "spot_avg_cost": cost / qty if qty > 0 else None,
                "spot_realized_pnl": realized, "market_value": qty * spot_price,
                "spot_unrealized_pnl": qty * spot_price - cost,
                "quality_flags": flags, "quality_status": ",".join(flags) or "OK",
            })
            spots.append(item)
        contracts = []
        for source in result.get("contracts", {}).get("accounts", []):
            item = copy.deepcopy(source)
            prev = previous_contracts.get(normalize_name(item.get("account_name")))
            initial, added = number(item.get("initial_capital")), number(item.get("added_capital"))
            item["cumulative_capital"] = initial + added
            item["current_funds"] = number(item.get("current_funds"))
            item["available_funds"] = number(item.get("available_funds"))
            item["position_qty"] = number(item.get("position_qty"))
            item["direction"] = str(item.get("direction") or "").strip()
            qty = item["position_qty"]
            if qty <= 0:
                avg = None
            elif prev and qty < number(prev.get("position_qty")) and item["direction"] == prev.get("direction"):
                avg = prev.get("contract_avg_entry")
            else:
                avg = (item["cumulative_capital"] - item["available_funds"]) * leverage / qty if qty else None
            unrealized = (contract_price - number(avg)) * qty if avg is not None and item["direction"] == "多" else (number(avg) - contract_price) * qty if avg is not None and item["direction"] == "空" else 0.0
            flags = [] if qty <= 0 or item["direction"] in ("多", "空") else ["DATA_INCOMPLETE"]
            item.update({
                "contract_avg_entry": avg,
                "contract_realized_pnl": item["current_funds"] - item["cumulative_capital"],
                "contract_unrealized_pnl": unrealized,
                "liquidation_estimate": avg * (1 - 1 / leverage) if avg is not None and leverage and item["direction"] == "多" else avg * (1 + 1 / leverage) if avg is not None and leverage and item["direction"] == "空" else None,
                "quality_flags": flags, "quality_status": ",".join(flags) or "OK",
            })
            contracts.append(item)
        enabled_spots = [x for x in spots if x.get("status", "启用") == "启用"]
        enabled_contracts = [x for x in contracts if x.get("status", "启用") == "启用"]
        spot_qty = sum(number(x.get("position_qty")) for x in enabled_spots)
        spot_cost = sum(number(x.get("spot_cost_basis")) for x in enabled_spots)
        spot_funds = sum(number(x.get("current_funds")) for x in enabled_spots)
        spot_capital = sum(number(x.get("cumulative_capital")) for x in enabled_spots)
        longs = [x for x in enabled_contracts if x.get("direction") == "多"]
        shorts = [x for x in enabled_contracts if x.get("direction") == "空"]
        long_qty, short_qty = sum(number(x.get("position_qty")) for x in longs), sum(number(x.get("position_qty")) for x in shorts)
        long_weight = sum(number(x.get("position_qty")) * number(x.get("contract_avg_entry")) for x in longs)
        short_weight = sum(number(x.get("position_qty")) * number(x.get("contract_avg_entry")) for x in shorts)
        contract_funds = sum(number(x.get("current_funds")) for x in enabled_contracts)
        contract_realized = sum(number(x.get("contract_realized_pnl")) for x in enabled_contracts)
        contract_unrealized = sum(number(x.get("contract_unrealized_pnl")) for x in enabled_contracts)
        spot_market = spot_qty * spot_price
        spot_total = spot_funds + spot_market - spot_capital
        contract_total = contract_realized + contract_unrealized
        net_qty = long_qty - short_qty
        project_net = spot_qty + net_qty
        k_value = spot_capital - spot_funds - contract_realized + long_weight - short_weight
        result["capital"] = {
            "cumulative": sum(number(x.get("cumulative_capital")) for x in enabled_spots + enabled_contracts),
            "available_funds": spot_funds + contract_funds, "current_funds": spot_funds + contract_funds,
        }
        result["spot"] = {
            **result.get("spot", {}), "qty": spot_qty, "cost_basis": spot_cost,
            "avg_cost": spot_cost / spot_qty if spot_qty else None,
            "available_funds": spot_funds, "market_value": spot_market,
            "realized": sum(number(x.get("spot_realized_pnl")) for x in enabled_spots),
            "unrealized": spot_market - spot_cost, "total_return": spot_total, "accounts": spots,
        }
        result["contracts"] = {
            **result.get("contracts", {}), "long_qty": long_qty,
            "long_avg": long_weight / long_qty if long_qty else None,
            "short_qty": short_qty, "short_avg": short_weight / short_qty if short_qty else None,
            "gross_qty": long_qty + short_qty, "net_qty": net_qty,
            "available_funds": contract_funds, "realized": contract_realized,
            "unrealized": contract_unrealized, "total_return": contract_total, "accounts": contracts,
        }
        result["project"] = {
            "spot_total_return": spot_total, "contract_total_return": contract_total,
            "realized": result["spot"]["realized"] + contract_realized,
            "unrealized": result["spot"]["unrealized"] + contract_unrealized,
            "total_return": spot_total + contract_total, "total_pnl": spot_total + contract_total,
            "net_qty": project_net, "break_even": k_value / project_net if abs(project_net) > 1e-9 else None,
            "break_even_status": "OK" if abs(project_net) > 1e-9 else "NO_SINGLE_BREAK_EVEN",
        }
        result["data_quality"] = sorted({flag for item in spots + contracts for flag in item.get("quality_flags", [])})
        return result

    def _ensure_original_version(self, segment_id: str) -> None:
        if self._row("SELECT 1 FROM detection_report_versions WHERE segment_id=? LIMIT 1", (segment_id,)):
            return
        row = self._row(
            "SELECT baseline_json,final_json,report_json,ended_at FROM detection_segments WHERE segment_id=?",
            (segment_id,),
        )
        if not row or not row["final_json"] or not row["report_json"]:
            return
        self._execute(
            "INSERT INTO detection_report_versions "
            "(version_id,segment_id,version_no,created_at,source,note,baseline_json,final_json,report_json,is_current,cascade_from) "
            "VALUES (?,?,?,?,?,?,?,?,?,1,NULL)",
            (str(uuid.uuid4()), segment_id, 1, row["ended_at"] or iso_now(), "ORIGINAL", "原始检测报告",
             row["baseline_json"], row["final_json"], row["report_json"]),
        )

    def _store_version(
        self, segment_id: str, baseline: dict, final: dict, report: dict,
        source: str, note: str, cascade_from: str | None = None,
    ) -> int:
        self._ensure_original_version(segment_id)
        with self.lock:
            current = self.conn.execute(
                "SELECT COALESCE(MAX(version_no),0) n FROM detection_report_versions WHERE segment_id=?",
                (segment_id,),
            ).fetchone()
            version_no = int(current["n"]) + 1
            self.conn.execute("UPDATE detection_report_versions SET is_current=0 WHERE segment_id=?", (segment_id,))
            self.conn.execute(
                "INSERT INTO detection_report_versions "
                "(version_id,segment_id,version_no,created_at,source,note,baseline_json,final_json,report_json,is_current,cascade_from) "
                "VALUES (?,?,?,?,?,?,?,?,?,1,?)",
                (str(uuid.uuid4()), segment_id, version_no, iso_now(), source, note,
                 json_dumps(baseline), json_dumps(final), json_dumps(report), cascade_from),
            )
            self.conn.commit()
        return version_no

    def _latest_segment_id(self, project_id: str) -> str | None:
        row = self._row(
            "SELECT s.segment_id FROM detection_segments s JOIN detection_tasks t ON t.task_id=s.task_id "
            "WHERE t.project_id=? AND s.status='STOPPED' ORDER BY s.started_at DESC LIMIT 1",
            (project_id,),
        )
        return row["segment_id"] if row else None

    def correction_editor(self, segment_id: str) -> dict:
        segment = self.get_segment(segment_id)
        task = self._row("SELECT workflow_version FROM detection_tasks WHERE task_id=?", (segment["task_id"],))
        if not task or int(task["workflow_version"] or 1) < 2:
            raise ValueError("旧版多时段任务保持只读，不能使用新纠错功能")
        if segment["status"] != "STOPPED" or not segment.get("final"):
            raise ValueError("只能编辑已经停止的检测任务")
        final = segment["final"]
        return {
            "segment_id": segment_id, "task_id": segment["task_id"], "task_name": segment["task_name"],
            "version_no": segment.get("report_version", 1),
            "can_recalibrate_from_workbook": self._latest_segment_id(segment["project_id"]) == segment_id,
            "prices": copy.deepcopy(final.get("prices", {})),
            "spot_accounts": [self._editable_account(x, "spot") for x in final.get("spot", {}).get("accounts", [])],
            "contract_accounts": [self._editable_account(x, "contract") for x in final.get("contracts", {}).get("accounts", [])],
            "versions": segment.get("versions", []),
        }

    def get_report_version(self, segment_id: str, version_no: int) -> dict:
        row = self._row(
            "SELECT * FROM detection_report_versions WHERE segment_id=? AND version_no=?",
            (segment_id, version_no),
        )
        if not row:
            raise ValueError("报告版本不存在")
        result = dict(row)
        for key in ("baseline_json", "final_json", "report_json"):
            result[key.removesuffix("_json")] = json.loads(result.pop(key))
        result["is_current"] = bool(result["is_current"])
        result["report"]["version_no"] = version_no
        result["report"]["version_source"] = result["source"]
        return result

    @staticmethod
    def _editable_account(item: dict, kind: str) -> dict:
        fields = ("account_name", "status", "initial_capital", "added_capital", "current_funds", "position_qty")
        result = {key: item.get(key) for key in fields}
        result["account_type"] = kind
        if kind == "contract":
            result.update({"available_funds": item.get("available_funds"), "direction": item.get("direction")})
        return result

    @staticmethod
    def _apply_editor_changes(final: dict, changes: dict) -> dict:
        result = copy.deepcopy(final)
        prices = changes.get("prices") or {}
        for key in ("spot", "contract", "leverage"):
            if key in prices:
                result.setdefault("prices", {})[key] = number(prices[key])
        allowed = {
            "spot": {"status", "initial_capital", "added_capital", "current_funds", "position_qty"},
            "contract": {"status", "initial_capital", "added_capital", "current_funds", "available_funds", "position_qty", "direction"},
        }
        for kind, payload_key, snapshot_key in (
            ("spot", "spot_accounts", "spot"), ("contract", "contract_accounts", "contracts")
        ):
            target = {normalize_name(x.get("account_name")): x for x in result.get(snapshot_key, {}).get("accounts", [])}
            for edit in changes.get(payload_key) or []:
                name = normalize_name(edit.get("account_name"))
                if name not in target:
                    raise ValueError(f"任务结束快照中不存在账户：{edit.get('account_name')}")
                for field in allowed[kind]:
                    if field in edit:
                        target[name][field] = edit[field] if field in {"status", "direction"} else number(edit[field])
        return result

    def recalibrate_from_workbook(self, segment_id: str, note: str) -> dict:
        segment = self.get_segment(segment_id)
        if self._latest_segment_id(segment["project_id"]) != segment_id:
            raise ValueError("只有该项目最新一次任务可以直接读取当前 Excel 重新校正")
        if not note.strip():
            raise ValueError("请填写本次校正原因")
        current = self.projects.engine(segment["project_id"]).aggregate()
        current["as_of"] = iso_now()
        return self._rebuild_correction_chain(segment_id, current, "CURRENT_WORKBOOK", note.strip())

    def edit_ending_state(self, segment_id: str, changes: dict, note: str) -> dict:
        if not note.strip():
            raise ValueError("请填写本次校正原因")
        segment = self.get_segment(segment_id)
        task = self._row("SELECT workflow_version FROM detection_tasks WHERE task_id=?", (segment["task_id"],))
        if not task or int(task["workflow_version"] or 1) < 2:
            raise ValueError("旧版多时段任务保持只读，不能修改结束状态")
        candidate = self._apply_editor_changes(segment["final"], changes)
        candidate["as_of"] = segment["ended_at"]
        return self._rebuild_correction_chain(segment_id, candidate, "MANUAL_EDIT", note.strip())

    def _rebuild_correction_chain(self, segment_id: str, candidate: dict, source: str, note: str) -> dict:
        target = self.get_segment(segment_id)
        rows = self._rows(
            "SELECT s.segment_id FROM detection_segments s JOIN detection_tasks t ON t.task_id=s.task_id "
            "WHERE t.project_id=? AND t.workflow_version>=2 AND s.status='STOPPED' AND s.started_at>=? "
            "ORDER BY s.started_at,s.ordinal",
            (target["project_id"], target["started_at"]),
        )
        previous_final = None
        for index, row in enumerate(rows):
            current = self.get_segment(row["segment_id"])
            if index == 0:
                effective_baseline = copy.deepcopy(current["baseline"])
                recorded_final = candidate
                version_source, version_note = source, note
            else:
                effective_baseline = self._recalculate_snapshot(previous_final, current["baseline"])
                effective_baseline["as_of"] = current["started_at"]
                recorded_final = current["final"]
                version_source = "CASCADE_RECALC"
                version_note = f"因前序任务 {target['task_name']} 的纠错自动重算"
            effective_final = self._recalculate_snapshot(effective_baseline, recorded_final)
            effective_final["as_of"] = current["ended_at"]
            engine = self.projects.engine(current["project_id"])
            report = self._build_report(
                current, effective_baseline, effective_final, current["ended_at"], current["has_gap"], engine,
                corrected=True, correction_source=version_source,
            )
            version_no = self._store_version(
                current["segment_id"], effective_baseline, effective_final, report,
                version_source, version_note, segment_id if index else None,
            )
            report["version_no"] = version_no
            previous_final = effective_final
        if previous_final is not None:
            engine = self.projects.engine(target["project_id"])
            current_live = engine.aggregate()
            rebuilt_live = self._recalculate_snapshot(previous_final, current_live)
            rebuilt_live["as_of"] = iso_now()
            engine.apply_detection_cost_rebuild(rebuilt_live, segment_id, note)
        return self.get_segment(segment_id)

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
        version_rows = self._rows(
            "SELECT version_no,created_at,source,note,is_current,cascade_from "
            "FROM detection_report_versions WHERE segment_id=? ORDER BY version_no DESC",
            (segment_id,),
        )
        result["versions"] = [dict(row) for row in version_rows]
        current_version = self._row(
            "SELECT * FROM detection_report_versions WHERE segment_id=? AND is_current=1 ORDER BY version_no DESC LIMIT 1",
            (segment_id,),
        )
        if current_version:
            if current_version["baseline_json"]:
                result["baseline"] = json.loads(current_version["baseline_json"])
            result["final"] = json.loads(current_version["final_json"])
            result["report"] = json.loads(current_version["report_json"])
            result["report_version"] = int(current_version["version_no"])
            result["report_version_source"] = current_version["source"]
        else:
            result["report_version"] = 1 if result.get("report") else None
            result["report_version_source"] = "ORIGINAL" if result.get("report") else None
        report = result.get("report")
        if report:
            report["version_no"] = result.get("report_version") or 1
            report["version_source"] = result.get("report_version_source") or "ORIGINAL"
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
