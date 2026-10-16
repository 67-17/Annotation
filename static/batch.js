"use strict";
// Separate from manual labels: batch results are never auto-applied/confirmed.
let screening = null, screeningResults = [], screeningDataset = null, screeningEpisode = null;
let screeningLimit = 40, screeningPolling = false, focusedScreening = null;
const screeningActive = run => run && ["queued", "planning", "running", "stopping"].includes(run.state);
function durationLabel(seconds) {
  if (seconds == null) return "校准中（至少处理两个窗口）";
  if (seconds < 60) return `约 ${Math.max(1, Math.ceil(seconds))} 秒`;
  if (seconds < 3600) return `约 ${Math.ceil(seconds / 60)} 分钟`;
  return `约 ${(seconds / 3600).toFixed(1)} 小时`;
}
function renderScreeningStatus() {
  const active = screeningActive(screening);
  $("batch-start").disabled = !ds || active;
  $("batch-coverage").disabled = !!active;
  $("batch-stop").hidden = !active;
  $("batch-stop").disabled = screening?.state === "stopping";
  $("batch-resume").hidden = !screening || !["cancelled", "interrupted", "error"].includes(screening.state);
  $("batch-error").hidden = !screening?.error;
  $("batch-error").textContent = screening?.error || "";
  $("batch-progress").max = screening?.total_windows || 1;
  $("batch-progress").value = screening?.done_windows || 0;
  if (!screening) {
    $("batch-status").textContent = ds ? "尚未筛查。自动候选不会覆盖人工标注。" : "请先导入数据集。";
    $("batch-estimate").textContent = "预计剩余时间：开始处理后按实际速度估算。";
  } else {
    const names = {queued:"等待开始", planning:"统计完整帧数…", running:"筛查中", stopping:"当前窗口结束后停止…", cancelled:"已停止，进度已保存", interrupted:"上次服务中断，可继续", error:"筛查出错，进度已保存", done:"自动筛查完成，等待人工复核"};
    const here = screening.current;
    $("batch-status").textContent = `${names[screening.state] || screening.state} · ${screening.done_windows}/${screening.total_windows ?? "…"} 窗口 · ${screening.done_units}/${screening.total_units} 侧/EP${active && here ? ` · EP ${here.episode} ${here.side === "left" ? "左" : "右"} ${here.start.toFixed(2)}–${here.end.toFixed(2)}s` : ""}`;
    $("batch-estimate").textContent = `${screening.options.coverage === "native" ? "原始触觉逐帧" : "数据帧逐帧（不是触觉全帧）"} · 预计剩余：${active ? durationLabel(screening.eta_seconds) : screening.state === "done" ? "自动筛查已结束" : "已暂停"} · 已计处理 ${durationLabel(screening.processing_seconds).replace("约 ", "")}。估算含图像读取和推理，不含人工复核；负载或错误会影响估算。${screening.failed_windows ? ` ${screening.failed_windows} 个窗口失败／格式无效，不能按无接触处理。` : ""}`;
  }
  document.querySelectorAll("#episodes button").forEach(b => {
    if (!b.dataset.baseLabel) b.dataset.baseLabel = b.textContent;
    const units = screening?.units?.filter(u => u.episode === Number(b.dataset.ep)) || [];
    const count = units.reduce((n,u) => n + u.unreviewed, 0);
    const complete = units.length === 2 && units.every(u => u.state === "done");
    b.textContent = b.dataset.baseLabel + (count ? ` · 待复核 ${count}` : complete ? " · 自动筛查完成" : units.some(u => u.done_windows) ? " · 筛查中/待继续" : "");
    b.classList.toggle("needs-review", count > 0);
  });
}
function renderScreeningEvents() {
  const all = screeningResults.flatMap(r => r.events || []);
  const visible = all.filter(e => $("batch-show-reviewed").checked || !e.reviewed);
  $("batch-review-summary").textContent = screeningResults.length ? `${screeningResults.filter(r => r.state === "done").length}/2 侧已完成 · ${all.filter(e => !e.reviewed).length} 处待复核 · 橙色为疑点，虚线为候选边界。${screeningResults.some(r => r.machine_no_contact) ? " 某侧模型判断全程无接触，仍未人工确认。" : ""}` : "本 EP 暂无已完成筛查结果；完成一侧后自动显示。";
  $("batch-events").replaceChildren(...visible.slice(0, screeningLimit).map(e => {
    const box = element("div", undefined, "screening-event" + (e.reviewed ? " reviewed" : "") + (focusedScreening === e.id ? " focused" : ""));
    const head = element("div", undefined, "card-head");
    head.append(element("strong", `${e.side === "left" ? "左夹爪" : "右夹爪"} · ${e.start.toFixed(3)}–${e.end.toFixed(3)} s · ${e.reviewed ? "已复核" : e.priority === "high" ? "重点疑点" : "边界选帧"}`));
    box.append(head, element("p", e.reason));
    box.append(button("查看 / 逐帧定位", () => focusScreeningEvent(e)));
    box.append(button(`新建${e.side === "left" ? "左" : "右"}侧区间`, () => {
      focusScreeningEvent(e);
      addInterval(e.side);
      document.querySelector("#intervals .interval:last-child")?.scrollIntoView({behavior:"smooth", block:"center"});
      toast("候选未自动写入：请逐帧选择接触和离开帧，再人工确认并保存。");
    }));
    if (!e.reviewed) box.append(button("此处已人工复核", async () => {
      if (dirty || !ann.revision) throw Error("请先保存本 EP 的人工标注（或空标注／全程无接触），再记录复核。");
      await api(`api/screening/${screening.id}/reviews/${e.id}`, {method:"PUT", body:JSON.stringify({episode:ep.id,revision:ann.revision})});
      await refreshScreening();
    }));
    return box;
  }));
  $("batch-more").hidden = visible.length <= screeningLimit;
  if (ep) drawCurves();
}
function focusScreeningEvent(event) {
  focusedScreening = event.id;
  stopPlayback();
  $("clock").value = ep.clocks[event.side] ? event.side : "data";
  $("model-side").value = event.side;
  $("range-start").value = event.start;
  $("range-end").value = event.end;
  seek(nearest(event.focus).time);
  renderScreeningEvents();
}
window.drawScreeningOverlay = (ctx, curve, x, height) => {
  if (screeningDataset !== ds?.id || screeningEpisode !== ep?.id) return;
  const events = screeningResults.flatMap(r => r.events || []).filter(e => !e.reviewed && (!curve.side || curve.side === e.side));
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
  const datasetId = ds.id, episodeId = ep?.id;
  try {
    const run = await api(`api/datasets/${datasetId}/screening`);
    const detail = run && episodeId != null ? await api(`api/screening/${run.id}/episodes/${episodeId}`) : null;
    if (ds?.id !== datasetId || ep?.id !== episodeId) return;
    screening = detail || run;
    screeningResults = detail?.results || [];
    screeningDataset = datasetId;screeningEpisode = episodeId;
    renderScreeningStatus();renderScreeningEvents();
  } finally { screeningPolling = false; }
}
$("batch-start").onclick = guarded(async () => {
  if (!ds) throw Error("请先导入数据集");
  if (dirty) throw Error("请先保存人工标注，再开始筛查");
  const run = await api("api/screening", {method:"POST",body:JSON.stringify({dataset:ds.id,coverage:$("batch-coverage").value,frames:6,camera:$("model-camera").value,prompt:$("prompt").value})});
  screening = run;renderScreeningStatus();await refreshScreening();
});
$("batch-stop").onclick = guarded(async () => {await api(`api/screening/${screening.id}/stop`, {method:"POST"});await refreshScreening();});
$("batch-resume").onclick = guarded(async () => {await api(`api/screening/${screening.id}/resume`, {method:"POST"});await refreshScreening();});
$("batch-show-reviewed").onchange = renderScreeningEvents;
$("batch-more").onclick = () => {screeningLimit += 40;renderScreeningEvents();};
window.addEventListener("annotation-episode", () => {
  screeningResults = [];focusedScreening = null;screeningLimit = 40;
  if (screeningDataset !== ds?.id) screening = null;
  renderScreeningStatus();renderScreeningEvents();guarded(refreshScreening)();
});
window.addEventListener("annotation-saved", guarded(refreshScreening));
setInterval(() => {if (ds) guarded(refreshScreening)();}, 3000);
renderScreeningStatus();
