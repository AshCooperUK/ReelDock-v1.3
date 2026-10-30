const assert=require('node:assert/strict'),fs=require('node:fs'),vm=require('node:vm'),ts=require('typescript');
const React=require('react'),M=require('@mantine/core'),{renderToStaticMarkup}=require('react-dom/server');
const clear=require('./activity-clear-test-helper.cjs')(),h=React.createElement;
const source=fs.readFileSync(__dirname+'/src/LibraryPages.tsx','utf8');
const compile=(file,imports={})=>{const out={};vm.runInNewContext(ts.transpileModule(fs.readFileSync(__dirname+'/src/'+file,'utf8'),{compilerOptions:{module:ts.ModuleKind.CommonJS,target:ts.ScriptTarget.ES2022,jsx:ts.JsxEmit.ReactJSX,esModuleInterop:true}}).outputText,{exports:out,Error,AbortController,require:name=>Object.hasOwn(imports,name)?imports[name]:require(name)});return out;};
const ui=compile('components/ui.tsx',{'../notifications-state':{registerNotificationHost:()=>()=>{}},'../ScrollToTop':{default:()=>null}});
const warm=compile('warmSnapshots.ts'),status=compile('components/ActivityStatus.tsx'),activityGroup=compile('components/ActivityGroup.tsx');
const nodes=value=>Array.isArray(value)?value.flatMap(nodes):value&&typeof value==='object'?[value,...nodes(value.props?.children),...nodes(value.props?.clearAction),...nodes(value.props?.actions)]:[];
const text=value=>typeof value==='string'?value:Array.isArray(value)?value.map(text).join(''):value&&typeof value==='object'?text(value.props?.children):'';
const turn=()=>new Promise(setImmediate),deferred=()=>{let resolve,reject;const promise=new Promise((yes,no)=>{resolve=yes;reject=no;});return{promise,resolve,reject};};
const observation=(state,generation=0,attempt='a'.repeat(64))=>({state,generation,attempt});
const serverItem=item=>({...item,activity_observation:observation(item.state||'failed',item.activity_generation||0)});
const row=(id,state,updated=1)=>({kind:'youtube',item:serverItem({id,title:id,state,created:1,updated,message:'Outcome '+id})});
const entry=(kind,id,state,generation=0)=>({id:kind+id,identity:kind+':'+id,state,pin:0,at:1,data:{kind,item:{id,state,activity_generation:generation,updated:1}}});

function screen({phone=false}={}){
 const values=[],effects=[],calls=[],exports=[],saved=[];let cursor=0,refreshes=0,torrentRefreshes=0,nzbRefreshes=0;
 const hooks={...React,useState(initial){const i=cursor++;if(!(i in values))values[i]=typeof initial==='function'?initial():initial;return[values[i],next=>values[i]=typeof next==='function'?next(values[i]):next];},useRef(initial){const i=cursor++;if(!(i in values))values[i]={current:initial};return values[i];},useEffect(fn,deps){const i=cursor++,previous=values[i];if(!previous||!deps||deps.some((value,index)=>!Object.is(value,previous.deps?.[index]))){previous?.cleanup?.();const cleanup=fn();values[i]={deps,cleanup};effects.push(cleanup);}}};
 const feed={items:[row('failed-history','failed'),row('done-history','complete')],active:[row('running-history','downloading')],activityRevision:0,clearedEngineIds:{torrent:[],nzb:[]},clearedEngineGenerations:{torrent:{},nzb:{}},busy:false,more:false,total:2,error:'',end:{current:null},refresh(){refreshes++;},reset(){refreshes++;}};
 const torrents={jobs:[{id:'failed-torrent',name:'Failed torrent',state:'failed',progress:.5,added:1,activity_generation:0},{id:'done-torrent',name:'Done torrent',state:'stopped',progress:1,added:1,activity_generation:0}],stateOf:job=>job.progress>=1?'complete':job.state==='failed'?'failed':'downloading',card:job=>h('article',{'data-job':job.id},job.name),refresh(){torrentRefreshes++;}};
 torrents.jobs=torrents.jobs.map(serverItem);
 const nzbs={jobs:[{id:'failed-nzb',name:'Failed NZB',state:'failed',progress:.1,added:1,activity_generation:0}],stateOf:job=>job.state==='failed'?'failed':'complete',card:job=>h('article',{'data-job':job.id},job.name),refresh(){nzbRefreshes++;}};
 nzbs.jobs=nzbs.jobs.map(serverItem);
 const generic=new Proxy({__esModule:true,default:()=>null},{get:(target,key)=>target[key]||(()=>null)});
 const imports={react:hooks,'./activityClear':clear,'./warmSnapshots':warm,'./components/ActivityGroup':activityGroup,'./components/ActivityStatus':status,'@mantine/hooks':{useMediaQuery:()=>phone},'./DownloadHistory':{useDownloadHistory:()=>feed},'./TorrentDownloads':{useTorrentActivity:()=>torrents},'./NzbDownloads':{useNzbActivity:()=>nzbs},'./components/ui':{...ui,Dialog:()=>null},'./components/SelectField':{SelectField:()=>null},'./components/ListRow':{__esModule:true,default:props=>h('article',null,props.title),ActivityQueueRows:({children})=>children},'./api':{api:(...args)=>{const pending=deferred();calls.push({args,pending});return pending.promise;}},'./mediaDate':{mediaDate:value=>value},'./activityLog':{redactActivityText:value=>String(value).replace(/https?:\/\/\S+/g,'[URL omitted]'),exportActivityLog:options=>{const pending=deferred();exports.push({options,pending});return pending.promise;},saveActivityLog:value=>saved.push(value)}};
 const out={};vm.runInNewContext(ts.transpileModule(source,{compilerOptions:{module:ts.ModuleKind.CommonJS,target:ts.ScriptTarget.ES2022,jsx:ts.JsxEmit.ReactJSX}}).outputText,{exports:out,Error,AbortController,require:name=>imports[name]||(['react/jsx-runtime','@mantine/core','lucide-react'].includes(name)?require(name):generic)});
 return{feed,torrents,nzbs,calls,exports,saved,draw(){cursor=0;return out.DownloadList({csrf:'exact-csrf'});},dispose(){effects.forEach(cleanup=>cleanup?.());},refreshes:()=>[refreshes,torrentRefreshes,nzbRefreshes]};
}
const clearButton=(tree,title)=>nodes(tree).find(node=>node.props?.['aria-label']==='Clear '+title+' section');
const group=(tree,title)=>nodes(tree).find(node=>node.type===activityGroup.default&&node.props?.title===title);
const exportButton=tree=>nodes(tree).find(node=>node.props?.onClick&&text(node)==='Export log (.txt)');
const itemRow=(tree,kind,id)=>nodes(tree).find(node=>node.props?.clearAction&&(node.key===kind+id||node.props['data-job']===id));
const itemClear=(tree,kind,id)=>itemRow(tree,kind,id)?.props.clearAction;

(async()=>{
 for(const value of [undefined,null,[],{},observation('',0),observation('failed',-1),observation('failed',1.2),observation('failed',Number.MAX_SAFE_INTEGER+1),observation('failed','1'),observation('failed',0,'short'),observation('failed',0,'z'.repeat(64))]){
  const invalidEntry=entry('nzb','job','failed');invalidEntry.data.item.activity_observation=value;
  assert.equal(clear.captureActivityClear(invalidEntry),null,'missing or malformed server observations cannot become a Clear action');
 }
 const capturedEntry=entry('nzb','job','failed');capturedEntry.data.item.activity_observation={...observation('failed',7),privateSource:'not forwarded'};
 const captured=clear.captureActivityClear(capturedEntry);assert.deepEqual(JSON.parse(JSON.stringify(captured.observed)),observation('failed',7));
 capturedEntry.data.item.activity_observation.state='downloading';capturedEntry.data.item.activity_observation.attempt='b'.repeat(64);
 assert.deepEqual(JSON.parse(JSON.stringify(captured.observed)),observation('failed',7),'capture copies the stamp rather than retaining a mutable item reference');
 const visibility={activityRevision:1,clearedEngineIds:{torrent:['job'],nzb:['job']},clearedEngineGenerations:{torrent:{job:2},nzb:{job:2}}};
 for(const kind of ['torrent','nzb']){
  for(const bucket of ['failed','completed'])assert.equal(clear.activityEntryCleared(entry(kind,'job','failed',2),bucket,visibility),true);
  for(const bucket of ['downloading','processing','queued','waiting'])assert.equal(clear.activityEntryCleared(entry(kind,'job','queued',2),bucket,visibility),false,'active/queued/paused source jobs can never be hidden by terminal archive markers');
  for(const state of ['paused','seeding','downloading','queued','choose']){
   const active=entry(kind,'job','complete',2);active.data.item.state=state;active.data.item.progress=1;
   const pending={ok:true,section:'completed',cleared:1,activityRevision:2,...visibility,signatures:new Set([clear.activityClearSignature(active)])};pending.activityRevision=2;
   assert.equal(clear.activityEntryCleared(active,'completed',visibility,pending),false,'raw native '+state+' retains its controls even if progress normalizes its badge to Completed');
  }
  assert.equal(clear.activityEntryCleared(entry(kind,'job','failed',3),'failed',visibility),false,'a future failed retry generation reappears even if no active phase was polled');
  assert.equal(clear.activityEntryCleared(entry(kind,'job','queued',3),'queued',{...visibility,suppressedActivityIds:{[kind]:['job']}}),true,'an explicit per-item hold suppresses the exact active id before the ordinary section guards');
  assert.equal(clear.activityEntryCleared(entry(kind,'other','queued',3),'queued',{...visibility,suppressedActivityIds:{[kind]:['job']}}),false,'per-item suppression cannot hide unrelated work');
 }
 assert.equal(clear.activityEntryCleared(entry('torrent','job','complete'),'completed',{clearedEngineIds:{torrent:['job'],nzb:[]}}),true,'legacy generations default to zero');

 const s=screen();let tree=s.draw();assert.ok(clearButton(tree,'Needs attention'));assert.ok(clearButton(tree,'Completed'));
 assert.equal(nodes(tree).filter(node=>/^Clear .* section$/.test(node.props?.['aria-label']||'')).length,2);
 for(const title of ['Needs attention','Completed']){
  const button=clearButton(tree,title);assert.equal(button.type,ui.Button);assert.equal(button.props.style.minHeight,44);assert.match(button.props.title,/keep entries in TXT exports/);
  const section=group(tree,title),sectionHtml=renderToStaticMarkup(h(M.MantineProvider,{forceColorScheme:'dark'},section));assert.match(sectionHtml,/--group-align:center;--group-justify:space-between;--group-wrap:wrap/,'the real group header wraps section actions with centered alignment');
  for(const scheme of ['dark','light'])for(const width of [320,768,1440]){
   const html=renderToStaticMarkup(h(M.MantineProvider,{forceColorScheme:scheme},h('div',{style:{width}},button)));
   assert.match(html,/min-height:44px/);assert.match(html,/type="button"/);assert.match(html,new RegExp('aria-label="Clear '+title+' section"'));
  }
 }
 const attention=clearButton(tree,'Needs attention'),completed=clearButton(tree,'Completed');attention.props.onClick();attention.props.onClick();completed.props.onClick();
 assert.equal(s.calls.length,1,'immediate double-click/other section is coalesced before React commits');
 assert.equal(s.calls[0].args[0],'/api/activity/clear');assert.deepEqual(JSON.parse(JSON.stringify(s.calls[0].args[2])),{section:'attention'});assert.equal(s.calls[0].args[3],'exact-csrf');assert.equal(s.calls[0].args[4].feedback,'inline');
 tree=s.draw();assert.equal(clearButton(tree,'Completed').props.disabled,true);exportButton(tree).props.onClick();assert.equal(s.exports.length,0,'a clear cannot overlap an export snapshot');
 const acknowledgement={ok:true,section:'attention',cleared:3,activityRevision:1,clearedEngineIds:{torrent:['failed-torrent'],nzb:['failed-nzb']},clearedEngineGenerations:{torrent:{'failed-torrent':0},nzb:{'failed-nzb':0}}};
 s.calls[0].pending.resolve(acknowledgement);await turn();tree=s.draw();assert.equal(group(tree,'Needs attention'),undefined,'acknowledged rows disappear immediately while the old feed read is still held');assert.ok(group(tree,'Completed'));assert.ok(group(tree,'In progress'));
 assert.deepEqual(s.refreshes(),[1,1,1]);assert.match(text(tree),/Entries remain in TXT exports/);
 s.feed.items=[row('done-history','complete')];Object.assign(s.feed,acknowledgement);tree=s.draw();assert.equal(group(tree,'Needs attention'),undefined,'the durable server revision/map takes over the temporary read-race guard');
 s.torrents.jobs[0]={...s.torrents.jobs[0],activity_generation:1};tree=s.draw();assert.ok(group(tree,'Needs attention'),'future failures from the same engine id reappear normally');
 s.feed.items.push(row('failed-history','failed',2));tree=s.draw();assert.ok(group(tree,'Needs attention'),'a future SQL outcome with the same id is not blocked by client signatures');
 exportButton(tree).props.onClick();assert.equal(s.exports.length,1);const snapshot=s.exports[0].options;
 assert.ok(snapshot.entries.some(value=>value.identity==='torrent:failed-torrent'),'new retry generation remains in export snapshot');assert.ok(!snapshot.entries.some(value=>value.identity==='nzb:failed-nzb'),'cleared native snapshot is exported from the archive only, without a live duplicate');
 const archived={kind:'torrent',item:{id:'archive:1',originalId:'failed-torrent',archived:true,state:'stopped',progress:1,added:3}};
 const normalised=snapshot.historyEntry(archived);assert.equal(normalised.state,'complete');assert.equal(normalised.identity,'torrent:archive:1');assert.equal(normalised.node,null);assert.equal(normalised.at,3);
 clearButton(s.draw(),'Completed').props.onClick();assert.equal(s.calls.length,1,'export prevents an overlapping clear');s.exports[0].pending.resolve({text:'Full archived log',filename:'fixture.txt',count:4});await turn();assert.equal(s.saved.length,1);s.dispose();

 const both=screen();clearButton(both.draw(),'Needs attention').props.onClick();both.calls[0].pending.resolve(acknowledgement);await turn();tree=both.draw();clearButton(tree,'Completed').props.onClick();both.calls[1].pending.resolve({...acknowledgement,section:'completed',cleared:2,activityRevision:2,clearedEngineIds:{torrent:['failed-torrent','done-torrent'],nzb:['failed-nzb']},clearedEngineGenerations:{torrent:{'failed-torrent':0,'done-torrent':0},nzb:{'failed-nzb':0}}});await turn();tree=both.draw();assert.equal(group(tree,'Needs attention'),undefined);assert.equal(group(tree,'Completed'),undefined,'a second clear before feed revalidation cannot resurrect the first section');assert.ok(group(tree,'In progress'));both.dispose();
 const failure=screen();clearButton(failure.draw(),'Completed').props.onClick();failure.calls[0].pending.reject(Error('Clear failed https://private.invalid/secret'));await turn();tree=failure.draw();assert.ok(group(tree,'Completed'));assert.equal(clearButton(tree,'Completed').props.disabled,false);assert.ok(!nodes(tree).find(node=>node.props?.kind==='error').props.message.includes('private.invalid'));assert.equal(failure.calls.length,1,'no failed POST is retried automatically');failure.dispose();
 const invalid=screen();clearButton(invalid.draw(),'Needs attention').props.onClick();invalid.calls[0].pending.resolve({ok:true,section:'completed',cleared:1,activityRevision:1});await turn();tree=invalid.draw();assert.ok(group(tree,'Needs attention'));assert.match(nodes(tree).find(node=>node.props?.kind==='error').props.message,/could not be confirmed/);assert.equal(invalid.calls.length,1);invalid.dispose();
 const retired=screen();clearButton(retired.draw(),'Needs attention').props.onClick();retired.dispose();assert.equal(retired.calls[0].args[1].aborted,true);retired.calls[0].pending.resolve(acknowledgement);await turn();assert.deepEqual(retired.refreshes(),[0,0,0],'unmount aborts a caller and late acknowledgement cannot update another screen');

 const single=screen();tree=single.draw();const activeClear=itemClear(tree,'youtube','running-history');assert.ok(activeClear);assert.equal(activeClear.props.children,'Clear');assert.match(activeClear.props['aria-label'],/stop automatic retries/);assert.match(activeClear.props.title,/explicit Retry or Resume/);activeClear.props.onClick();activeClear.props.onClick();clearButton(tree,'Completed').props.onClick();assert.equal(single.calls.length,1);
 assert.equal(single.calls[0].args[0],'/api/activity/clear-item');assert.deepEqual(JSON.parse(JSON.stringify(single.calls[0].args[2])),{kind:'youtube',id:'running-history',observed:observation('downloading')},'the exact captured server attempt is sent with source kind/id; the server resolves media/cancel identities');assert.equal(single.calls[0].args[3],'exact-csrf');assert.equal(single.calls[0].args[4].feedback,'inline');
 tree=single.draw();assert.equal(itemClear(tree,'youtube','running-history').props.children,'Clearing…');assert.equal(itemRow(tree,'torrent','failed-torrent').props.actionsDisabled,true);assert.ok(nodes(tree).filter(node=>node.props?.['aria-label']?.startsWith('Clear ')).every(node=>node.props.disabled));
 const held={ok:true,kind:'youtube',id:'running-history',cleared:1,activityRevision:1,held:true,stopping:true,suppressedActivityIds:{youtube:['running-history'],torrent:['failed-torrent']},clearedEngineIds:{torrent:[],nzb:[]},clearedEngineGenerations:{torrent:{},nzb:{}}};
 single.calls[0].pending.resolve(held);await turn();tree=single.draw();assert.equal(itemClear(tree,'youtube','running-history'),undefined,'accepted active Clear disappears before its held old feed read completes');assert.equal(itemClear(tree,'torrent','failed-torrent'),undefined,'server-reported linked work is suppressed immediately');assert.ok(itemClear(tree,'nzb','failed-nzb'));assert.match(text(tree),/Related work is stopping; automatic retries are blocked/);assert.deepEqual(single.refreshes(),[1,1,1]);
 Object.assign(single.feed,held);tree=single.draw();assert.equal(itemClear(tree,'youtube','running-history'),undefined);single.torrents.jobs[0]={...single.torrents.jobs[0],activity_generation:99};tree=single.draw();assert.equal(itemClear(tree,'torrent','failed-torrent'),undefined,'a durable per-item hold is not bypassed by a new automatic generation');
 single.feed.suppressedActivityIds={};single.feed.activityRevision=2;tree=single.draw();assert.ok(itemClear(tree,'torrent','failed-torrent'),'only a later authoritative server release permits the held identity to return');single.dispose();

 const log=screen();log.feed.items.push({kind:'event',item:serverItem({id:7,title:'Log-only event',state:'error',subscription:'QA',finished:2,stats:{}})});tree=log.draw();const logClear=itemClear(tree,'event',7);assert.ok(logClear);assert.equal(logClear.props['aria-label'],'Clear activity');assert.ok(!logClear.props.title.includes('retries'));logClear.props.onClick();assert.deepEqual(JSON.parse(JSON.stringify(log.calls[0].args[2])),{kind:'event',id:7,observed:observation('error')});log.calls[0].pending.resolve({ok:true,kind:'event',id:'7',cleared:1,activityRevision:1,held:false,stopping:false,suppressedActivityIds:{event:['7']}});await turn();tree=log.draw();assert.equal(itemClear(tree,'event',7),undefined);assert.match(text(tree),/Activity cleared; kept in TXT exports/);log.dispose();
 for(const fixture of [
  {kind:'m3u',item:{id:'server-job',title:'Server movie',state:'failed',kind:'movies',source:'PRIVATE_SOURCE',path:'PRIVATE_PATH',created:1}},
  {kind:'discover',item:{id:'request-job',title:'Movie request',state:'failed',mediaKind:'movie',created:1}},
  {kind:'m3u_sync',item:{id:8,title:'Sync record',state:'complete',subscription:'QA',finished:1,stats:{}}},
  {kind:'torrent',item:{id:'failed-torrent'}},{kind:'nzb',item:{id:'failed-nzb'}},
 ]){
  const view=screen();if(!['torrent','nzb'].includes(fixture.kind))view.feed.items.push({...fixture,item:serverItem(fixture.item)});
  const control=itemClear(view.draw(),fixture.kind,fixture.item.id);assert.ok(control,fixture.kind+' has the same visible row Clear control');control.props.onClick();assert.deepEqual(JSON.parse(JSON.stringify(view.calls[0].args[2])),{kind:fixture.kind,id:fixture.item.id,observed:observation(fixture.item.state||'failed')});assert.ok(!JSON.stringify(view.calls[0].args[2]).includes('PRIVATE_'));
  view.calls[0].pending.resolve({ok:true,kind:fixture.kind,id:fixture.item.id,cleared:1,activityRevision:1,held:fixture.kind!=='m3u_sync',stopping:false,suppressedActivityIds:{[fixture.kind]:[String(fixture.item.id)]}});await turn();tree=view.draw();assert.equal(itemClear(tree,fixture.kind,fixture.item.id),undefined);assert.match(text(tree),fixture.kind==='m3u_sync'?/kept in TXT exports/:/automatic retries blocked/);view.dispose();
 }

 // A linked request is hidden only in the display; native callbacks, exact
 // Clear identity, raw TXT snapshot and clear-section race guards all survive.
 for(const state of ['downloading','complete']){
  const linked=screen(),job={id:'actual-nzb-job',name:'Native source',state:state==='complete'?'stopped':'downloading',progress:state==='complete'?1:.25,added:1,activity_generation:0,activity_observation:observation(state==='complete'?'stopped':'downloading')};
  const request={kind:'discover',item:{id:'linked-request',title:'Request alias',state,engine:'nzb',engine_id:job.id,mediaKind:'movie',created:1}};
  linked.feed.items=[request];linked.feed.active=[];linked.torrents.jobs=[];linked.nzbs.jobs=[job];linked.nzbs.stateOf=()=>state;
  let pauses=0;linked.nzbs.card=item=>h('article',{'data-job':item.id},h(ui.Button,{'aria-label':'Pause actual job',onClick:()=>pauses++},'Pause'));
  tree=linked.draw();assert.ok(itemRow(tree,'nzb',job.id));assert.equal(itemRow(tree,'discover',request.item.id),undefined,'one native row replaces its exact request alias');
  const pause=nodes(tree).find(node=>node.props?.['aria-label']==='Pause actual job');pause.props.onClick();assert.equal(pauses,1,'the native transport callback remains reachable');
  exportButton(tree).props.onClick();assert.deepEqual(Array.from(linked.exports[0].options.entries,entry=>entry.identity).sort(),['discover:linked-request','nzb:actual-nzb-job'],'TXT snapshot keeps both original source records');linked.exports[0].pending.resolve({text:'Both original records',filename:'fixture.txt',count:2});await turn();tree=linked.draw();
  if(state==='complete'){
   clearButton(tree,'Completed').props.onClick();linked.calls[0].pending.resolve({ok:true,section:'completed',cleared:2,activityRevision:1,clearedEngineGenerations:{torrent:{},nzb:{[job.id]:0}}});await turn();
   assert.equal(group(linked.draw(),'Completed'),undefined,'a held old request frame cannot resurrect after its native row is section-cleared');
  }else{
   itemClear(tree,'nzb',job.id).props.onClick();assert.deepEqual(JSON.parse(JSON.stringify(linked.calls[0].args[2])),{kind:'nzb',id:job.id,observed:job.activity_observation});
   linked.calls[0].pending.resolve({ok:true,kind:'nzb',id:job.id,held:true,stopping:true,cleared:2,activityRevision:1,suppressedActivityIds:{nzb:[job.id],discover:[request.item.id]}});await turn();
   assert.equal(itemRow(linked.draw(),'discover',request.item.id),undefined);assert.equal(itemRow(linked.draw(),'nzb',job.id),undefined,'linked Clear suppresses both source snapshots immediately');
  }
  linked.dispose();
 }
 const alternatives=screen(),sourceMarker={requestId:'source-request',attemptKey:'failed-selected-attempt',eligible:true};
 const sourceRequest={kind:'discover',item:{id:sourceMarker.requestId,title:'Failed selected release',state:'failed',engine:'nzb',engine_id:'failed-child',sourceChoice:sourceMarker,created:1}};
 alternatives.feed.items=[sourceRequest];alternatives.feed.active=[];alternatives.torrents.jobs=[];alternatives.nzbs.jobs=[{id:'failed-child',name:'Failed selected release',state:'failed',progress:0,activity_generation:0}];
 tree=alternatives.draw();const parent=itemRow(tree,'discover',sourceMarker.requestId);assert.ok(parent,'a source-choice parent represents its failed child');assert.equal(itemRow(tree,'nzb','failed-child'),undefined);
 assert.match(text(parent.props.contextAction),/Find alternatives/,'the entry point is a visible button outside the action menu');assert.equal(parent.props.contextAction.props.style.maxWidth,'100%');
 let modal=nodes(tree).find(node=>node.props?.autoChoices);assert.deepEqual(JSON.parse(JSON.stringify(modal.props.autoChoices)),[sourceMarker]);assert.equal(modal.props.allowAuto,true);assert.equal(alternatives.calls.length,0,'mounting the lazy controller does not make Activity post or search');
 alternatives.nzbs.dialog=h(ui.Dialog,{open:true});modal=nodes(alternatives.draw()).find(node=>node.props?.autoChoices);assert.equal(modal.props.allowAuto,false,'an existing native detail blocks automatic alternatives');alternatives.nzbs.dialog=null;
 parent.props.contextAction.props.onClick();modal=nodes(alternatives.draw()).find(node=>node.props?.autoChoices);assert.deepEqual(JSON.parse(JSON.stringify(modal.props.choice)),sourceMarker);
 Object.assign(alternatives.feed,{activityRevision:1,suppressedActivityIds:{discover:[sourceMarker.requestId],nzb:['failed-child']}});tree=alternatives.draw();tree=alternatives.draw();assert.equal(nodes(tree).find(node=>node.props?.autoChoices),undefined,'cleared/held entries cannot open or retain the source popup');
 alternatives.feed.suppressedActivityIds={};sourceRequest.item.sourceChoice={...sourceMarker,eligible:false};assert.equal(nodes(alternatives.draw()).find(node=>node.props?.autoChoices),undefined,'ineligible markers cannot prompt');alternatives.dispose();

 // A retained handler belongs to the old failure, even after a same-parent
 // replacement arrives and even if a caller mutates the old item object.
 const stale=screen(),oldItem=serverItem({id:'same-parent',title:'Old failed attempt',state:'failed',engine:'nzb',engine_id:'old-native',mediaKind:'movie',created:1});
 stale.feed.items=[{kind:'discover',item:oldItem}];stale.feed.active=[];stale.torrents.jobs=[];stale.nzbs.jobs=[];
 const oldButton=itemClear(stale.draw(),'discover','same-parent'),oldObserved={...oldItem.activity_observation};
 assert.equal(oldButton.props['aria-label'],'Clear finished attempt');assert.match(oldButton.props.title,/newer replacement keeps running/);
 oldItem.activity_observation.state='downloading';oldItem.activity_observation.generation=2;oldItem.activity_observation.attempt='b'.repeat(64);
 const replacement={...oldItem,title:'Replacement running',state:'downloading',engine_id:'replacement-native',activity_observation:observation('downloading',2,'b'.repeat(64))};
 stale.feed.items=[{kind:'discover',item:replacement}];tree=stale.draw();assert.match(itemClear(tree,'discover','same-parent').props['aria-label'],/stop automatic retries/);
 oldButton.props.onClick();oldButton.props.onClick();assert.equal(stale.calls.length,1);
 assert.deepEqual(JSON.parse(JSON.stringify(stale.calls[0].args[2])),{kind:'discover',id:'same-parent',observed:oldObserved},'the old rendered callback never adopts the new poll stamp');
 stale.calls[0].pending.reject(Object.assign(Error('This Activity attempt changed. Refresh Activity before clearing.'),{status:409}));await turn();tree=stale.draw();
 assert.equal(stale.calls.length,1,'a stale attempt cannot automatically retry its destructive request');assert.deepEqual(stale.refreshes(),[1,1,1],'a rejected stale Clear revalidates every feed');
 assert.ok(itemClear(tree,'discover','same-parent'));assert.equal(text(itemRow(tree,'discover','same-parent').props.title),'Replacement running');assert.match(nodes(tree).find(node=>node.props?.kind==='error').props.message,/attempt changed/);stale.dispose();

 // An accepted terminal acknowledgement also cannot hide a new same-state
 // attempt merely because the source id and human-readable fields match.
 const renewed=screen();renewed.feed.active=[];renewed.torrents.jobs=[];renewed.nzbs.jobs=[];renewed.feed.items=[row('reused','failed')];
 itemClear(renewed.draw(),'youtube','reused').props.onClick();renewed.feed.items[0].item={...renewed.feed.items[0].item,activity_observation:observation('failed',1,'c'.repeat(64))};
 renewed.calls[0].pending.resolve({ok:true,kind:'youtube',id:'reused',cleared:1,activityRevision:1,held:false,stopping:false,clearedEngineGenerations:{torrent:{},nzb:{}}});await turn();tree=renewed.draw();
 assert.ok(itemClear(tree,'youtube','reused'),'new server attempt remains visible while old history reads are held');assert.match(text(tree),/kept in TXT exports/);renewed.dispose();

 const unstamped=screen();delete unstamped.feed.items[0].item.activity_observation;const disabledClear=itemClear(unstamped.draw(),'youtube','failed-history');
 assert.equal(disabledClear.props.disabled,true);assert.match(disabledClear.props.title,/Refresh Activity/);disabledClear.props.onClick();assert.equal(unstamped.calls.length,0,'missing observations never fall back to a client token or kind/id-only POST');assert.deepEqual(unstamped.refreshes(),[1,1,1]);unstamped.dispose();

 const itemFailure=screen();itemClear(itemFailure.draw(),'youtube','failed-history').props.onClick();itemFailure.calls[0].pending.reject(Error('Item clear failed.'));await turn();tree=itemFailure.draw();assert.ok(itemClear(tree,'youtube','failed-history'));assert.equal(itemClear(tree,'youtube','failed-history').props.disabled,false);assert.equal(itemFailure.calls.length,1,'per-item POST errors have no automatic retry');assert.deepEqual(itemFailure.refreshes(),[1,1,1]);itemFailure.dispose();
 for(const phone of [false,true])for(const scheme of ['light','dark']){
  const view=screen({phone}),button=itemClear(view.draw(),'youtube','failed-history');assert.equal(button.props.style.minHeight,44);if(phone){assert.equal(button.props.style.width,44);assert.equal(button.props.style.paddingInline,0);}
  const rows=compile('components/ListRow.tsx',{'./ActivityRow.module.css':{default:new Proxy({},{get:(_,key)=>String(key)})},'@mantine/hooks':{useMediaQuery:()=>phone}});
  const html=renderToStaticMarkup(h(M.MantineProvider,{forceColorScheme:scheme},h(rows.ActivityQueueRows,null,h(rows.default,{title:'Fixture failed item',badge:h(status.ActivityStatus,{state:'failed'}),clearAction:button,actionsDisabled:true,actions:h(ui.Button,{'aria-label':'Retry',onClick(){}},'Retry'),details:'Recorded outcome'}))));
  assert.equal((html.match(/aria-label="Clear finished attempt"/g)||[]).length,1,'the real ListRow always renders Clear visibly rather than only in a closed menu');assert.match(html,/min-height:44px/);if(phone)assert.match(html,/width:44px/);else assert.match(html,/<button(?=[^>]*aria-label="Retry")(?=[^>]*disabled)[^>]*>/,'original actions are disabled during a conflicting clear while callbacks remain intact');view.dispose();
 }

 // Clear Logs clears finished public history only after an explicit inline
 // confirmation, through the same guarded single-write/read-race machinery.
 const logs=screen();tree=logs.draw();
 const menuItems=nodes(tree).filter(node=>node.type===M.Menu.Item),exportAt=menuItems.findIndex(node=>text(node)==='Export log (.txt)');
 assert.equal(text(menuItems[exportAt+1]),'Clear Logs','Clear Logs sits immediately below Export log');
 const logsButton=frame=>nodes(frame).find(node=>node.props?.onClick&&text(node)==='Clear Logs');
 const confirmation=frame=>nodes(frame).find(node=>node.props?.confirm==='Clear Logs'&&typeof node.props.onConfirm==='function');
 logsButton(tree).props.onClick();tree=logs.draw();assert.equal(logs.calls.length,0,'opening confirmation never clears history');
 assert.match(text(confirmation(tree)),/Active, queued and waiting downloads keep running/);confirmation(tree).props.onCancel();assert.equal(confirmation(logs.draw()),undefined);assert.equal(logs.calls.length,0);
 logsButton(logs.draw()).props.onClick();tree=logs.draw();const prompt=confirmation(tree);prompt.props.onConfirm();prompt.props.onConfirm();exportButton(tree).props.onClick();
 assert.equal(logs.calls.length,1,'a confirmed clear has one write and no automatic duplicate');assert.equal(logs.exports.length,0,'a log export cannot race its clear');
 assert.equal(logs.calls[0].args[0],'/api/activity/clear');assert.deepEqual(JSON.parse(JSON.stringify(logs.calls[0].args[2])),{section:'logs'});assert.equal(logs.calls[0].args[3],'exact-csrf');assert.equal(logs.calls[0].args[4].feedback,'inline');
 const activeBefore=JSON.stringify(logs.feed.active);tree=logs.draw();assert.equal(confirmation(tree).props.busy,true);assert.equal(logsButton(tree).props.disabled,true);
 logs.calls[0].pending.resolve({ok:true,section:'logs',cleared:99,activityRevision:1,clearedEngineGenerations:{torrent:{'failed-torrent':0,'done-torrent':0},nzb:{'failed-nzb':0}}});await turn();tree=logs.draw();
 assert.equal(confirmation(tree),undefined);assert.equal(group(tree,'Needs attention'),undefined);assert.equal(group(tree,'Completed'),undefined);assert.ok(itemClear(tree,'youtube','running-history'),'the running download keeps its controls');assert.equal(JSON.stringify(logs.feed.active),activeBefore);assert.match(text(tree),/99 finished log entries cleared from Activity and TXT exports/);assert.deepEqual(logs.refreshes(),[1,1,1]);logs.dispose();
 const failedLogs=screen();logsButton(failedLogs.draw()).props.onClick();confirmation(failedLogs.draw()).props.onConfirm();failedLogs.calls[0].pending.reject(Error('Clear failed https://private.invalid/SECRET'));await turn();tree=failedLogs.draw();assert.equal(failedLogs.calls.length,1,'failed log-clear POST is not retried');assert.ok(group(tree,'Completed'));assert.ok(group(tree,'Needs attention'));assert.ok(confirmation(tree),'failed clear keeps the explicit choice available');assert.ok(!text(tree).includes('SECRET'));failedLogs.dispose();
 console.log('Activity clear: two real Mantine 44px wrapping section controls, exact CSRF POST once, immediate/version-safe durable suppression, preserved active/future attempts, archived export normalization, feedback/errors and lifecycle passed.');
})().catch(error=>{console.error(error);process.exitCode=1;});
