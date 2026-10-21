"use strict";
const $ = id => document.getElementById(id);
let ds=null,ep=null,ann={revision:0,mode:"single",intervals:[],no_contact:[]},current=0,dirty=false,playing=false,loadVersion=0,frameTimer=null,frameTarget=null,displayedFrame=null,shownImages=new Map(),imageRequests=new Map(),editVersion=0,saveRequest=null,playVersion=0,episodeDetailsCache=new Map(),episodePrefetchTimer=null,screeningSlotKey=null,screeningSlotDismissals={left:0,right:0},intervalIdSequence=0;
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
async function openEpisode(id){if(!askLeave())return;stopPlayback();invalidateFrames();clearTimeout(episodePrefetchTimer);const version=++loadVersion;$("save-state").textContent="正在加载帧时钟…";const base=`api/datasets/${ds.id}/episodes/${id}`;const [loaded,annotations]=await Promise.all([episodeDetails(id),api(base+"/annotations")]);if(version!==loadVersion)return;ep={...loaded,curves:[]};ann={mode:"single",...annotations};screeningSlotKey=null;screeningSlotDismissals={left:0,right:0};dirty=false;$("save-state").textContent=`已保存 · 版本 ${ann.revision}`;$("title").textContent=`${ds.name} / Episode ${String(id).padStart(3,"0")}`;$("task").textContent=ep.tasks.join(" · ");$("empty").hidden=true;$("review").hidden=false;document.querySelectorAll("#episodes button").forEach(b=>b.classList.toggle("active",Number(b.dataset.ep)===id));
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
function setupCurveWindow(){const panel=$("curve-card"),handle=$("curve-drag-handle"),opacity=$("curve-opacity"),collapse=$("curve-collapse");let drag=null;opacity.oninput=()=>panel.style.opacity=String(Number(opacity.value)/100);collapse.onclick=()=>{const collapsed=panel.classList.toggle("collapsed");collapse.textContent=collapsed?"+":"−";collapse.title=collapsed?"展开曲线窗口":"收起曲线窗口";collapse.setAttribute("aria-label",collapse.title);};handle.onpointerdown=e=>{if(e.button!==0||e.target.closest("button,input,label"))return;const rect=panel.getBoundingClientRect();drag={id:e.pointerId,x:e.clientX-rect.left,y:e.clientY-rect.top};handle.setPointerCapture(e.pointerId);e.preventDefault();};handle.onpointermove=e=>{if(!drag||drag.id!==e.pointerId)return;panel.style.left=`${Math.max(0,Math.min(window.innerWidth-panel.offsetWidth,e.clientX-drag.x))}px`;panel.style.top=`${Math.max(0,Math.min(window.innerHeight-handle.offsetHeight,e.clientY-drag.y))}px`;panel.style.right="auto";};handle.onpointerup=handle.onpointercancel=()=>{drag=null;};window.addEventListener("resize",()=>{if(!panel.style.left)return;panel.style.left=`${Math.max(0,Math.min(window.innerWidth-panel.offsetWidth,parseFloat(panel.style.left)))}px`;panel.style.top=`${Math.max(0,Math.min(window.innerHeight-handle.offsetHeight,parseFloat(panel.style.top)))}px`;});opacity.oninput();}
function drawCurveShells(){$("curves").replaceChildren(...ep.curves.map((c,i)=>{const wrap=element("div",undefined,"curve");const title=element("div",undefined,"curve-title");title.append(element("span",c.name),element("span",c.unit));const canvas=element("canvas");canvas.width=1200;canvas.height=116;canvas.dataset.curve=i;bindCurveScrub(canvas,c);wrap.append(title,canvas);return wrap;}));$("no-curves").hidden=ep.curves.length>0;}
function drawCurves(){if(!ep)return;document.querySelectorAll("#curves canvas").forEach(canvas=>{const curve=ep.curves[Number(canvas.dataset.curve)],ctx=canvas.getContext("2d"),w=canvas.width,h=canvas.height;ctx.clearRect(0,0,w,h);const values=curve.points.map(p=>p[1]),min=Math.min(...values),max=Math.max(...values),span=max-min||1;const x=t=>t/ep.duration*w,y=v=>h-14-(v-min)/span*(h-28);
canvas.setAttribute("aria-valuenow",current);canvas.setAttribute("aria-valuetext",`${current.toFixed(3)} 秒`);ann.intervals.filter(i=>!curve.side||i.side===curve.side).forEach(i=>{ctx.fillStyle=i.side==="left"?"#68bcff16":"#ecae8016";const start=i.start??0,end=i.end??ep.duration;ctx.fillRect(x(start),0,x(end)-x(start),h);});if(window.drawScreeningOverlay)window.drawScreeningOverlay(ctx,curve,x,h);ctx.strokeStyle="#263345";ctx.lineWidth=1;for(let i=1;i<4;i++){ctx.beginPath();ctx.moveTo(0,h*i/4);ctx.lineTo(w,h*i/4);ctx.stroke();}ctx.beginPath();curve.points.forEach(([t,v],i)=>{i?ctx.lineTo(x(t),y(v)):ctx.moveTo(x(t),y(v));});ctx.strokeStyle=curve.side==="left"?"#68bcff":curve.side==="right"?"#ecae80":"#69dac8";ctx.lineWidth=1.5;ctx.stroke();ctx.fillStyle="#91a1b6";ctx.font="12px monospace";ctx.fillText(max.toPrecision(3),5,12);ctx.fillText(min.toPrecision(3),5,h-3);ctx.strokeStyle="#fff";ctx.beginPath();ctx.moveTo(x(current),0);ctx.lineTo(x(current),h);ctx.stroke();});}
function stopPlayback(){playVersion++;playing=false;$("play").textContent="播放";}
function sideInterval(side){return ann.intervals.find(i=>i.side===side);}
function annotationMode(){return ann.mode==="multiple"?"multiple":"single";}
function intervalTime(item){return item.start??item.end??Infinity;}
function sideIntervals(side){return ann.intervals.filter(i=>i.side===side).sort((a,b)=>intervalTime(a)-intervalTime(b));}
function isScreeningSlot(item){return item.id?.startsWith("screening-slot-");}
function emptyInterval(item){return item.start==null&&item.end==null;}
function uniqueIntervalId(prefix){let id;do{id=`${prefix}-${Date.now().toString(36)}-${++intervalIdSequence}`;}while(ann.intervals.some(item=>item.id===id));return id;}
function repairDuplicateIntervalIds(){const seen=new Set();for(const item of ann.intervals){if(!item.id||seen.has(item.id))item.id=uniqueIntervalId(isScreeningSlot(item)?"screening-slot":"annotation");seen.add(item.id);}}
function setAnnotationMode(mode){if(mode===annotationMode())return;if(mode==="single"){ann.intervals=ann.intervals.filter(item=>!isScreeningSlot(item)||!emptyInterval(item));if(["left","right"].some(side=>sideIntervals(side).length>1))throw Error("请先将左右两侧都删到最多一个接触段，再切换为单段模式");}ann.mode=mode;markDirty();renderIntervals();if(window.renderScreeningEvents){screeningSignature=null;renderScreeningEvents();}}
function addInterval(side,source="manual",start=null,end=null,id=null){if(!ep)return;const existing=annotationMode()==="single"&&sideInterval(side);if(existing)return existing;ann.no_contact=ann.no_contact.filter(s=>s!==side);const item={id:id&&!ann.intervals.some(item=>item.id===id)?id:uniqueIntervalId("annotation"),side,start,end,clock:ep.clocks[side]?side:"data",starts_before:false,ends_after:false,confirmed:false,note:"",source};ann.intervals.push(item);markDirty();renderIntervals();drawCurves();return item;}
function removeInterval(item){ann.intervals=ann.intervals.filter(i=>i!==item);if(isScreeningSlot(item))screeningSlotDismissals[item.side]++;markDirty();renderIntervals();drawCurves();}
function ensureScreeningSlots(counts,key){if(annotationMode()!=="multiple"||!ep)return;if(screeningSlotKey!==key){screeningSlotKey=key;screeningSlotDismissals={left:0,right:0};}let changed=false;for(const side of ["left","right"]){const wanted=Math.max(0,(counts[side]||0)-screeningSlotDismissals[side]);const material=sideIntervals(side).filter(item=>!isScreeningSlot(item)||!emptyInterval(item)).length;const blanks=sideIntervals(side).filter(item=>isScreeningSlot(item)&&emptyInterval(item));const needed=Math.max(0,wanted-material);while(blanks.length>needed){ann.intervals=ann.intervals.filter(item=>item!==blanks.pop());changed=true;}while(blanks.length<needed){const item={id:uniqueIntervalId(`screening-slot-${key}-${side}`),side,start:null,end:null,clock:ep.clocks[side]?side:"data",starts_before:false,ends_after:false,confirmed:false,note:"",source:"curve"};ann.intervals.push(item);blanks.push(item);changed=true;}}if(changed)renderIntervals();}
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
function renderIntervalCard(item,index){const side=item.side,wrap=element("div",undefined,"interval"),head=element("div",undefined,"interval-head");head.append(element("strong",side==="left"?"左侧":"右侧","side-"+side));if(annotationMode()==="multiple"&&item.id)head.append(button("删除此段",()=>removeInterval(item)));const fields=element("div",undefined,"interval-fields");
for(const boundary of ["start","end"]){const box=element("div",undefined,"boundary");box.append(element("span",boundary==="start"?"接触第一帧":"离开第一帧","muted"));const anchor=item[boundary+"_anchor"],value=item[boundary];box.append(element("strong",value==null?"未设置":`${value.toFixed(6)} s${anchor?` · 数据帧 #${anchor.frame_index} · ${anchor.clock} #${anchor.sample_index}`:""}`));box.append(button(value!=null?"取消标记":"设为当前帧",()=>item.id?setBoundary(item,boundary):setSideBoundary(side,boundary)),button("跳转",()=>{if(value!=null)seek(value);}));const suggestion=window.screeningBoundarySuggestion?.(side,index,boundary);if(suggestion){const row=element("div",undefined,"boundary-candidate"),marked=screeningCandidateMarked(side,boundary,suggestion.time),source=suggestion.candidate.origin;row.append(button(`${source}候选 · ${suggestion.time.toFixed(3)} s`,()=>focusScreeningEvent(suggestion.event,suggestion.time)),button(marked?"取消标记":"设置为标记",()=>markScreeningCandidate(suggestion.event,boundary,suggestion.time,source==="模型"?"model":"curve",`screening-${suggestion.event.id}-${index}`,item.id?item:null)));box.append(row);}fields.append(box);}wrap.append(head,fields);return wrap;}
function renderIntervals(){$("mode-single").classList.toggle("active",annotationMode()==="single");$("mode-multiple").classList.toggle("active",annotationMode()==="multiple");$("annotation-mode-note").textContent=annotationMode()==="multiple"?"多段模式：每次接触和离开保存为独立区间；离开帧是第一帧未接触。":"单段模式：每侧最多一个接触区间；离开帧是第一帧未接触。";const bySide={left:sideIntervals("left"),right:sideIntervals("right")};const controls=element("div",undefined,"interval-side-controls");for(const side of ["left","right"]){const sideControls=element("section",undefined,"interval-side-control"),actions=element("div"),label=element("label"),nc=element("input");sideControls.append(element("strong",side==="left"?"左侧":"右侧","side-"+side));nc.type="checkbox";nc.checked=ann.no_contact.includes(side);nc.onchange=()=>{if(nc.checked&&sideIntervals(side).length){nc.checked=false;toast("请先删除该侧的接触段",true);return;}ann.no_contact=ann.no_contact.filter(s=>s!==side);if(nc.checked)ann.no_contact.push(side);markDirty();};label.append(nc,document.createTextNode("全程无接触"));actions.append(label);if(annotationMode()==="multiple")actions.append(button(`新增${side==="left"?"左":"右"}侧接触段`,()=>addInterval(side)));sideControls.append(actions);controls.append(sideControls);}const count=annotationMode()==="single"?1:Math.max(bySide.left.length,bySide.right.length);const pairs=[];for(let index=0;index<count;index++){const pair=element("section",undefined,"interval-pair");pair.append(element("h4",`接触段 ${index+1}`));const grid=element("div",undefined,"interval-pair-grid");for(const side of ["left","right"]){const item=bySide[side][index];if(item)grid.append(renderIntervalCard(item,index));else if(annotationMode()==="single")grid.append(renderIntervalCard({side,start:null,end:null,confirmed:false,clock:ep.clocks[side]?side:"data"},index));else{const empty=element("div",undefined,"interval-slot-empty");empty.append(element("strong",side==="left"?"左侧":"右侧","side-"+side),element("span",`${side==="left"?"左":"右"}侧暂无第 ${index+1} 段`,"muted"),button(`新增${side==="left"?"左":"右"}侧接触段`,()=>addInterval(side)));grid.append(empty);}}pair.append(grid);pairs.push(pair);}const children=[controls,...pairs];if(!pairs.length)children.push(element("p","尚无接触段，可从上方新增左侧或右侧接触段。","interval-empty"));$("intervals").replaceChildren(...children);}
async function save(){
  if(!ep)return;
  if(saveRequest){await saveRequest;return;}
  repairDuplicateIntervalIds();
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
function screeningCandidateInterval(side,boundary,time){return ann.intervals.find(item=>item.side===side&&item[boundary]!=null&&Math.abs(item[boundary]-time)<1e-6);}
function screeningCandidateMarked(side,boundary,time){return !!screeningCandidateInterval(side,boundary,time);}
function pairingInterval(side,boundary,time){const items=sideIntervals(side),blank=items.find(emptyInterval);if(boundary==="start")return items.filter(i=>i.start==null&&i.end!=null&&i.end>time).sort((a,b)=>a.end-b.end)[0]||blank;return items.filter(i=>i.end==null&&i.start!=null&&i.start<time).sort((a,b)=>b.start-a.start)[0]||blank;}
async function markScreeningCandidate(event,boundary,time,source="model",id=`screening-${event.id}`,targetItem=null){const clock=ep.clocks[event.side]?event.side:"data",point=nearest(time,ep.clocks[clock]);if(!point)throw Error("该侧没有可用帧");const marked=screeningCandidateInterval(event.side,boundary,point.time);if(marked){setBoundary(marked,boundary);return;}stopPlayback();$("clock").value=clock;seek(point.time);const target=frameTarget,version=loadVersion;for(let i=0;displayedFrame!==target&&i<200;i++){await new Promise(resolve=>setTimeout(resolve,100));if(version!==loadVersion||frameTarget!==target)throw Error("选帧已改变，请重新设置标记");}if(displayedFrame!==target)throw Error("图像未同步，请检查 camera / photon 后重试");let item=targetItem&&ann.intervals.includes(targetItem)?targetItem:annotationMode()==="single"?sideInterval(event.side):pairingInterval(event.side,boundary,point.time);item=item||addInterval(event.side,source,null,null,id);item[boundary]=point.time;item[boundary==="start"?"starts_before":"ends_after"]=false;item.confirmed=false;delete item[boundary+"_anchor"];markDirty();renderIntervals();drawCurves();}
async function status(){try{const h=await api("api/health");const names={not_loaded:"尚未加载",loading:"加载中",ready:"已就绪",error:"不可用"};$("model-status").textContent=`Qwen3-VL-8B · ${names[h.model.state]||h.model.state}`;$("model-status").className="pill"+(h.model.state==="ready"?" ready":"");$("model-status").title=h.model.error||h.model.path;}catch{$("model-status").textContent="服务连接失败";}}
$("import").onclick=guarded(openDataset);$("recent").onchange=guarded(()=>{if($("recent").value){$("path").value=$("recent").value;return openDataset();}});$("previous").onclick=()=>step(-1);$("next").onclick=()=>step(1);$("play").onclick=play;$("seek").oninput=e=>{stopPlayback();seek(e.target.value);};$("clock").onchange=()=>seek(nearest(current).time);$("mode-single").onclick=guarded(()=>setAnnotationMode("single"));$("mode-multiple").onclick=guarded(()=>setAnnotationMode("multiple"));$("save").onclick=guarded(save);$("export").onclick=guarded(exportAnnotations);setupCurveWindow();
window.addEventListener("beforeunload",e=>{if(dirty){e.preventDefault();e.returnValue="";}});window.addEventListener("keydown",handleReviewKeydown);guarded(recent)();status();setInterval(status,10000);
