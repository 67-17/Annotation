"use strict";
// Interaction/request tests with controlled image loading, without a browser or GPU.
const assert = require("node:assert/strict");
const fs = require("node:fs");
const vm = require("node:vm");
const ids = new Map(), timers = new Map(), imageTimers = new Map(), pendingImages = [];
let nextTimer = 0;
const images = ["camera", "photon_left", "photon_right"].map(camera=>({dataset:{camera},alt:camera,src:"previous",isConnected:true,parentElement:{querySelector:()=>({textContent:""})}}));
const sandbox = {
  console, document:{getElementById(id){if(!ids.has(id))ids.set(id,{value:"",textContent:""});return ids.get(id);},querySelectorAll:()=>images},
  setTimeout(fn,ms){const id=++nextTimer;(ms===20000?imageTimers:timers).set(id,fn);return id;},clearTimeout:id=>{timers.delete(id);imageTimers.delete(id);},
  Image:class {set src(value){this.url=value;if(value)pendingImages.push(this);}},
};
vm.createContext(sandbox);
const source = fs.readFileSync(__dirname+"/../static/app.js","utf8");
vm.runInContext(source.slice(0,source.indexOf('$("import").onclick=')),sandbox);
const run = code=>vm.runInContext(code,sandbox);
run('ds={id:"test"};ep={id:0,duration:1,clocks:{data:[{time:0},{time:1}],left:[{time:0},{time:0.25},{time:0.5},{time:0.75},{time:1}],right:[{time:0},{time:0.4},{time:0.8},{time:1}]}};drawCurves=()=>{};');
ids.get("clock") || sandbox.document.getElementById("clock");
ids.get("clock").value="data";
function flushTimer(){assert.equal(timers.size,1);const [id,fn]=timers.entries().next().value;timers.delete(id);fn();}
async function settle(){await new Promise(resolve=>setImmediate(resolve));}
async function finishImages(fail=-1){const batch=pendingImages.splice(0);batch.forEach((image,i)=>i===fail?image.onerror?.():image.onload?.());await settle();return batch;}
async function main(){
  const canvas={attributes:{},setAttribute(k,v){this.attributes[k]=v;},getBoundingClientRect:()=>({left:10,width:100}),focus(){this.focused=true;},setPointerCapture(id){this.capture=id;},hasPointerCapture(id){return this.capture===id;},releasePointerCapture(){this.capture=null;}};
  sandbox.canvas=canvas;
  run('playing=true;bindCurveScrub(canvas,{name:"左侧载荷",side:"left"})');
  const pointer=x=>({button:0,pointerId:7,clientX:x,preventDefault(){}});
  canvas.onpointerdown(pointer(36));
  assert.equal(run("playing"),false);
  assert.equal(ids.get("play").textContent,"播放");
  assert.equal(ids.get("clock").value,"left");
  assert.equal(run("current"),0.25);
  assert.equal(canvas.focused,true);
  canvas.onpointermove({...pointer(90),pointerId:8});
  assert.equal(run("current"),0.25);
  canvas.onpointermove(pointer(90));
  assert.equal(run("current"),0.75);
  canvas.onpointermove(pointer(-10));
  assert.equal(run("current"),0);
  canvas.onpointerup(pointer(200));
  assert.equal(run("current"),1);
  assert.equal(canvas.capture,null);
  canvas.onpointermove(pointer(60));
  assert.equal(run("current"),1);
  // Continuous motion retains a scheduled fetch instead of resetting a debounce.
  assert.equal(timers.size,1);
  flushTimer();
  assert.equal(pendingImages.length,3);
  run("seek(0.25);seek(0.5)");
  assert.equal(timers.size,1);
  assert.equal(pendingImages.length,3);
  pendingImages[0].onload();await settle();
  assert.ok(images.every(image=>image.src==="previous"));
  await finishImages();
  assert.ok(images.every(image=>image.src==="previous"));
  assert.match(ids.get("frame-status").textContent,/正在读取 0.500 s/);
  flushTimer();
  assert.ok(pendingImages.every(image=>image.url.endsWith("time=0.500000")));
  await finishImages();
  assert.match(ids.get("frame-status").textContent,/已同步/);
  assert.ok(images.every(image=>image.src.endsWith("time=0.500000")));
  // A bundle from another EP cannot overwrite current images, even if it finishes last.
  run("seek(0.75)");flushTimer();
  run("invalidateFrames();loadVersion++;ep.id=1;seek(0)");
  await finishImages();
  assert.ok(images.every(image=>image.src.includes("episodes/0/")));
  flushTimer();await finishImages();
  assert.ok(images.every(image=>image.src.includes("episodes/1/")));
  const previous=images.map(image=>image.src);
  run("seek(1)");flushTimer();await finishImages(1);
  assert.equal(images[1].src,previous[1]);
  assert.notEqual(images[0].src,previous[0]);
  assert.notEqual(images[2].src,previous[2]);
  assert.match(ids.get("frame-status").textContent,/不可标记/);
  assert.equal(timers.size,0); // No automatic endless retries on failure.
  // Drag cancellation stops scrubbing; clicking right curves selects the right clock.
  run('bindCurveScrub(canvas,{name:"右侧载荷",side:"right"})');
  canvas.onpointerdown(pointer(51));
  assert.equal(ids.get("clock").value,"right");
  assert.equal(run("current"),0.4);
  canvas.onpointercancel();canvas.onpointermove(pointer(100));
  assert.equal(run("current"),0.4);
  // A camera that never fires load/error must release the bundle and allow retry.
  flushTimer();
  assert.equal(imageTimers.size,3);
  for(const [id,fn] of [...imageTimers]){imageTimers.delete(id);fn();}
  await settle();pendingImages.splice(0);
  assert.match(ids.get("frame-status").textContent,/超时/);
  run("seek(0.8)");flushTimer();await finishImages();
  assert.match(ids.get("frame-status").textContent,/已同步/);
  // Manual scrubbing cancels stale downloads instead of waiting for their timeout.
  run("seek(0.25)");flushTimer();
  const stale=pendingImages.splice(0);
  run("seek(0.8)");flushTimer();
  assert.ok(stale.every(image=>image.onload===null&&image.url===""));
  assert.ok(pendingImages.every(image=>image.url.endsWith("time=0.800000")));
  await finishImages();
  assert.match(ids.get("frame-status").textContent,/已同步/);
  console.log("REVIEW_UI_OK: native-frame scrubbing, independent images, synchronization, stale EP guard and failures");
}
main().catch(error=>{console.error(error);process.exitCode=1;});
