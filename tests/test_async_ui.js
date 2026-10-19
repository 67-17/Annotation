"use strict";
// Exercise delayed responses and edits without a network connection or GPU.
const assert=require("node:assert/strict"),fs=require("node:fs"),vm=require("node:vm");
const ids=new Map(),ticks=[];
const sandbox={console,AbortController,setTimeout,clearTimeout,performance:{now:()=>0},
  document:{getElementById(id){if(!ids.has(id))ids.set(id,{value:"",textContent:""});return ids.get(id);}},
  window:{dispatchEvent(){},location:{}},Event:class {},requestAnimationFrame:fn=>ticks.push(fn),renders:0};
vm.createContext(sandbox);
const source=fs.readFileSync(__dirname+"/../static/app.js","utf8");
vm.runInContext(source.slice(0,source.indexOf('$("import").onclick=')),sandbox);
const run=code=>vm.runInContext(code,sandbox);
run('ds={id:"ds"};ep={id:0,duration:2,clocks:{data:[{time:0},{time:1},{time:2}]}};renderIntervals=()=>renders++;drawCurves=()=>{};toast=()=>{};ann={revision:0,intervals:[],no_contact:[]};dirty=true;');
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
  run('ann.intervals=[{id:"manual",side:"right",clock:"data",start:1,end:null,confirmed:false,source:"manual"},{id:"curve",side:"right",clock:"data",start:null,end:2,confirmed:false,source:"curve"}];');
  assert.equal(run('mergeComplementaryIntervals()'),true);
  assert.equal(run('ann.intervals.length'),1);
  assert.equal(run('ann.intervals[0].start'),1);
  assert.equal(run('ann.intervals[0].end'),2);
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
