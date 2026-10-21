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
  guarded:fn=>fn, stopPlayback(){}, drawCurves(){}, renderIntervals(){sandbox.renderedIntervals++;}, toast(){}, markDirty(){}, renderedIntervals:0,
  annotationMode:()=>"multiple",
  ensureScreeningSlots:(counts,key)=>{sandbox.slotCounts=counts;sandbox.slotKey=key;},
  nearest:t=>({time:t}), seek:t=>{sandbox.seekTime=t;},
  screeningCandidateMarked:(side,boundary,time)=>sandbox.markedCandidate?.side===side&&sandbox.markedCandidate?.boundary===boundary&&sandbox.markedCandidate?.time===time,
  markScreeningCandidate:(event,boundary,time)=>sandbox.marked={event:event.id,boundary,time},
  calls:[], results:[], run:null, save:async()=>{sandbox.savedBeforeScreening=true;vm.runInContext("dirty=false",sandbox);},saveRequest:null,
  api:async(url,options={})=>{sandbox.calls.push({url,...options});return url.includes("/episodes/")? {...sandbox.run,results:sandbox.results}:sandbox.run;}
};
vm.createContext(sandbox);
vm.runInContext('let ds={id:"ds",episodes:[{id:0}]}, ep={id:0,clocks:{left:[{time:0}],data:[{time:0}]}}, ann={revision:1,intervals:[]}, dirty=false,loadVersion=0;',sandbox);
vm.runInContext(fs.readFileSync(__dirname+"/../static/batch.js","utf8"),sandbox);
async function main() {
  sandbox.run={id:"run",state:"planning",options:{pipeline:"curve_first",coverage:"native"},planned_units:106,total_units:128,
    done_windows:0,total_windows:null,done_units:0,processing_seconds:1,eta_seconds:null,units:[]};
  await vm.runInContext("refreshScreening()",sandbox);
  assert.equal(ids.get("batch-progress").max,128);
  assert.equal(ids.get("batch-progress").value,106);
  sandbox.run={id:"run",state:"running",options:{coverage:"native"},done_windows:2,total_windows:10,done_units:0,total_units:2,
    processing_seconds:8,eta_seconds:32,units:[{episode:0,side:"left",unreviewed:2,state:"done"}]};
  sandbox.results=[{state:"done",side:"left",events:[
    {id:"event",side:"left",start:1,end:2,focus:1.5,priority:"high",reason:"contact uncertainty",reviewed:true,model_boundaries:[{kind:"contact_start",candidate_time:1.5}],curve_boundaries:[{kind:"contact_end",bracket_end:2}]},
    {id:"warning",side:"left",kind:"data_warning",start:0,end:16.9,focus:0,priority:"high",reason:"缺失区段需人工检查",reviewed:false},
  ]}];
  await vm.runInContext("refreshScreening()",sandbox);
  assert.equal(ids.get("batch-progress").max,10);
  assert.equal(ids.get("batch-progress").value,2);
  assert.match(ids.get("batch-estimate").textContent,/32 秒/);
  assert.doesNotMatch(episodeButton.textContent,/待复核/);
  assert.equal(ids.get("batch-events").children.length,1);
  assert.equal(JSON.stringify(sandbox.slotCounts),JSON.stringify({left:1,right:0}));
  assert.ok(sandbox.renderedIntervals>0);
  assert.doesNotMatch(ids.get("batch-review-summary").textContent,/待复核/);
  assert.equal(ids.get("batch-start").disabled,true);
  const eventBox = ids.get("batch-events").children[0];
  await vm.runInContext("refreshScreening()",sandbox);
  assert.equal(ids.get("batch-events").children[0],eventBox); // Stable review focus.
  const contact=sandbox.window.screeningBoundarySuggestion("left",0,"start");
  const release=sandbox.window.screeningBoundarySuggestion("left",0,"end");
  assert.equal(contact.time,1.5);
  assert.equal(release.time,2);
  const pairs=vm.runInContext('screeningBoundarySlots([{id:"two",side:"left",focus:0,model_boundaries:[{kind:"contact_start",candidate_time:1},{kind:"contact_end",candidate_time:2},{kind:"contact_start",candidate_time:3},{kind:"contact_end",candidate_time:4}]}])',sandbox);
  assert.equal(pairs.left.length,2);
  assert.equal(pairs.left[1].start.time,3);
  assert.equal(pairs.left[1].end.time,4);
  vm.runInContext('focusScreeningEvent(screeningBoundarySuggestion("left",0,"start").event,1.5)',sandbox);
  assert.equal(ids.get("clock").value,"left");
  assert.equal(sandbox.seekTime,1.5);
  assert.equal(eventBox.children.length,2); // Candidate actions belong to the boundary boxes.
  assert.doesNotMatch(fs.readFileSync(__dirname+"/../static/batch.js","utf8"),/scrollIntoView/);
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
  vm.runInContext("dirty=true",sandbox);
  await ids.get("batch-start").onclick();
  assert.equal(sandbox.savedBeforeScreening,true);
  const start=JSON.parse(sandbox.calls.find(c=>c.url==="api/screening").body);
  assert.equal(start.pipeline,"curve_first");
  assert.equal(start.contact_mode,"multiple");
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
