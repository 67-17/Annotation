"use strict";
// Separate from manual labels: batch results are never auto-applied/confirmed.
let screening = null, screeningResults = [], screeningDataset = null, screeningEpisode = null;
let screeningLimit = 40, screeningPolling = false, focusedScreening = null, screeningSignature = null, boundarySignature = null;
const screeningActive = run => run && ["queued", "planning", "running", "stopping"].includes(run.state);
function boundaryCandidates(event) {
  const model = (event.model_boundaries || []).map(boundary => ({...boundary, origin:"模型"}));
  if (["contact_start", "contact_end"].includes(event.kind) && !model.length)
    model.push({kind:event.kind, candidate_time:event.focus, origin:"模型"});
  const curve = (event.curve_boundaries || []).filter(boundary =>
    ["contact_start", "contact_end"].includes(boundary.kind) && !model.some(m => m.kind === boundary.kind)
  ).map(boundary => ({...boundary, origin:"曲线"}));
  return [...model, ...curve].sort((a,b) => (a.candidate_time ?? a.bracket_end) - (b.candidate_time ?? b.bracket_end));
}
function screeningBoundarySlots(events) {
  const slots={left:[],right:[]};
  if(!ep)return slots;
  for(const side of ["left","right"]){
    const seen=new Set(),candidates=[];
    for(const event of events.filter(e=>e.side===side))for(const candidate of boundaryCandidates(event)){
      const time=candidate.candidate_time??candidate.bracket_end;
      const point=nearest(time,ep.clocks[side]||ep.clocks.data);
      if(!point)continue;
      const key=`${candidate.kind}:${point.time.toFixed(9)}`;
      if(!seen.has(key)){seen.add(key);candidates.push({event,candidate,time:point.time,boundary:candidate.kind==="contact_start"?"start":"end"});}
    }
    candidates.sort((a,b)=>a.time-b.time||(a.boundary==="end"?-1:1));
    let slot=null;
    for(const entry of candidates){
      if(!slot||slot[entry.boundary]){slot={start:null,end:null};slots[side].push(slot);}
      slot[entry.boundary]=entry;
      if(entry.boundary==="end")slot=null;
    }
  }
  return slots;
}
function screeningPairCounts(events) {
  const slots=screeningBoundarySlots(events);
  return {left:slots.left.length,right:slots.right.length};
}
function screeningBoundarySuggestion(side,index,boundary) {
  if(screeningDataset!==ds?.id||screeningEpisode!==ep?.id)return null;
  return screeningBoundarySlots(screeningResults.flatMap(r=>r.events||[]))[side][index]?.[boundary]||null;
}
window.screeningBoundarySuggestion=screeningBoundarySuggestion;
function durationLabel(seconds) {
  if (seconds == null) return "校准中（至少处理两个窗口）";
  if (seconds < 60) return `约 ${Math.max(1, Math.ceil(seconds))} 秒`;
  if (seconds < 3600) return `约 ${Math.ceil(seconds / 60)} 分钟`;
  return `约 ${(seconds / 3600).toFixed(1)} 小时`;
}
function renderScreeningStatus() {
  const active = screeningActive(screening);
  $("mode-single").disabled = active;
  $("mode-multiple").disabled = active;
  $("batch-start").disabled = !ds || active;
  $("batch-scope").disabled = !!active;
  $("batch-stop").hidden = !active;
  $("batch-stop").disabled = screening?.state === "stopping";
  $("batch-resume").hidden = !screening || !["cancelled", "interrupted", "error"].includes(screening.state);
  $("batch-resume").textContent = screening?.options?.pipeline === "curve_first" ? "继续曲线优先筛查" : "继续旧全帧筛查（较慢）";
  $("batch-error").hidden = !screening?.error;
  $("batch-error").textContent = screening?.error || "";
  const planning = screening?.state === "planning";
  const progressTotal = planning ? screening?.total_units : screening?.total_windows;
  const progressDone = planning ? screening?.planned_units : screening?.done_windows;
  $("batch-progress").max = progressTotal || 1;
  $("batch-progress").value = Math.min(progressDone || 0, progressTotal || 1);
  if (!screening) {
    $("batch-status").textContent = ds ? "尚未筛查。开始后先圈定曲线候选段，再交给模型。" : "请先导入数据集。";
    $("batch-estimate").textContent = "预计剩余时间：开始处理后按实际速度估算。";
  } else {
    const curveFirst = screening.options.pipeline === "curve_first";
    const names = {queued:"等待开始", planning:curveFirst ? `曲线圈段中 · ${screening.planned_units || 0}/${screening.total_units || "…"} 侧/EP` : "统计完整帧数…", running:curveFirst ? "模型筛候选段中" : "旧全帧筛查中", stopping:"当前窗口结束后停止…", cancelled:"已停止，进度已保存", interrupted:"上次服务中断，可继续", error:"筛查出错，进度已保存", done:"自动筛查完成"};
    const here = screening.current;
    $("batch-status").textContent = `${names[screening.state] || screening.state} · ${screening.done_windows}/${screening.total_windows ?? "…"} 窗口 · ${screening.done_units}/${screening.total_units} 侧/EP${active && here ? ` · EP ${here.episode} ${here.side === "left" ? "左" : "右"} ${here.start.toFixed(2)}–${here.end.toFixed(2)}s` : ""}`;
    const mode = screening.options.contact_mode === "multiple" ? "多段筛查" : "单段筛查";
    const method = curveFirst ? `${mode} · 曲线 → 模型 · ${screening.candidate_regions ?? "…"} 个候选段${screening.full_scan_windows != null ? ` · ${screening.total_windows}/${screening.full_scan_windows} 个全帧窗口` : ""} · 未筛区段不代表无接触` : `旧全帧任务 · ${mode}；点开始可创建曲线优先任务`;
    $("batch-estimate").textContent = `${method} · 预计剩余：${active ? durationLabel(screening.eta_seconds) : screening.state === "done" ? "自动筛查已结束" : "已暂停"} · 已计处理 ${durationLabel(screening.processing_seconds).replace("约 ", "")}。估算含图像读取和推理，不含人工复核；负载或错误会影响估算。${screening.failed_windows ? ` ${screening.failed_windows} 个窗口失败／格式无效，不能按无接触处理。` : ""}`;
    const timing = screening.last_window;
    if (timing) $("batch-estimate").textContent += ` 上窗：读图 ${timing.read_seconds.toFixed(2)} s · 模型调用 ${timing.model_seconds.toFixed(2)} s · 总计 ${timing.total_seconds.toFixed(2)} s。`;
    if (active && here?.stage) $("batch-status").textContent += here.stage === "read" ? " · 正在读图" : " · 模型处理中";
  }
  document.querySelectorAll("#episodes button").forEach(b => {
    if (!b.dataset.baseLabel) b.dataset.baseLabel = b.textContent;
    const units = screening?.units?.filter(u => u.episode === Number(b.dataset.ep)) || [];
    const complete = units.length === 2 && units.every(u => u.state === "done");
    b.textContent = b.dataset.baseLabel + (complete ? " · 筛查完成" : units.some(u => u.done_windows) ? " · 筛查中/待继续" : "");
  });
}
function renderScreeningEvents() {
  // Keep candidate buttons and scroll position stable during unchanged polling.
  const signature = JSON.stringify([screeningDataset, screeningEpisode, screening?.id, screening?.options?.pipeline, screeningResults, screeningLimit, focusedScreening]);
  if (signature === screeningSignature) return;
  screeningSignature = signature;
  const all = screeningResults.flatMap(r => r.events || []);
  ensureScreeningSlots(screeningPairCounts(all),`${screening?.id||"none"}-${screeningEpisode}`);
  const nextBoundarySignature=JSON.stringify([screeningDataset,screeningEpisode,screening?.id,all]);
  if(ep&&nextBoundarySignature!==boundarySignature){boundarySignature=nextBoundarySignature;renderIntervals();}
  const visible = all.filter(e => boundaryCandidates(e).length > 0);
  $("batch-review-summary").textContent = screeningResults.length ? `${screeningResults.filter(r => r.state === "done").length}/2 侧已完成 · ${visible.length} 处候选区域。候选按钮已放在上方对应的接触/离开框中。${screening?.options.pipeline === "curve_first" ? " 未筛区段仍需留意漏检。" : ""}` : "本 EP 暂无已完成筛查结果；完成一侧后自动显示。";
  $("batch-events").replaceChildren(...visible.slice(0, screeningLimit).map(e => {
    const box = element("div", undefined, "screening-event" + (focusedScreening === e.id ? " focused" : ""));
    const head = element("div", undefined, "card-head");
    head.append(element("strong", `${e.side === "left" ? "左夹爪" : "右夹爪"} · ${e.start.toFixed(3)}–${e.end.toFixed(3)} s · 边界选帧`));
    box.append(head, element("p", e.reason));
    return box;
  }));
  $("batch-more").hidden = visible.length <= screeningLimit;
  if (ep) drawCurves();
}
function focusScreeningEvent(event,time=event.focus) {
  focusedScreening = event.id;
  stopPlayback();
  $("clock").value = ep.clocks[event.side] ? event.side : "data";
  seek(nearest(time,ep.clocks[event.side]||ep.clocks.data).time);
  renderScreeningEvents();
}
window.drawScreeningOverlay = (ctx, curve, x, height) => {
  if (screeningDataset !== ds?.id || screeningEpisode !== ep?.id) return;
  const events = screeningResults.flatMap(r => r.events || []).filter(e => boundaryCandidates(e).length > 0 && (!curve.side || curve.side === e.side));
  for (const e of events) {
    const focused = e.id === focusedScreening;
    if (e.priority === "high") {
      ctx.fillStyle = focused ? "#ffb95d45" : "#ffb95d16";
      ctx.fillRect(x(e.start), 0, Math.max(2, x(e.end) - x(e.start)), height);
    }
    if (e.priority !== "high" || focused) {
      ctx.strokeStyle = focused ? "#ffe39e" : "#d49f5e";
      ctx.setLineDash([4,4]);ctx.beginPath();ctx.moveTo(x(e.focus),0);ctx.lineTo(x(e.focus),height);ctx.stroke();ctx.setLineDash([]);
    }
  }
};
async function refreshScreening() {
  if (!ds || screeningPolling) return;
  screeningPolling = true;
  const datasetId = ds.id, episodeId = ep?.id, version = loadVersion;
  const matches = () => ds?.id === datasetId && ep?.id === episodeId && version === loadVersion;
  try {
    const run = await api(`api/datasets/${datasetId}/screening`, {timeout:15000});
    if (!matches()) return;
    // Publish global progress even if the per-episode result request fails.
    if (screening?.id !== run?.id) screeningResults = [];
    screening = run;
    renderScreeningStatus();
    const detail = run && episodeId != null ? await api(`api/screening/${run.id}/episodes/${episodeId}`, {timeout:15000}) : null;
    if (!matches()) return;
    screening = detail || run;
    screeningResults = detail?.results || [];
    screeningDataset = datasetId;screeningEpisode = episodeId;
    renderScreeningStatus();renderScreeningEvents();
    $("batch-sync").textContent = `进度已刷新 · ${new Date().toLocaleTimeString()}`;
  } catch (error) {
    if (matches()) $("batch-sync").textContent = `刷新失败：${error.message}；显示上次成功获取的数据，将自动重试。`;
  } finally { screeningPolling = false; }
}
$("batch-refresh").onclick = guarded(refreshScreening);
$("batch-start").onclick = guarded(async () => {
  if (!ds) throw Error("请先导入数据集");
  if (dirty || saveRequest) await save();
  if ($("batch-scope").value === "current" && !ep) throw Error("请先选择要测试的 EP");
  const run = await api("api/screening", {method:"POST",body:JSON.stringify({dataset:ds.id,pipeline:"curve_first",contact_mode:annotationMode(),episodes:$("batch-scope").value === "current" ? [ep.id] : null,coverage:"native",frames:6,camera:$("model-camera").value,prompt:$("prompt").value})});
  screening = run;renderScreeningStatus();await refreshScreening();
});
$("batch-stop").onclick = guarded(async () => {await api(`api/screening/${screening.id}/stop`, {method:"POST"});await refreshScreening();});
$("batch-resume").onclick = guarded(async () => {await api(`api/screening/${screening.id}/resume`, {method:"POST"});await refreshScreening();});
$("batch-more").onclick = () => {screeningLimit += 40;renderScreeningEvents();};
window.addEventListener("annotation-episode", () => {
  screeningResults = [];focusedScreening = null;screeningLimit = 40;
  if (screeningDataset !== ds?.id) screening = null;
  renderScreeningStatus();renderScreeningEvents();guarded(refreshScreening)();
});
window.addEventListener("annotation-saved", guarded(refreshScreening));
window.addEventListener("annotation-edited", () => {screeningSignature=null;renderScreeningEvents();});
window.addEventListener("online", guarded(refreshScreening));
document.addEventListener("visibilitychange", () => {if (!document.hidden) guarded(refreshScreening)();});
setInterval(() => {if (ds) guarded(refreshScreening)();}, 3000);
renderScreeningStatus();
