/* APR WPS read-only bridge. It contains no workbook write/save/close calls. */
(function () {
  "use strict";
  const AGENT = "http://127.0.0.1:8765/api/v1";
  const SESSION = `wps-${Date.now()}-${Math.random().toString(16).slice(2)}`;
  const state = {
    started_at: new Date().toISOString(),
    last_snapshot_at: null,
    last_event_at: null,
    last_error: null,
    event_registered: false,
    workbook_name: null
  };
  const FIELD_MAP = {
    spot: {"账户名称":"account_name","状态":"status","初始资金":"initial_capital","累计追加":"added_capital","现有资金":"current_funds","现货数量(APR)":"position_qty","变更类型":"change_type","备注":"note"},
    contract: {"账户名称":"account_name","状态":"status","初始资金":"initial_capital","累计追加":"added_capital","现有资金":"current_funds","剩余可开":"available_funds","方向":"direction","持仓数量(APR)":"position_qty","变更类型":"change_type","备注":"note"}
  };

  function app() { return window.Application || window.wps; }
  function value(sheet, row, col) {
    const cell = sheet.Cells.Item(row, col);
    const raw = cell.Value2;
    return raw === undefined ? cell.Value : raw;
  }
  function findHeader(sheet, startRow) {
    const max = Math.max(startRow + 80, Number(sheet.UsedRange.Rows.Count || 100));
    for (let row = startRow; row <= max; row += 1) {
      if (String(value(sheet, row, 1) || "").trim() === "账户名称") return row;
    }
    throw new Error("未找到账户表头");
  }
  function readBlock(sheet, headerRow, kind) {
    const map = FIELD_MAP[kind], cols = {};
    for (let col = 1; col <= 32; col += 1) {
      const label = String(value(sheet, headerRow, col) || "").trim();
      if (map[label]) cols[map[label]] = col;
      if (!cols.position_qty && kind === "spot" && label.indexOf("现货数量(") === 0) cols.position_qty = col;
      if (!cols.position_qty && kind === "contract" && label.indexOf("持仓数量(") === 0) cols.position_qty = col;
    }
    const rows = [];
    for (let row = headerRow + 1; row <= headerRow + 200; row += 1) {
      const name = String(value(sheet, row, cols.account_name) || "").trim();
      if (name === "现货合计" || name === "合约合计") break;
      if (!name) continue;
      const item = {account_type: kind, row};
      Object.keys(cols).forEach(key => { item[key] = value(sheet, row, cols[key]); });
      rows.push(item);
    }
    return rows;
  }
  function sheetNames(workbook) {
    const names = [];
    for (let i = 1; i <= Number(workbook.Worksheets.Count || 0); i += 1) {
      try { names.push(String(workbook.Worksheets.Item(i).Name)); } catch (_) {}
    }
    return names;
  }
  function workbooks() {
    const books = [], collection = app().Workbooks;
    for (let i = 1; i <= Number(collection.Count || 0); i += 1) {
      try { books.push(collection.Item(i)); } catch (_) {}
    }
    return books;
  }
  function readSnapshot(workbook) {
    const sheet = workbook.Worksheets.Item("APR实时表");
    const spotHeader = findHeader(sheet, 10);
    const contractHeader = findHeader(sheet, spotHeader + 2);
    return {
      project: value(sheet, 4, 1) || "APR",
      spot_price: Number(value(sheet, 4, 4) || 0),
      contract_price: Number(value(sheet, 4, 7) || 0),
      leverage: Number(value(sheet, 4, 10) || 2),
      spots: readBlock(sheet, spotHeader, "spot"),
      contracts: readBlock(sheet, contractHeader, "contract"),
      workbook_name: workbook.Name,
      workbook_path: workbook.FullName || null,
      sheet_name: sheet.Name,
      bridge_session_id: SESSION,
      bridge_telemetry: Object.assign({}, state, {workbook_name: workbook.Name})
    };
  }
  async function post(path, body) {
    const response = await fetch(`${AGENT}${path}`, {method:"POST", headers:{"Content-Type":"application/json"}, body:JSON.stringify(body)});
    if (!response.ok) throw new Error(`Agent HTTP ${response.status}`);
  }
  async function reconcile(trigger) {
    if (trigger === "SheetChange") state.last_event_at = new Date().toISOString();
    const errors = [];
    for (const workbook of workbooks()) {
      if (sheetNames(workbook).indexOf("APR实时表") < 0) continue;
      try {
        const snapshot = readSnapshot(workbook);
        state.workbook_name = snapshot.workbook_name;
        snapshot.bridge_telemetry = Object.assign({}, state, {workbook_name: workbook.Name});
        await post("/bridge/snapshot", snapshot);
        state.last_snapshot_at = new Date().toISOString();
      } catch (error) {
        errors.push(`${workbook.Name}: ${String(error)}`);
      }
    }
    state.last_error = errors.length ? errors.join(" | ") : null;
    catalog();
    heartbeat();
  }
  async function catalog() {
    const items = workbooks().map(workbook => ({
      name: String(workbook.Name || ""),
      full_name: String(workbook.FullName || ""),
      sheets: sheetNames(workbook),
      compatible: sheetNames(workbook).indexOf("APR实时表") >= 0
    }));
    try { await post("/bridge/catalog", {bridge_session_id: SESSION, workbooks: items}); } catch (_) {}
  }
  async function heartbeat() {
    try { await post("/bridge/heartbeat", Object.assign({bridge_session_id:SESSION}, state)); }
    catch (_) { /* Agent failure must never interrupt spreadsheet editing. */ }
  }
  function start() {
    try {
      const application = app();
      application.ApiEvent.AddApiEventListener("SheetChange", function () { reconcile("SheetChange"); });
      state.event_registered = true;
    } catch (error) {
      state.last_error = `SheetChange registration failed: ${String(error && (error.stack || error.message) || error)}`;
    }
    catalog(); reconcile("startup"); heartbeat();
    window.setInterval(heartbeat, 5000);
    window.setInterval(function () { reconcile("timer"); }, 15000);
  }
  window.APRReadOnlyBridge = {start, reconcile, readSnapshot, workbooks, catalog, state};
  window.setTimeout(start, 1000);
})();
