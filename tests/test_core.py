import json
import tempfile
import unittest
from pathlib import Path

from app.core import MonitorEngine, Settings, WorkbookParser


SOURCE = Path("/Users/xingxiu/Desktop/APR_做市实时表_V3_美化版.xlsx")


def snapshot(spot_funds=0, spot_qty=100, contract_qty=0, available=1000, direction=""):
    return {"project":"APR", "spot_price":2, "contract_price":2.2, "leverage":2,
            "workbook_name":"test.xlsx", "sheet_name":"APR实时表",
            "spots":[{"account_type":"spot","account_name":"A","status":"启用","initial_capital":1000,"added_capital":0,"current_funds":spot_funds,"position_qty":spot_qty,"change_type":"正常"}],
            "contracts":[{"account_type":"contract","account_name":"C","status":"启用","initial_capital":1000,"added_capital":0,"current_funds":1000,"available_funds":available,"direction":direction,"position_qty":contract_qty,"change_type":"正常"}]}


class MonitorTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        cfg = {"source_workbook_path":str(SOURCE),"sheet_name":"APR实时表","backup_dir":"backups","database_path":"data/test.db","host":"127.0.0.1","port":8765,"debounce_ms":0,"reconcile_interval_sec":15,"heartbeat_interval_sec":5,"offline_threshold_sec":15,"correction_session_gap_sec":60,"delta_tolerances_sec":{"1m":10,"5m":30,"10m":60},"leverage":2,"backup_retention_days":None,"anomaly_thresholds":{}}
        path=root/"config.json";path.write_text(json.dumps(cfg),encoding="utf-8")
        self.settings=Settings.load(path)
        self.engine=MonitorEngine(self.settings)

    def tearDown(self): self.tmp.cleanup()

    def test_target_workbook_parser(self):
        data=WorkbookParser(SOURCE,"APR实时表").parse()
        self.assertEqual(data["project"],"APR")
        self.assertEqual(len(data["spots"]),17)
        self.assertEqual(len(data["contracts"]),1)
        self.assertEqual(data["spot_price"],0.4)
        cells = WorkbookParser(SOURCE,"APR实时表").read_cells(["G7", "H7"])
        self.assertEqual(data["sheet_spot_total_avg_cost"], cells[0]["value"])
        self.assertEqual(cells[1]["resolved_address"], "G7")
        self.assertEqual(cells[1]["value"], cells[0]["value"])

    def test_spot_buy_and_sell_cost_engine(self):
        self.engine.observe(snapshot(spot_funds=800,spot_qty=100),"TEST",immediate=True)
        self.engine.observe(snapshot(spot_funds=600,spot_qty=200),"TEST",immediate=True)
        spot=self.engine.aggregate()["spot"]
        self.assertAlmostEqual(spot["cost_basis"],400)
        self.assertAlmostEqual(spot["avg_cost"],2)
        self.engine.observe(snapshot(spot_funds=900,spot_qty=100),"TEST",immediate=True)
        spot=self.engine.aggregate()["spot"]
        self.assertAlmostEqual(spot["cost_basis"],200)
        self.assertAlmostEqual(spot["realized"],100)

    def test_contract_open_and_reduce_keeps_average(self):
        self.engine.observe(snapshot(contract_qty=0,available=1000),"TEST",immediate=True)
        self.engine.observe(snapshot(contract_qty=100,available=800,direction="多"),"TEST",immediate=True)
        avg=self.engine.aggregate()["contracts"]["long_avg"]
        self.assertAlmostEqual(avg,4)
        self.engine.observe(snapshot(contract_qty=50,available=900,direction="多"),"TEST",immediate=True)
        self.assertAlmostEqual(self.engine.aggregate()["contracts"]["long_avg"],4)

    def test_restart_restores_historical_cost_basis(self):
        self.engine.observe(snapshot(spot_funds=800,spot_qty=100),"TEST",immediate=True)
        self.engine.observe(snapshot(spot_funds=600,spot_qty=200),"TEST",immediate=True)
        restored=MonitorEngine(self.settings)
        self.assertTrue(restored._restore_from_database())
        self.assertAlmostEqual(restored.aggregate()["spot"]["cost_basis"],400)

    def test_available_funds_and_total_returns(self):
        self.engine.observe(snapshot(spot_funds=800,spot_qty=100),"TEST",immediate=True)
        data=self.engine.aggregate()
        self.assertAlmostEqual(data["capital"]["available_funds"],1800)
        self.assertAlmostEqual(data["spot"]["available_funds"],800)
        self.assertAlmostEqual(data["contracts"]["available_funds"],1000)
        self.assertAlmostEqual(data["spot"]["total_return"],0)
        self.assertAlmostEqual(data["contracts"]["total_return"],0)
        self.assertAlmostEqual(data["project"]["total_return"],0)

        changed=snapshot(spot_funds=800,spot_qty=100,contract_qty=100,available=800,direction="多")
        changed["spot_price"]=2.5
        self.engine.observe(changed,"TEST",immediate=True)
        data=self.engine.aggregate()
        self.assertAlmostEqual(data["spot"]["total_return"],50)
        self.assertAlmostEqual(data["contracts"]["total_return"],-180)
        self.assertAlmostEqual(data["project"]["total_return"],-130)

    def test_bridge_telemetry_resets_between_sessions(self):
        self.engine.bridge_heartbeat("one", {"last_event_at":"event-1", "event_registered":True})
        self.assertEqual(self.engine.bridge_status()["last_event_at"], "event-1")
        self.engine.bridge_heartbeat("two", {"last_event_at":None, "event_registered":True})
        status = self.engine.bridge_status()
        self.assertEqual(status["session_id"], "two")
        self.assertIsNone(status["last_event_at"])


if __name__ == "__main__": unittest.main()
