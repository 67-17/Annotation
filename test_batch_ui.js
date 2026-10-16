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
const sandbox = {console, setInterval(){}, document:{querySelectorAll:()=>[episodeButton],querySelector:()=>null},
  window:{addEventListener:(name,fn)=>events[name]=fn}, elements:ids,
  $:id=>{if(!ids.has(id))ids.set(id,new Element());return ids.get(id);},
  element:(_tag,text)=>new Element(text),
  button:(text,fn)=>{const e=new Element(text);e.onclick=fn;return e;},
  guarded:fn=>fn, stopPlayback(){}, drawCurves(){}, toast(){}, markDirty(){},
  nearest:t=>({time:t}), seek:t=>{sandbox.seekTime=t;},
  addInterval:side=>sandbox.addedSide=side,
  calls:[], results:[], run:null,
  api:async(url,options={})=>{sandbox.calls.push({url,...options});return url.includes("/episodes/")? {...sandbox.run,results:sandbox.results}:sandbox.run;}
};
vm.createContext(sandbox);
vm.runInContext('let ds={id:"ds",episodes:[{id:0}]}, ep={id:0,clocks:{left:[{time:0}],data:[{time:0}]}}, ann={revision:1}, dirty=false;',sandbox);
vm.runInContext(fs.readFileSync(__dirname+"/static/batch.js","utf8"),sandbox);
async function main() {
  sandbox.run={id:"run",state:"running",options:{coverage:"native"},done_windows:2,total_windows:10,done_units:0,total_units:2,
    processing_seconds:8,eta_seconds:32,units:[{episode:0,side:"left",unreviewed:1,state:"done"}]};
  sandbox.results=[{state:"done",side:"left",events:[{id:"event",side:"left",start:1,end:2,focus:1.5,priority:"high",reason:"contact uncertainty",reviewed:false}]}];
  await vm.runInContext("refreshScreening()",sandbox);
  assert.match(ids.get("batch-estimate").textContent,/32 秒/);
  assert.match(episodeButton.textContent,/待复核 1/);
  assert.equal(ids.get("batch-start").disabled,true);
  const eventBox = ids.get("batch-events").children[0];
  eventBox.children[2].onclick();
  assert.equal(ids.get("clock").value,"left");
  assert.equal(sandbox.seekTime,1.5);
  assert.equal(ids.get("range-end").value,2);
  eventBox.children[3].onclick();
  assert.equal(sandbox.addedSide,"left");
  vm.runInContext("dirty=true",sandbox);
  await assert.rejects(eventBox.children[4].onclick(),/请先保存/);
  vm.runInContext("dirty=false",sandbox);
  await eventBox.children[4].onclick();
  const review = sandbox.calls.find(c=>c.url.includes("/reviews/"));
  assert.equal(JSON.parse(review.body).revision,1);
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
  console.log("BATCH_UI_STATE_OK: ETA, episode badges, side-clock focus, manual interval action, saved-revision guard and resume controls");
}
main().catch(error=>{console.error(error);process.exitCode=1;});
