const $=id=>document.getElementById(id);
let currentWindow="1m",activeProject=null,activeProjectSpec=null,projectItems=[],currentTasks=[],runningSegment=null,currentDirectory=null,currentTemplates=[],editingTemplateId=null,detectionTasksExpanded=false,editingCorrectionSegmentId=null,editingCorrectionData=null;
const esc=v=>String(v??"").replace(/[&<>"']/g,c=>({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));
const fmt=(v,d=2)=>v===null||v===undefined||Number.isNaN(Number(v))?"--":Number(v).toLocaleString("zh-CN",{minimumFractionDigits:d,maximumFractionDigits:d});
const price=v=>v===null||v===undefined?"--":Number(v).toFixed(6);
const avgPrice=v=>v===null||v===undefined?"--":Number(v).toFixed(5);
const reportNumber=v=>{if(v===null||v===undefined||Number.isNaN(Number(v)))return"--";const n=Math.trunc(Number(v));return(Object.is(n,-0)?0:n).toLocaleString("zh-CN",{maximumFractionDigits:0})};
const reportDelta=v=>`${Math.trunc(Number(v)||0)>=0?'+':''}${reportNumber(v)}`;
const qp=()=>activeProject?`project_id=${encodeURIComponent(activeProject)}`:"";
function pnl(el,v){el.textContent=fmt(v);el.classList.remove("positive","negative");if(v>0)el.classList.add("positive");if(v<0)el.classList.add("negative")}
async function get(url,opts){const r=await fetch(url,opts);if(!r.ok){let message=await r.text();try{message=JSON.parse(message).detail||message}catch(_){}throw new Error(message)}return r.json()}
const post=(url,body)=>get(url,{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify(body)});

async function loadProjects(selectId){
  const data=await get("/api/v1/projects"),select=$("project-select");
  projectItems=data.items;
  activeProject=selectId||activeProject||data.active_project_id;
  select.innerHTML=data.items.map(x=>`<option value="${esc(x.project_id)}" ${x.project_id===activeProject?"selected":""}>${esc(x.name)} · ${esc(x.workbook_name)} · ${esc(x.health.mode)}</option>`).join("");
  const active=data.items.find(x=>x.project_id===activeProject)||data.items[0];
  if(active){activeProject=active.project_id;activeProjectSpec=active;select.value=activeProject;renderDetectionTarget()}
}

function renderDetectionTarget(){
  if(!activeProjectSpec)return;$('detection-target').innerHTML=`<strong>当前检测：${esc(activeProjectSpec.name)} · ${esc(activeProjectSpec.workbook_name)}</strong><small>${esc(activeProjectSpec.workbook_path)} · ${esc(activeProjectSpec.sheet_name)}</small>`;
}

async function selectProject(projectId){
  await post("/api/v1/projects/select",{project_id:projectId});activeProject=projectId;detectionTasksExpanded=false;closeCorrectionEditor();
  await Promise.all([loadProjects(projectId),refresh(),loadDetections(),loadChatHistory()]);
}

async function refresh(){
  if(!activeProject)return;
  try{
    const d=await get(`/api/v1/summary/current?${qp()}`),h=d.health;
    $('as-of').textContent=d.as_of?new Date(d.as_of).toLocaleString('zh-CN',{hour12:false}):'--';
    $('lag').textContent=h.lag_ms===null?'--':`${(h.lag_ms/1000).toFixed(1)} 秒`;
    $('prices').textContent=`${price(d.prices.spot)} / ${price(d.prices.contract)}`;
    $('health-label').textContent=h.status==='ONLINE'?'正常监控':'降级监控';$('health-mode').textContent=h.mode;
    $('health-dot').classList.toggle('online',h.status==='ONLINE');$('capital').textContent=fmt(d.capital.cumulative);$('funds').textContent=fmt(d.capital.available_funds);
    pnl($('spot-total-return'),d.spot.total_return);pnl($('contract-total-return'),d.contracts.total_return);pnl($('total-pnl'),d.project.total_return);
    $('break-even').textContent=price(d.project.break_even);$('spot-qty').textContent=fmt(d.spot.qty,0);$('spot-avg').textContent=avgPrice(d.spot.holding_total_avg_cost);$('spot-avg').title=d.spot.holding_total_avg_cost_source||'APR实时表!G7:H7';
    $('spot-available-funds').textContent=fmt(d.spot.available_funds);$('spot-value').textContent=fmt(d.spot.market_value);pnl($('spot-unrealized'),d.spot.unrealized);pnl($('spot-panel-total-return'),d.spot.total_return);
    $('spot-count').textContent=`${d.spot.accounts.length} 个启用账户`;$('long-pos').textContent=`${fmt(d.contracts.long_qty,0)} / ${avgPrice(d.contracts.long_avg)}`;
    $('short-pos').textContent=`${fmt(d.contracts.short_qty,0)} / ${avgPrice(d.contracts.short_avg)}`;$('gross-net').textContent=`${fmt(d.contracts.gross_qty,0)} / ${fmt(d.contracts.net_qty,0)}`;
    pnl($('contract-realized'),d.contracts.realized);pnl($('contract-unrealized'),d.contracts.unrealized);pnl($('contract-panel-total-return'),d.contracts.total_return);
    $('contract-count').textContent=`${d.contracts.accounts.length} 个启用账户`;
    const w=$('warning');if(h.warnings.length||d.data_quality.length){w.classList.remove('hidden');w.textContent=`数据提示：${[...h.warnings,...d.data_quality].join(' · ')}`}else w.classList.add('hidden');
    renderAccounts(d);await refreshDelta();
  }catch(e){$('health-label').textContent='连接失败';$('health-mode').textContent=e.message}
}

function renderAccounts(d){
  const rows=[];
  for(const x of d.spot.accounts)rows.push(`<tr><td><span class="badge spot">现货</span></td><td>${esc(x.account_name)}</td><td>${fmt(x.current_funds)}</td><td>${fmt(x.position_qty,0)}</td><td>${avgPrice(x.spot_avg_cost)}</td><td class="${x.spot_unrealized_pnl>=0?'positive':'negative'}">${fmt(x.spot_unrealized_pnl)}</td><td class="${x.quality_status==='OK'?'quality-ok':'quality-warn'}">${esc(x.quality_status)}</td></tr>`);
  for(const x of d.contracts.accounts)rows.push(`<tr><td><span class="badge contract">合约${esc(x.direction||'')}</span></td><td>${esc(x.account_name)}</td><td>${fmt(x.current_funds)}</td><td>${fmt(x.position_qty,0)}</td><td>${avgPrice(x.contract_avg_entry)}</td><td class="${x.contract_unrealized_pnl>=0?'positive':'negative'}">${fmt(x.contract_unrealized_pnl)}</td><td class="${x.quality_status==='OK'?'quality-ok':'quality-warn'}">${esc(x.quality_status)}</td></tr>`);
  $('account-rows').innerHTML=rows.join('')||'<tr><td colspan="7">暂无账户</td></tr>';
}

async function refreshDelta(){
  const d=await get(`/api/v1/summary/delta?window=${currentWindow}&${qp()}`),el=$('delta-content');
  if(d.status!=='OK'){el.innerHTML=`<div class="empty">${d.status==='INCOMPLETE_GAP'?'该区间监控不完整，无法可靠计算净变化。':'历史数据不足，待系统累积快照后将自动显示。'}</div>`;return}
  const items=[['总可用资金',d.delta.available_funds,' USDT'],['现货总收益',d.delta.spot_total_return,' USDT'],['合约总收益',d.delta.contract_total_return,' USDT'],['项目总收益',d.delta.total_return,' USDT']];
  el.innerHTML=items.map(([n,v,u])=>`<div class="delta-item"><span>${n}</span><strong class="${v>=0?'positive':'negative'}">${v>=0?'+':''}${fmt(v)}${u}</strong></div>`).join('');
}

async function refreshProjectManager(){
  const books=await get('/api/v1/projects/open-workbooks');
  $('registered-projects').innerHTML=projectItems.map(x=>`<div class="choice-item"><div><strong>${esc(x.name)}${x.project_id===activeProject?' · 当前':''}</strong><small>${esc(x.workbook_name)}<br>${esc(x.workbook_path)}</small></div><button class="project-remove" data-remove-project="${esc(x.project_id)}" ${projectItems.length<=1?'disabled':''}>移除</button></div>`).join('');
  document.querySelectorAll('[data-remove-project]').forEach(b=>b.onclick=()=>removeProject(b.dataset.removeProject));
  $('open-workbooks').innerHTML=books.items.length?books.items.map(x=>`<div class="choice-item"><div><strong>${esc(x.name)}</strong><small>${esc(x.full_name||'未保存工作簿')} · ${x.compatible?'结构兼容':'不含 APR实时表'}</small></div>${x.registered?`<span class="quality-ok">已注册</span>`:(x.compatible&&x.full_name?`<button data-register-open="${esc(x.full_name)}">注册</button>`:'')}</div>`).join(''):'<div class="choice-item">WPS Bridge 尚未上报打开的表格</div>';
  document.querySelectorAll('[data-register-open]').forEach(b=>b.onclick=()=>registerProject(b.dataset.registerOpen));
  await browseFiles(currentDirectory);
}

async function removeProject(projectId){
  const spec=projectItems.find(x=>x.project_id===projectId);if(!spec)return;
  if(!confirm(`确定移除“${spec.name} · ${spec.workbook_name}”吗？\n\n只会停止并移除监控登记，数据库和备份会保留。`))return;
  try{const result=await post(`/api/v1/projects/${projectId}/remove`,{});activeProject=result.active_project_id;detectionTasksExpanded=false;closeCorrectionEditor();await loadProjects(activeProject);await Promise.all([refresh(),loadDetections(),refreshProjectManager()])}catch(e){alert(`移除失败：${e.message}`)}
}

async function browseFiles(directory){
  const data=await get(`/api/v1/files${directory?`?directory=${encodeURIComponent(directory)}`:''}`);currentDirectory=data.directory;
  $('browser-path').textContent=data.directory;$('browse-parent-btn').disabled=!data.parent;$('browse-parent-btn').dataset.parent=data.parent||'';
  const dirs=data.directories.map(x=>`<div class="file-item"><div>📁 ${esc(x.name)}</div><button data-dir="${esc(x.path)}" class="secondary">打开</button></div>`);
  const files=data.files.map(x=>`<div class="file-item"><div>📊 ${esc(x.name)}<small>${esc(x.path)}</small></div><button data-file="${esc(x.path)}">选择</button></div>`);
  $('file-browser').innerHTML=[...dirs,...files].join('')||'<div class="file-item">该目录没有 Excel 表格</div>';
  document.querySelectorAll('[data-dir]').forEach(b=>b.onclick=()=>browseFiles(b.dataset.dir));
  document.querySelectorAll('[data-file]').forEach(b=>b.onclick=()=>{$('project-path-input').value=b.dataset.file});
}

async function registerProject(path){
  try{const spec=await post('/api/v1/projects',{workbook_path:path,sheet_name:'APR实时表'});await loadProjects(spec.project_id);await selectProject(spec.project_id);await refreshProjectManager()}
  catch(e){alert(`注册失败：${e.message}`)}
}

async function loadDetections(){
  if(!activeProject)return;const data=await get(`/api/v1/detections/tasks?${qp()}`);currentTasks=data.items;runningSegment=data.running;
  $('detection-status').textContent=runningSegment?`检测中 · ${esc(runningSegment.task_name||'当前任务')}`:'待命';
  $('start-detection-btn').disabled=!!runningSegment;$('stop-detection-btn').disabled=!runningSegment;
  renderDetectionList();
}

function renderDetectionList(){
  const visibleTasks=detectionTasksExpanded?currentTasks:currentTasks.slice(0,3),hiddenCount=Math.max(0,currentTasks.length-3);
  const stopped=currentTasks.flatMap(t=>t.segments.map(s=>({task:t,segment:s}))).filter(x=>x.segment.status==='STOPPED').sort((a,b)=>new Date(b.segment.started_at)-new Date(a.segment.started_at));
  const latestStoppedId=stopped[0]?.segment.segment_id;
  const cards=visibleTasks.map(task=>{
    const legacy=Number(task.workflow_version||1)<2;
    const segments=task.segments.map(s=>{
      const version=s.report_version||1,canCorrect=!legacy&&s.status==='STOPPED',isLatest=s.segment_id===latestStoppedId;
      const actions=s.report?`<div class="task-actions"><button data-report="${esc(s.segment_id)}" class="secondary">查看报告</button>${canCorrect?`<button data-edit-segment="${esc(s.segment_id)}" class="secondary">编辑结束状态</button>`:''}${canCorrect&&isLatest?`<button data-recalibrate="${esc(s.segment_id)}">读取当前 Excel 校正</button>`:''}</div>`:'';
      return `<div class="segment-row"><input type="checkbox" class="segment-check" value="${esc(s.segment_id)}" ${s.status==='RUNNING'?'disabled':''}><strong>${legacy?`时段 ${s.ordinal}`:'检测任务'}</strong><span>${new Date(s.started_at).toLocaleString('zh-CN',{hour12:false})} — ${s.ended_at?new Date(s.ended_at).toLocaleString('zh-CN',{hour12:false}):'检测中'} ${s.has_gap?'<b class="gap">数据不完整</b>':''}</span>${s.report?`<span class="count">V${version}${s.report_version_source&&s.report_version_source!=='ORIGINAL'?' · 已校正':''}</span>`:''}${actions}<span class="${s.status==='RUNNING'?'quality-ok':'count'}">${s.status==='RUNNING'?'检测中':'已停止'}</span></div>`;
    }).join('');
    return `<div class="task-card"><div class="task-head"><strong>${esc(task.name)}</strong><span class="count">${legacy?'历史任务（只读）':'独立任务'} · ${new Date(task.created_at).toLocaleString('zh-CN',{hour12:false})}</span></div>${segments||'<div class="hint">尚无检测记录</div>'}</div>`;
  }).join('');
  const toggle=hiddenCount?`<button id="detection-list-toggle" class="secondary detection-list-toggle">${detectionTasksExpanded?'收起，仅显示最新 3 个':`展开其余 ${hiddenCount} 个检测任务`}</button>`:'';
  $('detection-list').innerHTML=currentTasks.length?cards+toggle:'暂无检测任务';
  document.querySelectorAll('[data-report]').forEach(b=>b.onclick=async()=>renderReport((await get(`/api/v1/detections/segments/${b.dataset.report}`)).report));
  document.querySelectorAll('[data-edit-segment]').forEach(b=>b.onclick=()=>openCorrectionEditor(b.dataset.editSegment));
  document.querySelectorAll('[data-recalibrate]').forEach(b=>b.onclick=()=>recalibrateSegment(b.dataset.recalibrate));
  const toggleButton=$('detection-list-toggle');if(toggleButton)toggleButton.onclick=()=>{detectionTasksExpanded=!detectionTasksExpanded;renderDetectionList()};
}

async function startDetection(){
  try{await post('/api/v1/detections/segments/start',{project_id:activeProject,task_id:null,task_name:$('task-name').value.trim()||null});$('task-name').value='';await loadDetections()}
  catch(e){alert(`启动失败：${e.message}`)}
}
async function stopDetection(){if(!runningSegment)return;try{const s=await post(`/api/v1/detections/segments/${runningSegment.segment_id}/stop`,{});await loadDetections();renderReport(s.report)}catch(e){alert(`停止失败：${e.message}`)}}
async function combineSegments(){const ids=[...document.querySelectorAll('.segment-check:checked')].map(x=>x.value);if(!ids.length){alert('请先勾选一个或多个已停止任务');return}try{renderReport(await post('/api/v1/detections/combine',{segment_ids:ids}))}catch(e){alert(`合并失败：${e.message}`)}}

const correctionSourceLabel=source=>({ORIGINAL:'原始报告',CURRENT_WORKBOOK:'Excel 重新校正',MANUAL_EDIT:'手动修改',CASCADE_RECALC:'前序任务联动重算'}[source]||source||'未知');
function closeCorrectionEditor(){$('task-correction-editor')?.classList.add('hidden');editingCorrectionSegmentId=null;editingCorrectionData=null}
async function openCorrectionEditor(segmentId){
  try{editingCorrectionData=await get(`/api/v1/detections/segments/${segmentId}/correction-editor`);editingCorrectionSegmentId=segmentId;renderCorrectionEditor(editingCorrectionData)}catch(e){alert(`无法编辑：${e.message}`)}
}
function correctionAccountRows(items,kind){
  return items.map((x,i)=>`<tr data-correction-kind="${kind}" data-correction-index="${i}"><td>${esc(x.account_name)}</td><td><select data-field="status"><option value="启用" ${x.status==='启用'?'selected':''}>启用</option><option value="停用" ${x.status==='停用'?'selected':''}>停用</option></select></td><td><input data-field="initial_capital" type="number" step="any" value="${esc(x.initial_capital??0)}"></td><td><input data-field="added_capital" type="number" step="any" value="${esc(x.added_capital??0)}"></td><td><input data-field="current_funds" type="number" step="any" value="${esc(x.current_funds??0)}"></td>${kind==='contract'?`<td><input data-field="available_funds" type="number" step="any" value="${esc(x.available_funds??0)}"></td><td><select data-field="direction"><option value="多" ${x.direction==='多'?'selected':''}>多</option><option value="空" ${x.direction==='空'?'selected':''}>空</option></select></td>`:''}<td><input data-field="position_qty" type="number" step="any" value="${esc(x.position_qty??0)}"></td></tr>`).join('');
}
function renderCorrectionEditor(data){
  $('correction-title').textContent=`编辑结束状态 · ${data.task_name} · V${data.version_no}`;
  $('correction-spot-price').value=data.prices?.spot??'';$('correction-contract-price').value=data.prices?.contract??'';$('correction-leverage').value=data.prices?.leverage??'';$('correction-note').value='';
  const spot=`<h3>现货账户</h3><table class="correction-table"><thead><tr><th>账户</th><th>状态</th><th>初始投入</th><th>追加投入</th><th>可用资金</th><th>持仓数量</th></tr></thead><tbody>${correctionAccountRows(data.spot_accounts||[],'spot')}</tbody></table>`;
  const contract=`<h3>合约账户</h3><table class="correction-table"><thead><tr><th>账户</th><th>状态</th><th>初始投入</th><th>追加投入</th><th>现有资金</th><th>可用资金</th><th>方向</th><th>持仓数量</th></tr></thead><tbody>${correctionAccountRows(data.contract_accounts||[],'contract')}</tbody></table>`;
  $('correction-accounts').innerHTML=spot+contract;
  $('correction-versions').innerHTML='<strong>报告版本：</strong>'+data.versions.map(v=>`<button class="secondary small" data-version-no="${v.version_no}">V${v.version_no} · ${esc(correctionSourceLabel(v.source))}${v.is_current?' · 当前':''}</button>`).join('');
  document.querySelectorAll('[data-version-no]').forEach(b=>b.onclick=()=>viewReportVersion(editingCorrectionSegmentId,Number(b.dataset.versionNo)));
  $('task-correction-editor').classList.remove('hidden');$('task-correction-editor').scrollIntoView({behavior:'smooth',block:'nearest'});
}
function collectCorrectionRows(kind){return [...document.querySelectorAll(`[data-correction-kind="${kind}"]`)].map(row=>{const original=(kind==='spot'?editingCorrectionData.spot_accounts:editingCorrectionData.contract_accounts)[Number(row.dataset.correctionIndex)],result={account_name:original.account_name};row.querySelectorAll('[data-field]').forEach(input=>result[input.dataset.field]=['status','direction'].includes(input.dataset.field)?input.value:Number(input.value));return result})}
async function saveCorrection(){
  if(!editingCorrectionSegmentId)return;const note=$('correction-note').value.trim();if(!note){alert('请填写本次修改原因');return}
  const body={note,prices:{spot:Number($('correction-spot-price').value),contract:Number($('correction-contract-price').value),leverage:Number($('correction-leverage').value)},spot_accounts:collectCorrectionRows('spot'),contract_accounts:collectCorrectionRows('contract')};
  try{const result=await post(`/api/v1/detections/segments/${editingCorrectionSegmentId}/ending-state`,body);closeCorrectionEditor();await Promise.all([loadDetections(),refresh()]);renderReport(result.report)}catch(e){alert(`保存失败：${e.message}`)}
}
async function recalibrateSegment(segmentId){
  const note=prompt('请填写校正原因。系统将用当前已保存 Excel 作为该任务的正确结束状态，并重算后续成本。');if(!note?.trim())return;
  try{const result=await post(`/api/v1/detections/segments/${segmentId}/recalibrate`,{note:note.trim()});await Promise.all([loadDetections(),refresh()]);renderReport(result.report)}catch(e){alert(`校正失败：${e.message}`)}
}
async function viewReportVersion(segmentId,versionNo){try{const result=await get(`/api/v1/detections/segments/${segmentId}/versions/${versionNo}`);renderReport(result.report)}catch(e){alert(`读取版本失败：${e.message}`)}}
function renderReport(report){
  if(!report)return;const el=$('detection-report');el.classList.remove('hidden');
  const metrics=report.metrics||[],target=report.target,cost=report.spot_cost_analysis||{},purchases=cost.purchases||[];
  const targetHtml=target?`<div class="report-target"><strong>检测对象：${esc(target.project_name)} · ${esc(target.workbook_name)}</strong><small>${esc(target.workbook_path)} · ${esc(target.sheet_name)}</small></div>`:`<div class="report-target"><strong>历史报告：无工作簿标识</strong><small>该报告生成于标识功能上线之前</small></div>`;
  const costCards=[];
  costCards.push(['现货总持仓数量',reportNumber(cost.ending_position_qty)]);
  costCards.push(['持仓成本变化',reportDelta(cost.position_cost_change)],['阶段买入成本',reportNumber(cost.known_purchase_cost)],['阶段买入均价',avgPrice(cost.known_purchase_avg_cost)]);
  const costHtml=report.spot_cost_analysis?`<h3>现货持仓成本（由持仓数量与资金变化推导，非市价）</h3><div class="cost-summary">${costCards.map(x=>`<div><span>${x[0]}</span><strong>${x[1]}</strong></div>`).join('')}</div>${purchases.length?`<table class="cost-table"><thead><tr><th>阶段</th><th>时间</th><th>账户</th><th>买入数量</th><th>买入成本</th><th>买入均价</th></tr></thead><tbody>${purchases.map(x=>`<tr><td>${esc(x.segment_ordinal?`时段${x.segment_ordinal}-${x.stage}`:x.stage)}</td><td>${esc(new Date(x.captured_at).toLocaleString('zh-CN',{hour12:false}))}</td><td>${esc(x.account_name)}</td><td>${reportNumber(x.bought_qty)}</td><td>${reportNumber(x.purchase_cost)}</td><td>${avgPrice(x.purchase_avg_cost)}</td></tr>`).join('')}</tbody></table>`:'<p class="hint">该时段没有检测到可确定推导的现货买入，不会用市价猜测成本。</p>'}`:'';
  const accountChanges=report.account_changes||[];
  const accountHtml=`<details class="report-details"><summary>账户变化（${accountChanges.length}）</summary><pre>${esc(JSON.stringify(accountChanges,null,2))}</pre></details>`;
  const eventsHtml=report.events?`<details class="report-details"><summary>中间变更事件（${report.events.length}）</summary><pre>${esc(JSON.stringify(report.events,null,2))}</pre></details>`:'';
  const versionLabel=report.segment_id&&report.version_no?` · V${report.version_no}${report.version_source&&report.version_source!=='ORIGINAL'?` · ${correctionSourceLabel(report.version_source)}`:''}`:'';
  el.innerHTML=`<div class="panel-title"><div><h2>${report.segment_id?'任务报告':'合并报告'}${versionLabel}</h2></div><span class="${report.has_gap?'gap':'quality-ok'}">${report.has_gap?'⚠ 数据存在监控缺口':'数据完整'}</span></div>${targetHtml}<div class="report-grid">${metrics.map(x=>`<div class="report-metric"><span>${esc(x.label)}</span><strong class="${x.delta>=0?'positive':'negative'}">${reportDelta(x.delta)}</strong>${x.start!==undefined?`<small>${reportNumber(x.start)} → ${reportNumber(x.end)}</small>`:''}</div>`).join('')}</div>${costHtml}${accountHtml}${eventsHtml}`;
  el.scrollIntoView({behavior:'smooth',block:'nearest'});
}

function selectedSegmentIds(){return [...document.querySelectorAll('.segment-check:checked')].map(x=>x.value)}
function activeTemplate(){return currentTemplates.find(x=>x.template_id===$('chat-template-select').value)||currentTemplates[0]}
function updateTemplateDescription(){
  const template=activeTemplate();$('template-description').textContent=template?`${template.description||'无额外说明'} · ${template.data_source==='CURRENT'?'当前快照':template.data_source==='LATEST_SEGMENT'?'最近检测':'勾选时段'}`:'尚无回答模板';
}
async function loadChatTemplates(preferred){
  const data=await get('/api/v1/chat/templates'),select=$('chat-template-select'),before=preferred||select.value;
  currentTemplates=data.items;select.innerHTML=currentTemplates.map(x=>`<option value="${esc(x.template_id)}">${esc(x.name)}</option>`).join('');
  if(before&&currentTemplates.some(x=>x.template_id===before))select.value=before;updateTemplateDescription();$('run-template-btn').disabled=!currentTemplates.length;
}
function fillTemplateEditor(template){
  editingTemplateId=template?.template_id||null;$('template-name').value=template?.name||'';$('template-desc').value=template?.description||'';
  $('template-source').value=template?.data_source||'CURRENT';$('template-instructions').value=template?.instructions||'';
  $('template-accounts').checked=!!template?.include_accounts;$('template-events').checked=!!template?.include_events;
  const tokens=Number(template?.max_output_tokens||1100);$('template-tokens').value=tokens<=700?'600':tokens<=1400?'1100':'1800';$('delete-template-btn').disabled=!editingTemplateId;
}
function openTemplateEditor(){const el=$('template-editor');el.classList.remove('hidden');fillTemplateEditor(activeTemplate())}
function newTemplate(){fillTemplateEditor(null);$('template-name').focus()}
async function saveTemplate(){
  const body={name:$('template-name').value.trim(),description:$('template-desc').value.trim(),data_source:$('template-source').value,instructions:$('template-instructions').value.trim(),include_accounts:$('template-accounts').checked,include_events:$('template-events').checked,max_output_tokens:Number($('template-tokens').value)};
  if(!body.name||!body.instructions){alert('请填写模板名称和输出要求');return}
  try{const saved=await post(editingTemplateId?`/api/v1/chat/templates/${editingTemplateId}`:'/api/v1/chat/templates',body);editingTemplateId=saved.template_id;await loadChatTemplates(saved.template_id);fillTemplateEditor(saved)}catch(e){alert(`保存失败：${e.message}`)}
}
async function deleteTemplate(){
  if(!editingTemplateId||!confirm('确定删除这个回答模板吗？'))return;
  try{await post(`/api/v1/chat/templates/${editingTemplateId}/delete`,{});editingTemplateId=null;await loadChatTemplates();fillTemplateEditor(activeTemplate())}catch(e){alert(`删除失败：${e.message}`)}
}
async function runTemplate(){
  const template=activeTemplate();if(!template)return;
  const ids=selectedSegmentIds();if(template.data_source==='SELECTED_SEGMENTS'&&!ids.length){alert('请先在上方检测列表中勾选至少一个已停止时段');return}
  const button=$('run-template-btn'),old=button.textContent;button.disabled=true;button.textContent='正在精确取数…';
  try{await sendChat($('chat-input').value.trim(),template.template_id,ids)}finally{button.disabled=false;button.textContent=old}
}

async function refreshOpenAI(){const s=await get('/api/v1/openai/status');$('openai-status').textContent=s.configured?`GPT 已连接 · ${s.model} · 密钥存储于 ${s.storage}`:`尚未配置 API Key，当前使用本地规则回答 · 目标模型 ${s.model}`;$('openai-status').className=`hint ${s.configured?'quality-ok':'gap'}`}
function chatQuestionLabel(item){
  if(item.template_id){const template=currentTemplates.find(x=>x.template_id===item.template_id);return `模板：${template?.name||'已删除的模板'}${item.question?`\n附加要求：${item.question}`:''}`}
  return item.question||'按模板生成';
}
function renderChatHistory(items){
  const log=$('chat-log');
  if(!items.length){log.innerHTML='<div class="chat-empty">暂无历史对话</div>';return}
  log.innerHTML=[...items].reverse().map(item=>`<div class="user">${esc(chatQuestionLabel(item))}</div><div class="chat-meta">${esc(new Date(item.captured_at).toLocaleString('zh-CN',{hour12:false}))} · ${esc(item.provider||'未知来源')}${item.model?` · ${esc(item.model)}`:''}</div><div class="bot${item.legacy?' legacy-answer':''}">${esc(item.answer)}</div>`).join('');
  log.scrollTop=log.scrollHeight;
}
async function loadChatHistory(){
  if(!activeProject)return;
  try{const data=await get(`/api/v1/chat/history?project_id=${encodeURIComponent(activeProject)}`);renderChatHistory(data.items||[])}
  catch(e){$('chat-log').innerHTML=`<div class="chat-empty">历史对话读取失败：${esc(e.message)}</div>`}
}
async function saveOpenAIKey(){const key=$('openai-key').value.trim();if(!key)return;try{await post('/api/v1/openai/key',{api_key:key});$('openai-key').value='';$('openai-settings').classList.add('hidden');await refreshOpenAI()}catch(e){alert(`保存失败：${e.message}`)}}
async function sendChat(q,templateId=null,segmentIds=[]){
  const log=$('chat-log'),template=currentTemplates.find(x=>x.template_id===templateId),label=template?`模板：${template.name}${q?`\n附加要求：${q}`:''}`:q;
  if(log.querySelector('.chat-empty'))log.innerHTML='';
  log.innerHTML+=`<div class="user">${esc(label)}</div>`;$('chat-input').value='';log.scrollTop=log.scrollHeight;
  try{const d=await post('/api/v1/chat',{question:q,project_id:activeProject,template_id:templateId,segment_ids:segmentIds});log.innerHTML+=`<div class="chat-meta">${esc(d.provider)}${d.template_name?` · ${esc(d.template_name)}`:''}${d.model?` · ${esc(d.model)}`:''}</div><div class="bot">${esc(d.answer)}</div>`}
  catch(e){log.innerHTML+=`<div class="bot">查询失败：${esc(e.message)}</div>`}log.scrollTop=log.scrollHeight;
}

document.querySelectorAll('.tabs button').forEach(b=>b.onclick=()=>{document.querySelectorAll('.tabs button').forEach(x=>x.classList.remove('active'));b.classList.add('active');currentWindow=b.dataset.window;refreshDelta()});
$('project-select').onchange=e=>selectProject(e.target.value);$('refresh-btn').onclick=refresh;
$('manage-projects-btn').onclick=async()=>{$('project-manager').classList.toggle('hidden');if(!$('project-manager').classList.contains('hidden'))await refreshProjectManager()};
$('close-projects-btn').onclick=()=>$('project-manager').classList.add('hidden');$('browse-parent-btn').onclick=e=>browseFiles(e.target.dataset.parent);
$('register-path-btn').onclick=()=>registerProject($('project-path-input').value.trim());
$('start-detection-btn').onclick=startDetection;$('stop-detection-btn').onclick=stopDetection;$('combine-segments-btn').onclick=combineSegments;
$('close-correction-btn').onclick=closeCorrectionEditor;$('cancel-correction-btn').onclick=closeCorrectionEditor;$('save-correction-btn').onclick=saveCorrection;
$('openai-settings-btn').onclick=()=>$('openai-settings').classList.toggle('hidden');$('save-openai-key').onclick=saveOpenAIKey;
$('chat-template-select').onchange=()=>{updateTemplateDescription();if(!$('template-editor').classList.contains('hidden'))fillTemplateEditor(activeTemplate())};$('run-template-btn').onclick=runTemplate;
$('template-settings-btn').onclick=openTemplateEditor;$('new-template-btn').onclick=newTemplate;$('save-template-btn').onclick=saveTemplate;$('delete-template-btn').onclick=deleteTemplate;$('close-template-btn').onclick=()=>$('template-editor').classList.add('hidden');
document.querySelectorAll('.quick button').forEach(b=>b.onclick=()=>sendChat(b.textContent));$('chat-form').onsubmit=e=>{e.preventDefault();const q=$('chat-input').value.trim();if(q)sendChat(q)};

async function init(){try{await loadProjects();await loadChatTemplates();await Promise.all([refresh(),loadDetections(),refreshOpenAI(),loadChatHistory()])}catch(e){$('health-label').textContent='初始化失败';$('health-mode').textContent=e.message}}
init();setInterval(refresh,1000);setInterval(loadDetections,5000);
