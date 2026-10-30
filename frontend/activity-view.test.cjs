const assert=require('node:assert/strict'),fs=require('node:fs'),vm=require('node:vm'),ts=require('typescript');
const code=ts.transpileModule(fs.readFileSync(require('node:path').join(__dirname,'src/activityView.ts'),'utf8'),{compilerOptions:{module:ts.ModuleKind.CommonJS}}).outputText;
const out={};vm.runInNewContext(code,{exports:out});
const rows=out.activeEntries([{id:'done',state:'complete'},{id:'waiting',state:'queued',automatic:1},{id:'active',state:'downloading',automatic:1},{id:'cancel',state:'cancelling',automatic:1}],[{id:'sync',state:'running'},{id:'import',state:'queued'},{id:'old',state:'completed'}]);
assert.equal(rows.map(r=>r.key).join(','),'download-active,download-cancel,task-sync,download-waiting,task-import');
assert.equal(rows.filter(r=>r.state==='running').length,3);
console.log('Activity: active-only, cancellation, queued details, running priority, stable current selection and empty state passed.');

const manual=[{id:"manual",state:"downloading",automatic:0},{id:"auto",state:"queued",automatic:1},{id:"done",state:"complete",automatic:0}];
assert.equal(out.manualYoutubeEntries(manual).map(r=>r.item.id).join(","),"manual");
assert.equal(out.activeEntries(manual,[{id:"server-download-vod",state:"running"}]).map(r=>r.item.id).join(","),"auto");

// Overview counts use the same task classification as the main Activity page.
// (deepEqual compares across realms here, so check the JSON form.)
assert.equal(JSON.stringify(out.activityCounts(rows)),'{"running":3,"queued":2,"waiting":0}');
const held=out.activeEntries([],[{id:'discover-waiting',state:'queued',waiting:4},{id:'plain',state:'queued'}]);
assert.equal(JSON.stringify(out.activityCounts(held)),'{"running":0,"queued":1,"waiting":4}');
assert.equal(out.activitySummaryText(out.activityCounts(rows)),'3 running · 2 queued');
assert.equal(out.activitySummaryText(out.activityCounts(held)),'0 running · 1 queued · 4 waiting');


// Overview includes every download engine, while an exact linked request is
// represented by its actual transport. Background checks are not downloads.
const summary=out.overviewActivityCounts([
 {id:'manual',state:'downloading',automatic:0},{id:'automatic',state:'queued',automatic:1},{id:'saved',state:'complete'},
],[
 {id:'discover-youtube',state:'running',engine:'youtube',engine_id:'manual'},
 {id:'server-download-5',state:'running'},
 {id:'discover-vod',state:'queued',engine:'vod',engine_id:'5'},
 {id:'discover-nzb',state:'queued',engine:'nzb',engine_id:'native'},
 {id:'discover-paused',state:'queued',engine:'nzb',engine_id:'paused'},
 {id:'discover-searching',state:'running'},
 {id:'metadata',state:'queued'},
 {id:'discover-waiting',state:'queued',waiting:2},
],[
 {id:'torrent',state:'downloading',progress:.2},{id:'seed',state:'seeding',progress:1},{id:'failed',state:'failed',progress:.2},
],[
 {id:'native',state:'downloading',progress:.4},{id:'publishing',state:'downloading',progress:1},
 {id:'paused',state:'paused',progress:.2},{id:'placement',state:'choose',progress:1},{id:'done',state:'stopped',progress:1},
]);
assert.equal(JSON.stringify(summary),'{'+'"running":6,"queued":2,"waiting":2,"paused":1}');
assert.equal(out.activitySummaryText(summary),'6 running · 2 queued · 2 waiting · 1 paused');
const waitingOnly=out.overviewActivityCounts([],[{id:'discover-waiting',state:'queued',waiting:2}],[],[{id:'ali',state:'downloading',progress:.5}]);
assert.equal(JSON.stringify(waitingOnly),'{'+'"running":1,"queued":0,"waiting":2,"paused":0}','waiting Discover requests do not also increment queued');
const paused=out.overviewActivityCounts([],[{id:'discover-alias',state:'queued',engine:'nzb',engine_id:'ali'}],[],[{id:'ali',state:'paused',progress:.5}]);
assert.equal(out.activitySummaryText(paused),'0 running · 0 queued · 1 paused','VPN/user-paused native work is never reported running or counted twice');
const differentEngines=out.overviewActivityCounts([],[{id:'discover-nzb-only',state:'running',engine:'nzb',engine_id:'same-id'}],[{id:'same-id',state:'queued',progress:0}],[]);
assert.equal(differentEngines.running,1);assert.equal(differentEngines.queued,1,'same native ID in another engine cannot suppress an unrelated request');
const removed=out.overviewActivityCounts([],[{id:'discover-missing',state:'running',engine:'nzb',engine_id:'missing'}],[],[]);
assert.equal(removed.running,1,'a missing native snapshot preserves its request fallback');
for(const state of ['queued','running']){
 const background=[{id:'guide-weeks',state},{id:'youtube-push',state},{id:'metadata',state},{id:'youtube-sync',state},{id:'emby-refresh',state}];
 assert.equal(out.activitySummaryText(out.overviewActivityCounts([],background)), '0 running · 0 queued', 'guide/notification/metadata checks never become '+state+' downloads');
 const acquisition=[{id:'server-download-vod',state},{id:'discover-request',state},{id:'auto-stream',state},{id:'auto-youtube-unknown',state}];
 const counts=out.overviewActivityCounts([],[...background,...acquisition]);
 assert.equal(counts[state],4,'VOD, Discover, watched STRM and unknown YouTube transfers remain '+state);
 assert.equal(counts[state==='queued'?'running':'queued'],0);
 assert.equal(counts.waiting,0);
}
const linkedAuto=out.overviewActivityCounts([{id:'video',state:'downloading'}],[
 {id:'auto-youtube-video',state:'running',engine:'youtube',engine_id:'video'},
 {id:'discover-video',state:'queued',engine:'youtube',engine_id:'video'},
 {id:'auto-completed',state:'complete'},{id:'guide-weeks',state:'queued'},{id:'youtube-push',state:'queued'},
]);
assert.equal(out.activitySummaryText(linkedAuto),'1 running · 0 queued','matching automatic/Discover aliases count the actual transfer once and ignore finished work');
console.log('Overview summary: manual/automatic YouTube, VOD/STRM/Discover acquisition tasks, native transfer/processing, exact linked-parent dedupe, paused/waiting states and exclusion of background guide/notification checks passed.');
