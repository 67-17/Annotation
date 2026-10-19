"use strict";
const $ = id => document.getElementById(id);
let ds=null,ep=null,ann={revision:0,intervals:[],no_contact:[]},current=0,dirty=false,playing=false,loadVersion=0,frameTimer=null,frameTarget=null,displayedFrame=null,shownImages=new Map(),imageRequests=new Map(),editVersion=0,saveRequest=null,playVersion=0,episodeDetailsCache=new Map(),episodePrefetchTimer=null;
const prefix=()=>`api/datasets/${ds.id}/episodes/${ep.id}`;
function toast(message,error=false){$("toast").textContent=message;$("toast").className=error?"error":"";$("toast").hidden=false;clearTimeout(toast.timer);toast.timer=setTimeout(()=>$("toast").hidden=true,6500);}
async function api(url,options={}){
  const {timeout=60000,...request}=options, controller=new AbortController();
  const timer=setTimeout(()=>controller.abort(),timeout);
  try{
    const r=await fetch(url,{cache:"no-store",...request,signal:controller.signal,headers:{"Content-Type":"application/json",...request.headers}});
    if(!r.ok){let d;try{d=await r.json();}catch{d={detail:`HTTP ${r.status}`};}throw Error(typeof d.detail==="string"?d.detail:JSON.stringify(d.detail));}
    return await r.json();
  }catch(error){if(controller.signal.aborted)throw Error("请求超时，请检查连接后重试");throw error;}
  finally{clearTimeout(timer);}
}
const guarded=fn=>(...args)=>Promise.resolve().then(()=>fn(...args)).catch(e=>toast(e.message,true));
function element(tag,text,className){const e=document.createElement(tag);if(text!==undefined)e.textContent=text;if(className)e.className=className;return e;}
function button(text,action){const b=element("button",text);b.onclick=guarded(action);return b;}
function markDirty(){editVersion++;dirty=true;$("save-state").textContent="有未保存修改";window.dispatchEvent(new Event("annotation-edited"));}
function askLeave(){return !dirty||window.confirm("有未保存标注，确定离开并放弃这些修改？");}
async function recent(){const rows=await api("api/datasets");$("recent").replaceChildren(new Option("最近导入",""),...rows.map(r=>new Option(r.name,r.path)));}
function episodeDetails(id){
  if(!episodeDetailsCache.has(id)){
    const request=api(`api/datasets/${ds.id}/episodes/${id}?include_curves=false`);
    episodeDetailsCache.set(id,request);
    request.catch(()=>{if(episodeDetailsCache.get(id)===request)episodeDetailsCache.delete(id);});
  }
  return episodeDetailsCache.get(id);
}
function prefetchAdjacentEpisode(id){
  clearTimeout(episodePrefetchTimer);
  const version=loadVersion,index=ds.episodes.findIndex(row=>row.id===id),next=ds.episodes[index+1]?.id;
  if(next===undefined)return;
  episodePrefetchTimer=setTimeout(()=>{
    if(version!==loadVersion)return;
    episodeDetails(next).catch(()=>{});
    for(const key of episodeDetailsCache.keys())if(key!==id&&key!==next)episodeDetailsCache.delete(key);
  },1000);
}
async function openDataset(){if(!askLeave())return;stopPlayback();invalidateFrames();const version=++loadVersion;const path=$("path").value.trim();$("import").disabled=true;$("dataset-info").textContent="正在读取数据集目录…";let loaded;try{loaded=await api("api/datasets",{method:"POST",body:JSON.stringify({path})});}finally{$("import").disabled=false;}if(version!==loadVersion)return;clearTimeout(episodePrefetchTimer);episodeDetailsCache.clear();ds=loaded;ep=null;dirty=false;$("review").hidden=true;$("empty").hidden=false;$("dataset-info").textContent=`${ds.name} · ${ds.fps} Hz · ${ds.episodes.length} 个 Episode`;
$("warning").hidden=!ds.tactile_metadata.synthetic;$("warning").textContent="此数据包含仿真触觉信号。载荷、接触状态与形变曲线仅作参考，仍需人工确认。";
$("episodes").replaceChildren(...ds.episodes.map(e=>{const b=button(`Episode ${String(e.id).padStart(3,"0")} · ${e.length} 帧`,()=>openEpisode(e.id));b.dataset.ep=e.id;return b;}));$("episode-count").textContent=ds.episodes.length;$("export").disabled=false;recent().catch(e=>toast(e.message,true));await openEpisode(ds.episodes[0].id);}
async function openEpisode(id){if(!askLeave())return;stopPlayback();invalidateFrames();clearTimeout(episodePrefetchTimer);const version=++loadVersion;$("save-state").textContent="正在加载帧时钟…";const base=`api/datasets/${ds.id}/episodes/${id}`;const [loaded,annotations]=await Promise.all([episodeDetails(id),api(base+"/annotations")]);if(version!==loadVersion)return;ep={...loaded,curves:[]};ann=annotations;dirty=false;$("save-state").textContent=`已保存 · 版本 ${ann.revision}`;if(mergeComplementaryIntervals())markDirty();$("title").textContent=`${ds.name} / Episode ${String(id).padStart(3,"0")}`;$("task").textContent=ep.tasks.join(" · ");$("empty").hidden=true;$("review").hidden=false;document.querySelectorAll("#episodes button").forEach(b=>b.classList.toggle("active",Number(b.dataset.ep)===id));
$("clock").replaceChildren(...Object.keys(ep.clocks).map(k=>new Option(k==="data"?`数据帧 · ${ds.fps} Hz`:`${k==="left"?"左":"右"}侧触觉采样`,k)));$("seek").max=ep.duration;
$("cameras").replaceChildren(...ep.cameras.map(c=>{const box=element("div",undefined,"camera");box.append(element("header",c.name));const img=element("img");img.dataset.camera=c.key;img.alt=c.name;box.append(img,element("span","尚未加载","camera-time"));return box;}));
$("model-camera").replaceChildren(...ep.cameras.map(c=>new Option(c.name,c.key)));const scene=ep.cameras.find(c=>c.name==="third_camera")||ep.cameras[0];if(scene)$("model-camera").value=scene.key;
$("no-curves").textContent="正在加载曲线…";$("no-curves").hidden=false;drawCurveShells();renderIntervals();seek(0);window.dispatchEvent(new Event("annotation-episode"));
api(base+"/curves").then(curves=>{if(version!==loadVersion)return;ep.curves=curves;$("no-curves").textContent="此数据集没有可直接展示的触觉数值曲线；可结合图像和夹爪位置人工检查。";drawCurveShells();drawCurves();}).catch(error=>{if(version===loadVersion)$("no-curves").textContent=`曲线加载失败：${error.message}`;});prefetchAdjacentEpisode(id);}
function points(){return ep?.clocks[$("clock").value]||[];}
function nearest(time,list=points()){return list.reduce((a,b)=>Math.abs(a.time-time)<=Math.abs(b.time-time)?a:b,list[0]);}
function invalidateFrames(){clearTimeout(frameTimer);frameTimer=null;frameTarget=null;displayedFrame=null;for(const pending of imageRequests.values())pending.cancel();shownImages=new Map();imageRequests=new Map();}
function frameStatus(){const request=frameTarget;if(!request)return;const ready=request.images.every(img=>shownImages.get(img)?.time===request.time&&shownImages.get(img)?.version===request.version);displayedFrame=ready?request:null;const count=request.images.filter(img=>shownImages.get(img)?.time===request.time&&shownImages.get(img)?.version===request.version).length;$("frame-status").textContent=ready?`当前图像 ${request.time.toFixed(3)} s · camera / photon 已同步`:`正在读取 ${request.time.toFixed(3)} s · ${count}/${request.images.length} 路同步；未同步时不可标记`;
}
function scheduleFrames(){if(frameTimer!==null||!frameTarget)return;frameTimer=setTimeout(()=>{frameTimer=null;refreshFrames();},playing?100:20);}
function refreshFrames(){
  const request=frameTarget;if(!request)return;
  for(const img of request.images){
    const pending=imageRequests.get(img);
    if(pending){if(playing||pending.request===request)continue;pending.cancel();}
    const shown=shownImages.get(img);
    if(shown?.version===request.version&&shown.time===request.time)continue;
    const image=new Image(),url=`${request.base}/frame?camera=${encodeURIComponent(img.dataset.camera)}&time=${request.time.toFixed(6)}`;
    let finished=false;
    const timer=setTimeout(()=>finish(false,"超时"),20000);
    const entry={request,cancel(){if(finished)return;finished=true;clearTimeout(timer);image.onload=image.onerror=null;image.src="";if(imageRequests.get(img)===entry)imageRequests.delete(img);}};
    imageRequests.set(img,entry);
    function finish(ok,reason){
      if(finished)return;finished=true;clearTimeout(timer);image.onload=image.onerror=null;
      if(imageRequests.get(img)===entry)imageRequests.delete(img);
      if(request.version!==loadVersion||request.base!==prefix()||!img.isConnected)return;
      const previous=shownImages.get(img);
      if(ok&&(request===frameTarget||playing&&request.time<=current&&(!previous||previous.time<=request.time))){
        img.src=url;shownImages.set(img,{time:request.time,version:request.version});
        img.parentElement.querySelector(".camera-time").textContent=`图像 ${request.time.toFixed(3)} s`;frameStatus();
      }else if(!ok&&request===frameTarget){
        img.parentElement.querySelector(".camera-time").textContent=`读取${reason} · 上一帧`;
        $("frame-status").textContent=`${img.alt||img.dataset.camera} 读取${reason}；可重新选帧重试`;
      }
      if(frameTarget!==request)scheduleFrames();
    }
    image.onload=()=>finish(true);image.onerror=()=>finish(false,"失败");image.src=url;
  }
}
function seek(time){if(!ep)return;current=Math.max(0,Math.min(ep.duration,Number(time)));$("seek").value=current;const p=nearest(current);$("position").textContent=`${current.toFixed(3)} s · #${p?.index??0}`;drawCurves();frameTarget={version:loadVersion,time:current,base:prefix(),images:[...document.querySelectorAll("#cameras img")]};frameStatus();scheduleFrames();}
function step(delta){stopPlayback();const list=points();if(!list.length)return;const p=nearest(current);const i=list.indexOf(p);seek(list[Math.max(0,Math.min(list.length-1,i+delta))].time);}
function handleReviewKeydown(e){
  if(!ep||e.altKey||e.ctrlKey||e.metaKey||e.target.closest?.('input:not([type="range"]),textarea,select,[contenteditable="true"],[role="textbox"]'))return;
  if(e.key==="ArrowLeft"||e.key==="ArrowRight"){
    e.preventDefault();step(e.key==="ArrowLeft"?-1:1);
  }else if(e.code==="Space"&&!e.target.closest?.("button,input")){
    e.preventDefault();play();
  }
}
function play(){if(!ep)return;if(playing){stopPlayback();return;}playing=true;$("play").textContent="暂停";const version=++playVersion;let last=performance.now();const tick=now=>{if(!playing||version!==playVersion)return;if(now-last>=250){const target=current+(now-last)/1000;last=now;if(target>=ep.duration){playing=false;$("play").textContent="播放";seek(ep.duration);return;}seek(target);}requestAnimationFrame(tick);};requestAnimationFrame(tick);}
function bindCurveScrub(canvas,curve){let pointer=null;canvas.tabIndex=0;canvas.setAttribute("role","slider");canvas.setAttribute("aria-label",`${curve.name}，拖动或用左右方向键选帧`);canvas.setAttribute("aria-valuemin","0");canvas.setAttribute("aria-valuemax",ep.duration);const scrub=e=>{const rect=canvas.getBoundingClientRect();if(!rect.width)return;const time=Math.max(0,Math.min(1,(e.clientX-rect.left)/rect.width))*ep.duration;seek(nearest(time)?.time??time);};canvas.onpointerdown=e=>{if(e.button!==0||pointer!==null)return;e.preventDefault();stopPlayback();if(curve.side&&ep.clocks[curve.side])$("clock").value=curve.side;canvas.focus({preventScroll:true});pointer=e.pointerId;canvas.setPointerCapture(pointer);scrub(e);};canvas.onpointermove=e=>{if(e.pointerId===pointer)scrub(e);};canvas.onpointerup=e=>{if(e.pointerId!==pointer)return;scrub(e);pointer=null;if(canvas.hasPointerCapture(e.pointerId))canvas.releasePointerCapture(e.pointerId);};canvas.onpointercancel=canvas.onlostpointercapture=()=>{pointer=null;};}
function drawCurveShells(){$("curves").replaceChildren(...ep.curves.map((c,i)=>{const wrap=element("div",undefined,"curve");const title=element("div",undefined,"curve-title");title.append(element("span",c.name),element("span",c.unit));const canvas=element("canvas");canvas.width=1200;canvas.height=116;canvas.dataset.curve=i;bindCurveScrub(canvas,c);wrap.append(title,canvas);return wrap;}));$("no-curves").hidden=ep.curves.length>0;}
function drawCurves(){if(!ep)return;document.querySelectorAll("#curves canvas").forEach(canvas=>{const curve=ep.curves[Number(canvas.dataset.curve)],ctx=canvas.getContext("2d"),w=canvas.width,h=canvas.height;ctx.clearRect(0,0,w,h);const values=curve.points.map(p=>p[1]),min=Math.min(...values),max=Math.max(...values),span=max-min||1;const x=t=>t/ep.duration*w,y=v=>h-14-(v-min)/span*(h-28);
canvas.setAttribute("aria-valuenow",current);canvas.setAttribute("aria-valuetext",`${current.toFixed(3)} 秒`);ann.intervals.filter(i=>!curve.side||i.side===curve.side).forEach(i=>{ctx.fillStyle=i.side==="left"?"#68bcff16":"#ecae8016";const start=i.start??0,end=i.end??ep.duration;ctx.fillRect(x(start),0,x(end)-x(start),h);});if(window.drawScreeningOverlay)window.drawScreeningOverlay(ctx,curve,x,h);ctx.strokeStyle="#263345";ctx.lineWidth=1;for(let i=1;i<4;i++){ctx.beginPath();ctx.moveTo(0,h*i/4);ctx.lineTo(w,h*i/4);ctx.stroke();}ctx.beginPath();curve.points.forEach(([t,v],i)=>{i?ctx.lineTo(x(t),y(v)):ctx.moveTo(x(t),y(v));});ctx.strokeStyle=curve.side==="left"?"#68bcff":curve.side==="right"?"#ecae80":"#69dac8";ctx.lineWidth=1.5;ctx.stroke();ctx.fillStyle="#91a1b6";ctx.font="12px monospace";ctx.fillText(max.toPrecision(3),5,12);ctx.fillText(min.toPrecision(3),5,h-3);ctx.strokeStyle="#fff";ctx.beginPath();ctx.moveTo(x(current),0);ctx.lineTo(x(current),h);ctx.stroke();});}
function stopPlayback(){playVersion++;playing=false;$("play").textContent="播放";}
function sideInterval(side){return ann.intervals.find(i=>i.side===side);}
function addInterval(side,source="manual",start=null,end=null,id=null){if(!ep)return;const existing=sideInterval(side);if(existing)return existing;ann.no_contact=ann.no_contact.filter(s=>s!==side);const item={id:id||globalThis.crypto?.randomUUID?.()||`${Date.now()}-${Math.random()}`,side,start,end,clock:ep.clocks[side]?side:"data",starts_before:false,ends_after:false,confirmed:false,note:"",source};ann.intervals.push(item);markDirty();renderIntervals();drawCurves();return item;}
function mergeComplementaryIntervals(){let changed=false;for(const side of ["left","right"]){const items=ann.intervals.filter(i=>i.side===side);if(items.length<2||items.some(i=>i.confirmed))continue;const starts=items.filter(i=>i.start!==null),ends=items.filter(i=>i.end!==null);if(starts.length>1||ends.length>1||new Set(items.map(i=>i.clock)).size>1)continue;const primary=items.find(i=>i.source==="manual")||items[0];for(const item of items){if(item===primary)continue;for(const boundary of ["start","end"]){if(item[boundary]!==null){primary[boundary]=item[boundary];primary[boundary+"_anchor"]=item[boundary+"_anchor"];}}}ann.intervals=ann.intervals.filter(i=>i.side!==side||i===primary);changed=true;}return changed;}
function setBoundary(item,boundary){
  if(item[boundary]!==null){item[boundary]=null;delete item[boundary+"_anchor"];item.confirmed=false;if(item.start===null&&item.end===null)ann.intervals=ann.intervals.filter(i=>i!==item);markDirty();renderIntervals();drawCurves();return;}
  if(playing||!displayedFrame||displayedFrame.version!==loadVersion||displayedFrame!==frameTarget){
    throw Error("请暂停并等待 camera / photon 图像同步，再设置当前帧");
  }
  const p=nearest(current,ep.clocks[item.clock]);
  if(!p)throw Error("此时钟没有可用帧");
  if(Math.abs(p.time-current)>1e-6){seek(p.time);throw Error("已定位到标注时钟的最近帧，请等待图像同步后再次设置");}
  item[boundary]=p.time;item[boundary==="start"?"starts_before":"ends_after"]=false;item.confirmed=false;
  delete item[boundary+"_anchor"];markDirty();renderIntervals();drawCurves();
}
function setSideBoundary(side,boundary){let item=sideInterval(side);if(!item){if(playing||!displayedFrame||displayedFrame!==frameTarget)throw Error("请暂停并等待 camera / photon 图像同步，再设置当前帧");const clock=ep.clocks[side]?side:"data";if(Math.abs(nearest(current,ep.clocks[clock]).time-current)>1e-6){$("clock").value=clock;seek(nearest(current,ep.clocks[clock]).time);throw Error("已定位到标注时钟的最近帧，请等待图像同步后再次设置");}item=addInterval(side);}setBoundary(item,boundary);}
function renderIntervals(){const ordered=["left","right"].flatMap(side=>{const items=ann.intervals.filter(i=>i.side===side);return items.length?items:[null].map(()=>({side,start:null,end:null,confirmed:false}));});$("intervals").replaceChildren(...ordered.map(item=>{const side=item.side,existing=!!item.id,wrap=element("div",undefined,"interval"),head=element("div",undefined,"interval-head");head.append(element("strong",side==="left"?"左侧接触 / 离开":"右侧接触 / 离开","side-"+side));const noContact=element("label"),nc=element("input");nc.type="checkbox";nc.checked=ann.no_contact.includes(side);nc.onchange=()=>{if(nc.checked&&ann.intervals.some(i=>i.side===side)){nc.checked=false;toast("请先取消该侧的接触和离开标记",true);return;}ann.no_contact=ann.no_contact.filter(s=>s!==side);if(nc.checked)ann.no_contact.push(side);markDirty();};noContact.append(nc,document.createTextNode("全程无接触"));head.append(noContact);const fields=element("div",undefined,"interval-fields");
for(const boundary of ["start","end"]){const box=element("div",undefined,"boundary");box.append(element("span",boundary==="start"?"接触第一帧":"离开第一帧","muted"));const anchor=item[boundary+"_anchor"];box.append(element("strong",item[boundary]===null?"未设置":`${item[boundary].toFixed(6)} s${anchor?` · 数据帧 #${anchor.frame_index} · ${anchor.clock} #${anchor.sample_index}`:""}`));box.append(button(item[boundary]!==null?"取消标记":"设为当前帧",()=>setSideBoundary(side,boundary)),button("跳转",()=>{if(item[boundary]!==null)seek(item[boundary]);}));fields.append(box);}
wrap.append(head,fields);return wrap;}));}
async function save(){
  if(!ep)return;
  if(saveRequest){await saveRequest;return;}
  const version=loadVersion, edits=editVersion, url=prefix()+"/annotations", body=JSON.stringify(ann);
  $("save").disabled=true;
  saveRequest=(async()=>{
    const result=await api(url,{method:"PUT",body});
    if(version!==loadVersion)return;
    if(edits!==editVersion){ann.revision=result.revision;$("save-state").textContent="较早修改已保存；仍有新修改待保存";}
    else{ann=result;dirty=false;$("save-state").textContent=`已保存 · 版本 ${ann.revision}`;renderIntervals();toast("标注已保存");}
    window.dispatchEvent(new Event("annotation-saved"));
  })();
  try{await saveRequest;}finally{saveRequest=null;$("save").disabled=false;}
}
async function exportAnnotations(){
  const version=loadVersion,datasetId=ds.id;
  if(dirty||saveRequest)await save();
  if(version!==loadVersion||dirty)throw Error("页面或标注已变更，请保存当前修改后重新导出");
  window.location.href=`api/datasets/${datasetId}/export`;
}
async function markScreeningCandidate(event,boundary,time,source="model",id=`screening-${event.id}`){const existing=sideInterval(event.side);if(existing?.[boundary]!==null&&existing?.[boundary]!==undefined){setBoundary(existing,boundary);return;}const clock=existing?.clock||(ep.clocks[event.side]?event.side:"data"),point=nearest(time,ep.clocks[clock]);if(!point)throw Error("该侧没有可用帧");stopPlayback();$("clock").value=clock;seek(point.time);const target=frameTarget,version=loadVersion;for(let i=0;displayedFrame!==target&&i<200;i++){await new Promise(resolve=>setTimeout(resolve,100));if(version!==loadVersion||frameTarget!==target)throw Error("选帧已改变，请重新设置标记");}if(displayedFrame!==target)throw Error("图像未同步，请检查 camera / photon 后重试");const item=existing||addInterval(event.side,source,null,null,id);setBoundary(item,boundary);}
async function status(){try{const h=await api("api/health");const names={not_loaded:"尚未加载",loading:"加载中",ready:"已就绪",error:"不可用"};$("model-status").textContent=`Qwen3-VL-8B · ${names[h.model.state]||h.model.state}`;$("model-status").className="pill"+(h.model.state==="ready"?" ready":"");$("model-status").title=h.model.error||h.model.path;}catch{$("model-status").textContent="服务连接失败";}}
$("import").onclick=guarded(openDataset);$("recent").onchange=guarded(()=>{if($("recent").value){$("path").value=$("recent").value;return openDataset();}});$("previous").onclick=()=>step(-1);$("next").onclick=()=>step(1);$("play").onclick=play;$("seek").oninput=e=>{stopPlayback();seek(e.target.value);};$("clock").onchange=()=>seek(nearest(current).time);$("save").onclick=guarded(save);$("export").onclick=guarded(exportAnnotations);
window.addEventListener("beforeunload",e=>{if(dirty){e.preventDefault();e.returnValue="";}});window.addEventListener("keydown",handleReviewKeydown);guarded(recent)();status();setInterval(status,10000);
