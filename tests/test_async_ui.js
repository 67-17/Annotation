"use strict";
// Exercise delayed responses and edits without a network connection or GPU.
const assert=require("node:assert/strict"),fs=require("node:fs"),vm=require("node:vm");
const ids=new Map(),ticks=[];
const domElement=(tag="div")=>({tag,textContent:"",value:"",checked:false,hidden:false,disabled:false,children:[],dataset:{},className:"",
  classList:{toggle(){}},append(...children){this.children.push(...children)},replaceChildren(...children){this.children=children}});
const sandbox={console,AbortController,setTimeout,clearTimeout,performance:{now:()=>0},
  document:{getElementById(id){if(!ids.has(id))ids.set(id,domElement());return ids.get(id);},
    createElement:tag=>domElement(tag),createTextNode:text=>({textContent:text})},
  window:{dispatchEvent(){},location:{}},Event:class {},requestAnimationFrame:fn=>ticks.push(fn),renders:0};
vm.createContext(sandbox);
const source=fs.readFileSync(__dirname+"/../static/app.js","utf8");
vm.runInContext(source.slice(0,source.indexOf('$("import").onclick=')),sandbox);
const run=code=>vm.runInContext(code,sandbox);
run('globalThis.realRenderIntervals=renderIntervals;ds={id:"ds"};ep={id:0,duration:2,clocks:{data:[{time:0},{time:1},{time:2}]}};renderIntervals=()=>renders++;drawCurves=()=>{};toast=()=>{};ann={revision:0,intervals:[],no_contact:[]};dirty=true;');
const settle=()=>new Promise(resolve=>setImmediate(resolve));
async function main(){
  // A stalled fetch is aborted and the next request is still usable.
  sandbox.fetch=(_url,{signal})=>new Promise((resolve,reject)=>signal.addEventListener("abort",()=>reject(Error("aborted"))));
  await assert.rejects(run('api("test",{timeout:5})'),/请求超时/);
  sandbox.fetch=async(_url,options)=>{assert.equal(options.cache,"no-store");return {ok:true,json:async()=>({ok:true})};};
  assert.equal((await run('api("test")')).ok,true);
  // Episode metadata is shared between prefetch and navigation; annotations
  // are intentionally not cached.
  let metadataCalls=0;
  run('ds={id:"ds",episodes:[{id:0},{id:1}]}');
  sandbox.fetch=async url=>{metadataCalls++;return {ok:true,json:async()=>({id:Number(url.match(/episodes\/(\d+)/)[1]),curves:[]})};};
  const firstDetails=run('episodeDetails(1)'),prefetched=run('episodeDetails(1)');
  assert.equal(firstDetails,prefetched);
  await firstDetails;
  assert.equal(metadataCalls,1);
  // The timeout also covers reading the response body.
  sandbox.fetch=async(_url,{signal})=>({ok:true,json:()=>new Promise((resolve,reject)=>signal.addEventListener("abort",()=>reject(Error("aborted"))))});
  await assert.rejects(run('api("test",{timeout:5})'),/请求超时/);
  let resolveRequest,rejectRequest,calls=0;
  sandbox.request=()=>{calls++;return new Promise((resolve,reject)=>{resolveRequest=resolve;rejectRequest=reject;});};
  run('api=request');
  const first=run('save()'),duplicate=run('save()');
  assert.equal(calls,1);assert.equal(ids.get("save").disabled,true);
  run('ann.intervals.push({id:"new"});markDirty()');
  resolveRequest({revision:1,intervals:[],no_contact:[]});await Promise.all([first,duplicate]);
  assert.equal(run('ann.intervals[0].id'),"new");assert.equal(run("ann.revision"),1);assert.equal(run("dirty"),true);
  assert.equal(sandbox.renders,0);assert.equal(ids.get("save").disabled,false);
  const second=run('save()');
  run('loadVersion++;ep.id=1;ann={revision:7,intervals:[],no_contact:[]};dirty=false;');
  resolveRequest({revision:2,intervals:[{id:"old-ep"}],no_contact:[]});await second;
  assert.equal(run("ann.revision"),7);assert.equal(run("ann.intervals.length"),0);
  run('markDirty()');
  const failed=run('save()');rejectRequest(Error("offline"));await assert.rejects(failed,/offline/);
  assert.equal(run("dirty"),true);assert.equal(ids.get("save").disabled,false);
  const exporting=run('exportAnnotations()');
  run('markDirty();ann.intervals.push({id:"newer"})');resolveRequest({revision:8,intervals:[],no_contact:[]});
  await assert.rejects(exporting,/重新导出/);assert.equal(sandbox.window.location.href,undefined);
  // Do not label a selected frame while older images are still on screen.
  run('item={clock:"data",start:null};frameTarget={version:loadVersion,time:1};displayedFrame={version:loadVersion,time:0};current=1;playing=false;');
  assert.throws(()=>run('setBoundary(item,"start")'),/等待/);
  assert.equal(run("item.start"),null);
  run('displayedFrame=frameTarget;setBoundary(item,"start")');assert.equal(run("item.start"),1);
  run('setBoundary(item,"start")');assert.equal(run("item.start"),null);
  // A suggested boundary remains a draft until both boundaries are saved.
  run('seek=t=>{current=t;frameTarget={version:loadVersion,time:t};displayedFrame=frameTarget};');
  await run('markScreeningCandidate({id:"candidate",side:"left"},"start",1,"curve")');
  assert.equal(run('ann.intervals.find(i=>i.id==="screening-candidate").start'),1);
  assert.equal(run('ann.intervals.find(i=>i.id==="screening-candidate").source'),"curve");
  assert.equal(run('ann.intervals.find(i=>i.id==="screening-candidate").confirmed'),false);
  await run('markScreeningCandidate({id:"candidate",side:"left"},"start",1,"curve")');
  assert.equal(run('ann.intervals.filter(i=>i.side==="left").length'),0);
  run('ann.intervals=[];addInterval("left","manual")');
  await run('markScreeningCandidate({id:"second",side:"left"},"start",1,"curve")');
  assert.equal(run('ann.intervals.filter(i=>i.side==="left").length'),1);
  assert.equal(run('ann.intervals[0].source'),"manual");
  // Multiple mode pairs every release with the nearest unfinished contact,
  // while preserving earlier complete turns.
  run('ann={revision:0,mode:"multiple",intervals:[],no_contact:[]};ep.clocks.data=[{time:0},{time:1},{time:2},{time:3}];');
  await run('markScreeningCandidate({id:"turn-1-start",side:"left"},"start",0,"model")');
  await run('markScreeningCandidate({id:"turn-1-end",side:"left"},"end",1,"model")');
  await run('markScreeningCandidate({id:"turn-2-start",side:"left"},"start",2,"model")');
  await run('markScreeningCandidate({id:"turn-2-end",side:"left"},"end",3,"model")');
  assert.equal(run('ann.intervals.length'),2);
  assert.equal(run('JSON.stringify(ann.intervals.map(i=>[i.start,i.end]))'),'[[0,1],[2,3]]');
  run('renderIntervals=realRenderIntervals;renderIntervals()');
  assert.equal(ids.get("intervals").children.length,3); // side controls + two paired contact rows
  assert.equal(ids.get("intervals").children[1].children[0].textContent,"接触段 1");
  assert.equal(ids.get("intervals").children[1].children[1].children.length,2); // left card + right placeholder
  assert.equal(ids.get("intervals").children[1].children[1].children[0].children[0].children[0].textContent,"左侧");
  assert.equal(ids.get("intervals").children[1].children[1].children[1].className,"interval-slot-empty");
  sandbox.window.screeningBoundarySuggestion=(side,index,boundary)=>side==="left"&&index===0&&boundary==="start"?{event:{id:"suggestion",side:"left"},candidate:{origin:"模型"},time:0}:null;
  sandbox.focusScreeningEvent=(_event,time)=>{sandbox.focusedTime=time;};
  run('renderIntervals()');
  const candidateRow=ids.get("intervals").children[1].children[1].children[0].children[1].children[0].children[4];
  assert.equal(candidateRow.className,"boundary-candidate");
  assert.match(candidateRow.children[0].textContent,/模型候选/);
  await candidateRow.children[0].onclick();
  assert.equal(sandbox.focusedTime,0);
  delete sandbox.window.screeningBoundarySuggestion;
  assert.equal(ids.get("mode-multiple").classList!==undefined,true);
  assert.equal(run('screeningCandidateMarked("left","start",2)'),true);
  assert.throws(()=>run('setAnnotationMode("single")'),/最多一个接触段/);
  await run('markScreeningCandidate({id:"turn-2-start",side:"left"},"start",2,"model")');
  assert.equal(run('ann.intervals[1].start'),null);
  run('ann={revision:0,mode:"multiple",intervals:[],no_contact:[]};renderIntervals=()=>{}');
  await run('markScreeningCandidate({id:"reverse-end",side:"right"},"end",3,"model")');
  await run('markScreeningCandidate({id:"reverse-start",side:"right"},"start",2,"model")');
  assert.equal(run('JSON.stringify(ann.intervals.map(i=>[i.start,i.end]))'),'[[2,3]]');
  run('ann={revision:0,mode:"multiple",intervals:[],no_contact:[]};renderIntervals=()=>{};screeningSlotKey=null;screeningSlotDismissals={left:0,right:0};dirty=false;ensureScreeningSlots({left:3,right:2},"run-0")');
  assert.equal(run('ann.intervals.filter(i=>i.side==="left").length'),3);
  assert.equal(run('ann.intervals.filter(i=>i.side==="right").length'),2);
  assert.equal(run('dirty'),false); // Empty screening slots are not saved edits.
  run('ensureScreeningSlots({left:3,right:2},"run-0")');
  assert.equal(run('dirty'),false);
  run('ann={revision:0,mode:"multiple",intervals:[],no_contact:[]};screeningSlotKey=null;screeningSlotDismissals={left:0,right:0};ensureScreeningSlots({left:1,right:0},"growing-run");ann.intervals[0].start=0;ann.intervals[0].end=1;ensureScreeningSlots({left:2,right:0},"growing-run")');
  assert.equal(run('ann.intervals.length'),2);
  assert.equal(run('new Set(ann.intervals.map(i=>i.id)).size'),2);
  run('ann.intervals=[];addInterval("left","manual",0,1,"screening-repeat");addInterval("left","model",2,3,"screening-repeat")');
  assert.equal(run('new Set(ann.intervals.map(i=>i.id)).size'),2);
  run('ann.intervals=[];screeningSlotKey=null;screeningSlotDismissals={left:0,right:0};ensureScreeningSlots({left:1,right:0},"growing-run");ann.intervals[0].start=0;ann.intervals[0].end=1;ensureScreeningSlots({left:2,right:0},"growing-run")');
  run('ann.intervals[1].id=ann.intervals[0].id;ann.intervals[1].start=2;ann.intervals[1].end=3;dirty=true;api=async(_url,options)=>{globalThis.savedBody=JSON.parse(options.body);return {...savedBody,revision:1}}');
  await run('save()');
  assert.equal(run('new Set(savedBody.intervals.map(i=>i.id)).size'),2);
  assert.equal(run('JSON.stringify(savedBody.intervals.map(i=>[i.start,i.end]))'),'[[0,1],[2,3]]');
  assert.equal(run('dirty'),false);
  run('ann={revision:0,mode:"multiple",intervals:[],no_contact:[]};screeningSlotKey=null;screeningSlotDismissals={left:0,right:0};dirty=false;ensureScreeningSlots({left:3,right:2},"run-0")');
  run('renderIntervals=realRenderIntervals;renderIntervals()');
  assert.equal(ids.get("intervals").children.length,4); // controls + contact segments 1, 2, 3
  assert.equal(ids.get("intervals").children[3].children[0].textContent,"接触段 3");
  assert.equal(ids.get("intervals").children[3].children[1].children[0].className,"interval");
  assert.equal(ids.get("intervals").children[3].children[1].children[1].className,"interval-slot-empty");
  assert.doesNotMatch(source,/人工已确认/);
  // Restarting playback must not revive the previous animation loop.
  sandbox.seeks=[];run('seek=t=>{current=t;seeks.push(t)};current=0;play();stopPlayback();play();');
  const oldTick=ticks.shift(),newTick=ticks.shift();oldTick(500);assert.equal(sandbox.seeks.length,0);
  newTick(500);assert.equal(sandbox.seeks.length,1);run("stopPlayback()");
  // Arrow shortcuts match the on-screen frame buttons, including when a
  // playback button or the seek slider retains keyboard focus.
  run('current=1;ep.clocks.data=[{time:0},{time:1},{time:2}];');
  ids.get("clock").value="data";
  let prevented=0;
  const key=(name,editable=false)=>({key:name,code:name,preventDefault(){prevented++;},
    target:{closest(selector){return editable&&selector.includes("textarea")?{}:null;}}});
  sandbox.arrow=key("ArrowLeft");run("handleReviewKeydown(arrow)");
  assert.equal(run("current"),0);
  sandbox.arrow=key("ArrowRight");run("handleReviewKeydown(arrow)");
  assert.equal(run("current"),1);
  sandbox.arrow=key("ArrowLeft",true);run("handleReviewKeydown(arrow)");
  assert.equal(run("current"),1);
  assert.equal(prevented,2);
  await settle();
  console.log("ASYNC_UI_OK: request timeout, save deduplication, edit/episode races, export guard, synchronized boundaries and playback restart");
}
main().catch(error=>{console.error(error);process.exitCode=1;});
