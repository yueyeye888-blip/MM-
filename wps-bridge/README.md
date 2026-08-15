# APR WPS 只读 Bridge

这个 Bridge 只读取 WPS 当前打开的全部兼容工作簿内存值，通过 `127.0.0.1:8765` 分项目发送快照。代码中没有写单元格、保存或关闭工作簿的接口。

V3 行为：

- 枚举 WPS `Workbooks`，把工作簿名、完整路径、工作表列表上报到项目管理页面；
- 对包含 `APR实时表` 的所有工作簿并行读取，不再只读取 `ActiveWorkbook`；
- `SheetChange` 后立即全量同步，另有 15 秒 reconcile；
- 数量列支持 `现货数量(任意代币)` 与 `持仓数量(任意代币)`；
- 未注册表格只显示在项目管理中，不进入任何项目数据库。

## Mac 安装

本机已经完成依赖安装和加载项注册。注册文件位于 WPS 沙盒：

`~/Library/Containers/com.kingsoft.wpsoffice.mac/Data/.kingsoft/wps/jsaddons/publish.xml`

日常使用只需双击项目根目录的 `启动APR监控.command`。如需重新安装：

```bash
cd "/Users/xingxiu/Desktop/MM表格数据汇报/wps-bridge"
npm install
npx wpsjs debug -p 3889
```

然后完整退出并重新打开 WPS。用下面地址确认：

`http://127.0.0.1:8765/api/v1/bridge/status`

成功标准：`connected=true`、`mode=WPS_MEMORY`、`event_registered=true`、`last_error=null`。

## 已完成的 Phase 0 实测

WPS Mac 7.5.1（build 8994）中，`SheetChange` 已实际触发；测试副本 D4 的未保存值 `0.41` 被 Agent 捕获，同时文件 mtime 与大小保持不变。生产源码不包含写单元格、保存或关闭工作簿的调用。

WPS Mac 客户端对加载项与 `SheetChange` 的支持有版本差异，因此正式使用前必须完成上述 Go/No-Go；未通过时，不能把磁盘轮询称为“未保存实时监控”。
