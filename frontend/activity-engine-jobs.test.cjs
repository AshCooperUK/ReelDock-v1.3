const sharedSelect=require('./select-field-test-helper.cjs')();
const choicesByMarkup=new Map();
const assert = require('node:assert/strict'), fs = require('node:fs'), vm = require('node:vm'), ts = require('typescript');
const React = require('react'), jsxRuntime = require('react/jsx-runtime'), { renderToStaticMarkup } = require('react-dom/server');
const Mantine = require('@mantine/core');
const h = React.createElement;
const out = {};
const generic = new Proxy({}, { get: (_, key) => key === '__esModule' ? false : ({ children }) => h('div', null, children) });
const code = ts.transpileModule(fs.readFileSync(__dirname + '/src/LibraryPages.tsx', 'utf8'), { compilerOptions: { module: ts.ModuleKind.CommonJS, jsx: ts.JsxEmit.ReactJSX, esModuleInterop: true } }).outputText;
const warmSnapshots={};vm.runInNewContext(ts.transpileModule(fs.readFileSync(__dirname+'/src/warmSnapshots.ts','utf8'),{compilerOptions:{module:ts.ModuleKind.CommonJS}}).outputText,{exports:warmSnapshots});
const activityStatus={};vm.runInNewContext(ts.transpileModule(fs.readFileSync(__dirname+'/src/components/ActivityStatus.tsx','utf8'),{compilerOptions:{module:ts.ModuleKind.CommonJS,jsx:ts.JsxEmit.ReactJSX}}).outputText,{exports:activityStatus,require});

const activityGroup={};vm.runInNewContext(ts.transpileModule(fs.readFileSync(__dirname+'/src/components/ActivityGroup.tsx','utf8'),{compilerOptions:{module:ts.ModuleKind.CommonJS,jsx:ts.JsxEmit.ReactJSX}}).outputText,{exports:activityGroup,require});
const now = 1_800_000_000;
function history(count, more = true) {
  // Recent YouTube history only: every row is newer than the engine jobs below.
  const items = Array.from({ length: count }, (_, i) => ({ kind: 'youtube', item: { id: 'v' + i, title: 'Video ' + i, state: 'complete', updated: now - i, created: now - i } }));
  return { items, active: [], more, busy: false, error: '', acquisitionHold: false, refresh() {}, load() {}, loadMore() {} };
}
function engine(prefix, jobs) {
  const stateOf = job => job.state === 'failed' ? 'failed' : job.progress >= 1 && job.state === 'stopped' ? 'complete' : ['paused', 'queued', 'choose'].includes(job.state) ? 'queued' : 'downloading';
  return { jobs, stateOf, card: job => h('article', { key: job.id, 'data-job': prefix + job.id }, prefix + ':' + job.id + ':' + stateOf(job), stateOf(job) === 'complete' && prefix === 'nzb' ? h('button', null, 'Change metadata') : null) };
}
function render(feed, torrents, nzbs, phone=false, interaction) {
  render.lastRows=[];
  let normalizedChoices=[];
  const modules = {
    './activityClear':require('./activity-clear-test-helper.cjs')(),'./warmSnapshots':require('./warm-snapshots-test-helper.cjs')(),
    react: interaction?{...React,useState:initial=>initial==='all'?[interaction.filter||'all',value=>interaction.filter=value]:React.useState(initial)}:React, 'react/jsx-runtime': jsxRuntime,
    '@mantine/core': Mantine,
    './components/ActivityGroup':activityGroup,'./components/ActivityStatus':activityStatus,'./activityClear':require('./activity-clear-test-helper.cjs')(),'./warmSnapshots':warmSnapshots,
    './components/SelectField':{SelectField:props=>{if(interaction)interaction.select=props;normalizedChoices=Array.from(sharedSelect.normalizeSelectData(props)).flatMap(row=>row.items||[row]);return h(sharedSelect.SelectField,props);}},
    '@mantine/hooks':{useMediaQuery:()=>phone},
    './LibraryOptimiseJobs': { LibraryOptimiseActivity: () => h('section', {'data-optimisation-fixture': true}, '99 separate optimisation jobs') },
    './DownloadHistory': { useDownloadHistory: () => feed },
    './TorrentDownloads': { useTorrentActivity: () => torrents },
    './NzbDownloads': { useNzbActivity: () => nzbs },
    './downloadFeed': { refreshDownloads() {}, downloadLabel: item => item.phase === 'encoding' ? 'Encoding with FFmpeg' : 'Downloading' },
    './mediaDate': { mediaDate: d => d }, './api': { api: async () => ({}) },
    './components/ui': { Button: ({ children, variant, ...props }) => h('button', props, children), Dialog: generic.Dialog, DialogContent: generic.DialogContent, DialogDescription: generic.DialogDescription, DialogTitle: generic.DialogTitle, Switch: generic.Switch },
    './components/ListRow': { __esModule: true, default: props => {render.lastRows.push(props);return h('article', null, props.title);},CompactActivityRows:({children})=>children,ActivityQueueRows:({children})=>children },
    './components/states': { EmptyState: ({ title }) => h('p', null, title), SkeletonCard: generic.SkeletonCard, SkeletonRow: generic.SkeletonRow },
  };
  const exportsObject = {};
  vm.runInNewContext(code, { exports: exportsObject, require: name => modules[name] || (name.endsWith('.css') ? {} : new Proxy(generic, { get: (t, k) => k === '__esModule' ? false : k === 'default' ? generic.X : t[k] })) });
  const html=renderToStaticMarkup(h(Mantine.MantineProvider, {forceColorScheme:'dark'}, h(exportsObject.DownloadList, { csrf: 'c' })));
  render.distinctActivityJobs=exportsObject.distinctActivityJobs;
  choicesByMarkup.set(html,normalizedChoices);return html;
}
const tab = (html,label) => Number(choicesByMarkup.get(html).find(row=>row.label.startsWith(label+' (')).label.match(/\((\d+)\)$/)[1]);

// History is only partly paged (more=true) and 15 recent rows are newer than every engine job.
const oldNzb = [
  { id: 'n1', state: 'stopped', progress: 1, added: now - 900000 }, { id: 'n2', state: 'stopped', progress: 1, added: now - 800000 },
  { id: 'n3', state: 'failed', progress: 1, added: now - 700000 }, { id: 'n4', state: 'downloading', progress: 0.4, added: now - 600000 },
];
const oldTorrent = [{ id: 't1', state: 'failed', progress: 0.2, added: now - 950000 }, { id: 't2', state: 'queued', progress: 0, added: now - 10 }];
const html = render(history(15), engine('torrent', oldTorrent), engine('nzb', oldNzb));
assert.equal(tab(html, 'Failed'), 2, 'old failed torrent + NZB jobs count immediately');
assert.equal(tab(html, 'Completed'), 15 + 2, 'old finished NZB jobs count beside the paged history');
assert.equal(tab(html, 'Downloading'), 1); assert.equal(tab(html, 'Queued'), 1);
assert.equal(tab(html, 'All'), 15 + 6);
assert.ok(html.indexOf('aria-label="Activity summary"')<html.indexOf('aria-label="Activity status"'));assert.ok(html.indexOf('aria-label="Activity status"')<html.indexOf('data-optimisation-fixture'));assert.ok(html.indexOf('data-optimisation-fixture')<html.indexOf('aria-label="Completed activity"'),'optimisation appears after summary/toolbar and before Completed');assert.ok(html.includes('99 separate optimisation jobs'));assert.equal(tab(html,'All'),21,'optimisation jobs never enter download counts');
for(const section of ['In progress','Waiting','Needs attention','Completed'])assert.ok(html.includes('aria-label="'+section+' activity"'),section+' group remains reachable in the queue');assert.ok(html.includes('aria-label="Activity summary"'));
assert.ok(html.includes('Change metadata'), 'Change metadata is reachable without paging unrelated history');
for (const id of ['nzbn1', 'nzbn2', 'nzbn3', 'nzbn4', 'torrentt1', 'torrentt2']) assert.equal(html.split('data-job="' + id + '"').length, 2, id + ' appears exactly once');
// Active jobs stay pinned above older history.
assert.ok(html.indexOf('data-job="nzbn4"') < html.indexOf('Video 0') && html.indexOf('data-job="torrentt2"') < html.indexOf('Video 0'));
// More history loaded changes nothing for the engine jobs (no duplicates, same counts for them).
const full = render(history(15, false), engine('torrent', oldTorrent), engine('nzb', oldNzb));
assert.equal(tab(full, 'Failed'), 2); assert.equal(full.split('data-job="nzbn1"').length, 2);
const processing = history(15);
processing.active = [
 {kind:'youtube',item:{id:'encoding',title:'Encoding video',state:'downloading',phase:'encoding',progress:99}},
 {kind:'discover',item:{id:'placement',title:'Awaiting placement',state:'verifying'}},
 {kind:'youtube',item:{id:'finished',title:'Finished encoding',state:'complete',phase:'encoding'}},
];
const phases = render(processing,engine('torrent',oldTorrent),engine('nzb',oldNzb));
assert.equal(tab(phases,'Processing'),2,'real post-download encoding and placement have their own state');
assert.equal(tab(phases,'Downloading'),1);assert.equal(tab(phases,'Completed'),18,'a completed encoding job is completed');
assert.equal(tab(phases,'All'),24,'state tabs partition the complete merged feed');
// Native NZB post-download stages use the same real display helper as their
// card badges, so status counts and filtering cannot lag behind publication.
const nzbSource={};
vm.runInNewContext(ts.transpileModule(fs.readFileSync(__dirname+'/src/NzbDownloads.tsx','utf8'),{compilerOptions:{module:ts.ModuleKind.CommonJS,jsx:ts.JsxEmit.ReactJSX}}).outputText,{exports:nzbSource,require:()=>generic});
const nzbStages=['assembling','repairing','unpacking','extracting','processing','publishing'].map((progress_stage,index)=>({id:'stage-'+index,state:'downloading',progress:1,progress_stage,added:now}));
const stagedJobs=[...nzbStages,{id:'fetch',state:'downloading',progress:.4,progress_stage:'downloading',added:now},{id:'failed-publish',state:'failed',progress:1,progress_stage:'publishing',added:now},{id:'paused-publish',state:'paused',progress:1,progress_stage:'publishing',added:now},{id:'complete-publish',state:'stopped',progress:1,progress_stage:'publishing',added:now}];
const stageEngine={...engine('nzb',stagedJobs),stateOf:job=>nzbSource.nzbActivityStatus(job).state};
for(const phone of [false,true]){
 const controls={filter:'all'};
 let staged=render(history(0,false),engine('torrent',[]),stageEngine,phone,controls);
 assert.equal(tab(staged,'Processing'),6);assert.equal(tab(staged,'Downloading'),1);assert.equal(tab(staged,'Queued'),1);assert.equal(tab(staged,'Failed'),1);assert.equal(tab(staged,'Completed'),1);assert.equal(tab(staged,'All'),10);
 controls.select.onChange({currentTarget:{value:'processing'}});
 staged=render(history(0,false),engine('torrent',[]),stageEngine,phone,controls);
 for(const job of nzbStages)assert.ok(staged.includes('data-job="nzb'+job.id+'"'));
 for(const id of ['fetch','failed-publish','paused-publish','complete-publish'])assert.ok(!staged.includes('data-job="nzb'+id+'"'),'non-processing state retains its own partition');
}
for(const phone of [false,true]){
 const controls={};
 let rendered=render(processing,engine('torrent',oldTorrent),engine('nzb',oldNzb),phone,controls);
 assert.match(rendered,/<input[^>]*aria-label="Activity status"[^>]*role="combobox"/,'all widths use the same styled Mantine status dropdown');assert.ok(!rendered.includes('<select'),'OS-rendered menus are absent');
 assert.ok(!rendered.includes('aria-label="Filter activity"'),'the former filter button group is absent');
 for(const [id,label,count] of [['all','All',24],['downloading','Downloading',1],['processing','Processing',2],['queued','Queued',1],['waiting','Waiting for release',0],['failed','Failed',2],['completed','Completed',18]]){
  assert.equal(choicesByMarkup.get(rendered).find(row=>row.value===id).label,label+' ('+count+')','styled options retain the same global status count');
 }
 assert.equal(controls.select.style.width,220);assert.equal(controls.select.style.flex,'0 1 220px');assert.equal(controls.select.style.minWidth,0,'dropdown can shrink beside the 44px overflow menu');
 controls.select.onChange({currentTarget:{value:'processing'}});
 rendered=render(processing,engine('torrent',oldTorrent),engine('nzb',oldNzb),phone,controls);
 assert.match(rendered,/value="Processing \(2\)"/);
 assert.ok(rendered.includes('Encoding video')&&rendered.includes('Awaiting placement'),'native event selects the real post-download processing partition');
 assert.ok(!rendered.includes('Video 0')&&!rendered.includes('data-job="nzbn4"'),'other states are excluded from the selected list');
 assert.equal(tab(rendered,'Completed'),18,'filtering does not change overall status counts');
 controls.select.onChange({currentTarget:{value:'failed'}});
 rendered=render(processing,engine('torrent',oldTorrent),engine('nzb',oldNzb),phone,controls);
 assert.ok(rendered.includes('data-job="nzbn3"')&&rendered.includes('data-job="torrentt1"'),'failed engine jobs remain immediately reachable through the universal selector');
 assert.ok(!rendered.includes('Encoding video'));
}
// Source guard: the paged-history date gate is gone.
assert.ok(!/oldest/.test(fs.readFileSync(__dirname + '/src/LibraryPages.tsx', 'utf8')));

// One source acquisition owns one row, using its stored transport identity.
for(const kind of ['torrent','nzb','youtube','vod']){
 const source=kind==='vod'?'m3u':kind,id='exact-job',request={kind:'discover',item:{id:'request',engine:kind,engine_id:id,title:'Request representation',state:'downloading',mediaKind:'movie',created:now}};
 const linked=history(0,false);linked.items=[request];linked.active=[request];
 const job={id,state:'downloading',progress:.25,added:now};
 if(kind==='youtube')linked.items.push({kind:'youtube',item:{...job,title:'Transport representation'}});
 if(kind==='vod')linked.items.push({kind:'m3u',item:{...job,title:'Transport representation',kind:'movie',source:'source',path:'file.strm',description:''}});
 const merged=render(linked,engine('torrent',kind==='torrent'?[job]:[]),engine('nzb',kind==='nzb'?[job]:[]));
 assert.equal(tab(merged,'All'),1,kind+' request/transport count as one Activity');
 assert.equal(tab(merged,'Downloading'),1,kind+' uses the transport displayed state');
 assert.ok(!merged.includes('Request representation'),'the linked Discover row is absent when the exact transport exists');
 if(kind==='torrent'||kind==='nzb')assert.ok(merged.includes('data-job="'+kind+id+'"'),'the actual native card owns controls');
 const fallback=history(0,false);fallback.items=[request];
 const unavailable=render(fallback,engine('torrent',[]),engine('nzb',[]));
 assert.equal(tab(unavailable,'All'),1);assert.ok(unavailable.includes('Request representation'),'missing '+source+' snapshot retains the request fallback');
}
const identify=(kind,id,extra={})=>({identity:kind+':'+id,data:{kind,item:{id,...extra}}});
const native=identify('nzb','job'),request=identify('discover','request',{engine:'nzb',engine_id:'job'});
const distinct=render.distinctActivityJobs;
assert.deepEqual(Array.from(distinct([request,native]),item=>item.identity),['nzb:job']);
assert.deepEqual(Array.from(distinct([request,identify('torrent','job')]),item=>item.identity),['discover:request','torrent:job'],'same job id in a different engine cannot merge');
assert.equal(distinct([request,identify('nzb','other',{title:'Same displayed title'})]).length,2,'names cannot merge different stored IDs');
assert.equal(distinct([identify('discover','legacy',{engine:'nzb'}),native]).length,2,'legacy missing links remain visible');
assert.equal(distinct([identify('discover','archive:1',{archived:true,engine:'nzb',engine_id:'job'}),native]).length,2,'archives are independently retained');
assert.equal(distinct([request,identify('nzb','archive:2',{archived:true})]).length,2,'an archived transport cannot hide a current request');
// A failed old file is not the current task when its owner is already trying
// another release. The request keeps one visible row until a live child exists.
for(const engineKind of ['nzb','torrent','youtube','vod'])for(const parentState of ['waiting','searching','starting'])for(const failedState of ['failed','error','cancelled','expired']){
 const childKind=engineKind==='vod'?'m3u':engineKind;
 const parent=identify('discover','retry-request',{engine:engineKind,engine_id:'old-job',state:parentState});
 const child={...identify(childKind,'old-job',{state:failedState}),state:failedState};
 assert.deepEqual(Array.from(distinct([parent,child]),row=>row.identity),['discover:retry-request'],engineKind+' '+failedState+' child yields to '+parentState+' parent');
 for(const liveState of ['queued','downloading','paused','complete']){
  const live={...child,state:liveState};assert.deepEqual(Array.from(distinct([parent,live]),row=>row.identity),[childKind+':old-job'],'live/successful transport keeps its real controls');
 }
}
const retryFeed=history(0,false);retryFeed.items=[{kind:'discover',item:{id:'retry-request',title:'Trying the next release',state:'waiting',engine:'nzb',engine_id:'failed-job',message:'Trying another release...',created:now}}];retryFeed.active=[retryFeed.items[0]];
let retryHtml=render(retryFeed,engine('torrent',[]),engine('nzb',[{id:'failed-job',state:'failed',progress:0,added:now-1}]));
assert.equal(tab(retryHtml,'All'),1);assert.equal(tab(retryHtml,'Waiting for release'),1);assert.equal(tab(retryHtml,'Failed'),0);assert.ok(retryHtml.includes('Trying the next release'));assert.ok(!retryHtml.includes('data-job="nzbfailed-job"'),'failed old child does not mask the live retry');
retryHtml=render(retryFeed,engine('torrent',[]),engine('nzb',[{id:'failed-job',state:'downloading',progress:.1,added:now}]));
assert.equal(tab(retryHtml,'All'),1);assert.equal(tab(retryHtml,'Downloading'),1);assert.ok(retryHtml.includes('data-job="nzbfailed-job"'),'a live transport becomes the representative again');
const choiceParent=identify('discover','choice-request',{state:'failed',engine:'nzb',engine_id:'job',sourceChoice:{requestId:'choice-request',attemptKey:'exact-failed-attempt',eligible:true}});
assert.deepEqual(Array.from(distinct([choiceParent,{...native,state:'failed'}]),row=>row.identity),['discover:choice-request'],'manual source choice stays reachable over the exact failed native child');
assert.deepEqual(Array.from(distinct([choiceParent,{...native,state:'downloading'}]),row=>row.identity),['nzb:job'],'a stale source-choice marker cannot hide a live native transfer');
console.log('Activity: old engine jobs appear immediately with correct counts, Change metadata, pinning and no duplicates.');

// Retention is a successful policy outcome, while unrelated expiry/failure
// keeps its original attention status and retry controls.
const cleanupMessage='Removed automatically: outside this channel’s download range.';
const retention=history(0,false);
retention.items=[
 {kind:'youtube',item:{id:'range-cleanup',title:'Range cleanup',state:'expired',message:cleanupMessage,updated:now}},
 {kind:'youtube',item:{id:'range-ascii',title:'Legacy range cleanup',state:'expired',message:cleanupMessage.replace('’',"'"),updated:now}},
 {kind:'youtube',item:{id:'unknown-expiry',title:'Unknown expiry',state:'expired',message:'An unrecognised expiry reason',updated:now}},
 {kind:'youtube',item:{id:'failed-cleanup',title:'Failed cleanup',state:'failed',message:cleanupMessage,updated:now}},
 {kind:'youtube',item:{id:'cancelled',title:'Cancelled download',state:'cancelled',message:'Download cancelled.',updated:now}},
 {kind:'m3u',item:{id:'other-source',title:'Other source expiry',state:'expired',message:cleanupMessage,updated:now}},
];
const originalRetention=JSON.stringify(retention.items);
const readable=value=>typeof value==='string'?value:Array.isArray(value)?value.map(readable).join(''):value&&typeof value==='object'?readable(value.props?.children):'';
for(const phone of [false,true]){
 const controls={filter:'all'};
 let output=render(retention,engine('torrent',[]),engine('nzb',[]),phone,controls);
 assert.equal(tab(output,'All'),6);assert.equal(tab(output,'Completed'),2);assert.equal(tab(output,'Failed'),4);
 for(const row of render.lastRows){
  const state=row.badge.props.state,actions=readable(row.actions);
  if(state==='removed'){
   assert.equal(row.statusColour,'green');assert.ok(!actions.includes('Retry'),'expected cleanup offers no retry');
   assert.ok(readable(row.details).includes('Removed automatically: outside this channel'),'cleanup reason remains in Details');
  }else assert.ok(actions.includes('Retry'),'other expiry, failure and cancellation retain Retry');
 }
 assert.equal(render.lastRows.filter(row=>row.badge.props.state==='removed').length,2);
 controls.select.onChange({currentTarget:{value:'completed'}});
 output=render(retention,engine('torrent',[]),engine('nzb',[]),phone,controls);
 assert.equal(render.lastRows.length,2);assert.ok(render.lastRows.every(row=>row.badge.props.state==='removed'));
 assert.ok(output.includes('aria-label="Completed activity"'));assert.ok(!output.includes('aria-label="Needs attention activity"'));
 controls.select.onChange({currentTarget:{value:'failed'}});
 output=render(retention,engine('torrent',[]),engine('nzb',[]),phone,controls);
 assert.equal(render.lastRows.length,4);assert.ok(render.lastRows.every(row=>row.badge.props.state!=='removed'));
 assert.ok(output.includes('aria-label="Needs attention activity"'));assert.ok(!output.includes('aria-label="Completed activity"'));
}
assert.equal(JSON.stringify(retention.items),originalRetention,'display classification never rewrites raw expired history');
console.log('Retention Activity: Completed counts/filter, green Removed status and no Retry; unrelated expiry/failure/cancellation and raw history preserved.');
