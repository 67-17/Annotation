"use strict";
// Test UI state and actions without network or GPU; not a browser rendering test.
const assert = require("node:assert/strict");
const fs = require("node:fs");
const vm = require("node:vm");
class Element {
  constructor(text = "") { this.textContent = text;this.children=[];this.dataset={};this.value="";this.checked=false;this.hidden=false;this.classList={toggle(){}}; }
  append(...children) {this.children.push(...children);}
  replaceChildren(...children) {this.children=children;}
}
const ids = new Map(), episodeButton = new Element("Episode 000 · 7 帧"), events = {};
episodeButton.dataset.ep="0";
const sandbox = {console, setInterval(){}, document:{querySelectorAll:()=>[episodeButton],querySelector:()=>null,addEventListener(){}},
  window:{addEventListener:(name,fn)=>events[name]=fn}, elements:ids,
  $:id=>{if(!ids.has(id))ids.set(id,new Element());return ids.get(id);},
  element:(_tag,text)=>new Element(text),
  button:(text,fn)=>{const e=new Element(text);e.onclick=fn;return e;},
  guarded:fn=>fn, stopPlayback(){}, drawCurves(){}, toast(){}, markDirty(){},
  nearest:t=>({time:t}), seek:t=>{sandbox.seekTime=t;},
  markScreeningCandidate:(event,boundary,time)=>sandbox.marked={event:event.id,boundary,time},
  calls:[], results:[], run:null,
  api:async(url,options={})=>{sandbox.calls.push({url,...options});return url.includes("/episodes/")? {...sandbox.run,results:sandbox.results}:sandbox.run;}
};
vm.createContext(sandbox);
vm.runInContext('let ds={id:"ds",episodes:[{id:0}]}, ep={id:0,clocks:{left:[{time:0}],data:[{time:0}]}}, ann={revision:1,intervals:[]}, dirty=false,loadVersion=0;',sandbox);
vm.runInContext(fs.readFileSync(__dirname+"/../static/batch.js","utf8"),sandbox);
async function main() {
  sandbox.run={id:"run",state:"running",options:{coverage:"native"},done_windows:2,total_windows:10,done_units:0,total_units:2,
    processing_seconds:8,eta_seconds:32,units:[{episode:0,side:"left",unreviewed:2,state:"done"}]};
  sandbox.results=[{state:"done",side:"left",events:[
    {id:"event",side:"left",start:1,end:2,focus:1.5,priority:"high",reason:"contact uncertainty",reviewed:true,model_boundaries:[{kind:"contact_start",candidate_time:1.5}],curve_boundaries:[{kind:"contact_end",bracket_end:2}]},
    {id:"warning",side:"left",kind:"data_warning",start:0,end:16.9,focus:0,priority:"high",reason:"缺失区段需人工检查",reviewed:false},
  ]}];
  await vm.runInContext("refreshScreening()",sandbox);
  assert.match(ids.get("batch-estimate").textContent,/32 秒/);
  assert.doesNotMatch(episodeButton.textContent,/待复核/);
  assert.equal(ids.get("batch-events").children.length,1);
  assert.doesNotMatch(ids.get("batch-review-summary").textContent,/待复核/);
  assert.equal(ids.get("batch-start").disabled,true);
  const eventBox = ids.get("batch-events").children[0];
  await vm.runInContext("refreshScreening()",sandbox);
  assert.equal(ids.get("batch-events").children[0],eventBox); // Stable review focus.
  eventBox.children[2].children[0].onclick();
  assert.equal(ids.get("clock").value,"left");
  assert.equal(sandbox.seekTime,1.5);
  eventBox.children[2].children[1].onclick();
  assert.equal(sandbox.marked.boundary,"start");
  assert.match(eventBox.children[3].children[2].textContent,/曲线候选/);
  assert.equal(eventBox.children.length,4); // No per-event review confirmation button.
  assert.equal(sandbox.calls.some(c=>c.url.includes("/reviews/")),false);
  sandbox.run.state="cancelled";
  await vm.runInContext("refreshScreening()",sandbox);
  assert.equal(ids.get("batch-resume").hidden,false);
  assert.equal(ids.get("batch-start").disabled,false);
  assert.match(ids.get("batch-resume").textContent,/旧全帧/);
  sandbox.run.options.pipeline="curve_first";
  sandbox.run.candidate_regions=2;
  sandbox.run.full_scan_windows=100;
  await vm.runInContext("refreshScreening()",sandbox);
  assert.match(ids.get("batch-resume").textContent,/曲线优先/);
  assert.match(ids.get("batch-estimate").textContent,/2 个候选段/);
  assert.match(ids.get("batch-estimate").textContent,/未筛区段不代表无接触/);
  ids.get("batch-scope").value="current";
  await ids.get("batch-start").onclick();
  const start=JSON.parse(sandbox.calls.find(c=>c.url==="api/screening").body);
  assert.equal(start.pipeline,"curve_first");
  assert.deepEqual(start.episodes,[0]);
  ids.get("batch-scope").value="all";
  sandbox.calls=[];
  await ids.get("batch-start").onclick();
  assert.equal(JSON.parse(sandbox.calls.find(c=>c.url==="api/screening").body).episodes,null);
  // A detail failure cannot hide newer global progress, and a later poll can retry.
  const goodApi=sandbox.api;
  sandbox.run.done_windows=9;
  sandbox.api=async(url,options)=>{if(url.includes("/episodes/"))throw Error("offline");return goodApi(url,options);};
  await vm.runInContext("refreshScreening()",sandbox);
  assert.match(ids.get("batch-status").textContent,/9\/10/);
  assert.match(ids.get("batch-sync").textContent,/刷新失败/);
  assert.equal(vm.runInContext("screeningPolling",sandbox),false);
  sandbox.api=goodApi;
  await vm.runInContext("refreshScreening()",sandbox);
  assert.match(ids.get("batch-sync").textContent,/进度已刷新/);
  // Late data for an old page must never replace the currently selected episode.
  let finishPoll;
  sandbox.api=()=>new Promise(resolve=>finishPoll=resolve);
  const pending=vm.runInContext("refreshScreening()",sandbox);
  vm.runInContext("loadVersion++;ep.id=1;",sandbox);
  finishPoll({...sandbox.run,done_windows:10});await pending;
  assert.equal(vm.runInContext("screening.done_windows",sandbox),9);
  console.log("BATCH_UI_STATE_OK: ETA, boundary-only candidates, no review actions and resume controls");
}
main().catch(error=>{console.error(error);process.exitCode=1;});
