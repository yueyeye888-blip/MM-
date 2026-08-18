#!/bin/zsh
set -e
PROJECT_DIR="${0:A:h}"
cd "$PROJECT_DIR"
mkdir -p logs
MONITOR_USER_ID="$(id -u)"

# When the self-healing LaunchAgents are installed, ask launchd to ensure both
# services are alive. Fall back to the original one-shot startup on machines
# that have not completed the automatic-start installation yet.
if launchctl print "gui/$MONITOR_USER_ID/com.mm.monitor.agent" >/dev/null 2>&1; then
  launchctl kickstart "gui/$MONITOR_USER_ID/com.mm.monitor.agent" >/dev/null 2>&1 || true
fi
if launchctl print "gui/$MONITOR_USER_ID/com.mm.monitor.bridge" >/dev/null 2>&1; then
  launchctl kickstart "gui/$MONITOR_USER_ID/com.mm.monitor.bridge" >/dev/null 2>&1 || true
fi

if ! curl -fsS --max-time 1 "http://127.0.0.1:8765/api/v1/health" >/dev/null 2>&1; then
  nohup ./run.sh >> logs/agent.log 2>&1 &
fi

if ! curl -fsS --max-time 1 "http://127.0.0.1:3889/index.html" >/dev/null 2>&1; then
  cd "$PROJECT_DIR/wps-bridge"
  nohup npm run dev >> "$PROJECT_DIR/logs/wps-bridge.log" 2>&1 &
fi

for _ in {1..20}; do
  if curl -fsS --max-time 1 "http://127.0.0.1:8765/api/v1/health" >/dev/null 2>&1 && \
     curl -fsS --max-time 1 "http://127.0.0.1:3889/index.html" >/dev/null 2>&1; then
    open "http://127.0.0.1:8765"
    exit 0
  fi
  sleep 0.5
done

open "$PROJECT_DIR/logs"
echo "做市表格实时监控启动失败，请查看 logs/agent.log 与 logs/wps-bridge.log"
exit 1
