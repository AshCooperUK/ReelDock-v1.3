const assert=require('node:assert/strict'),fs=require('node:fs'),vm=require('node:vm'),ts=require('typescript');
const tick=()=>new Promise(setImmediate),deferred=()=>{let resolve,reject;const promise=new Promise((yes,no)=>{resolve=yes;reject=no;});return{promise,resolve,reject};};
function hooks(){const slots=[];let cursor=0,effects=[],updates=[];const same=(a,b)=>a&&b&&a.length===b.length&&a.every((value,index)=>Object.is(value,b[index]));const react={useState(initial){const i=cursor++;if(!slots[i])slots[i]={value:typeof initial==='function'?initial():initial};return[slots[i].value,next=>updates.push(()=>slots[i].value=typeof next==='function'?next(slots[i].value):next)];},useRef(initial){return(slots[cursor++]??={value:{current:initial}}).value;},useCallback(fn,deps){const i=cursor++;if(!slots[i]||!same(slots[i].deps,deps))slots[i]={value:fn,deps};return slots[i].value;},useEffect(fn,deps){const i=cursor++;if(!slots[i]||!same(slots[i].deps,deps))effects.push({i,fn,deps});}};return{react,draw(fn){const pending=updates;updates=[];pending.forEach(update=>update());cursor=0;return fn();},commit(){const pending=effects;effects=[];for(const {i,fn,deps}of pending){slots[i]?.cleanup?.();slots[i]={deps,cleanup:fn()};}},unmount(){slots.forEach(slot=>slot?.cleanup?.());}};}
function screen(){const hooksState=hooks(),calls=[],timers=new Map(),listeners=new Map(),window={scrollY:0,innerHeight:800,addEventListener(name,fn){listeners.set(name,fn);},removeEventListener(name,fn){if(listeners.get(name)===fn)listeners.delete(name);}},out={};
 vm.runInNewContext(ts.transpileModule(fs.readFileSync(__dirname+'/src/DownloadHistory.ts','utf8'),{compilerOptions:{module:ts.ModuleKind.CommonJS,target:ts.ScriptTarget.ES2022}}).outputText,{exports:out,AbortController,Error,window,document:{documentElement:{clientWidth:1000}},setTimeout(fn,delay){const key={};timers.set(key,{fn,delay});return key;},clearTimeout(key){timers.delete(key);},require:name=>name==='react'?hooksState.react:name==='./visibility'?{pageHidden:()=>false,onVisible:()=>()=>{}}:name==='./api'?{api:(url,signal)=>{const pending=deferred();calls.push({url,signal,pending});return pending.promise;}}:{}});
 return{hooks:hooksState,calls,timers,module:out,draw:()=>hooksState.draw(out.useDownloadHistory),wheel(){listeners.get('wheel')?.({deltaY:1});},poll(){const timer=[...timers.values()].find(timer=>timer.delay===5000);assert.ok(timer);timer.fn();}};
}
const rows=(start,state='complete',count=50)=>Array.from({length:count},(_,i)=>({kind:'youtube',item:{id:'v'+(start+i),title:'Saved '+(start+i),state,updated:1}}));
const page=(items,more,total,revision=0)=>({items,more,total,active:[],activityRevision:revision,clearedEngineIds:{torrent:['t1'],nzb:[]},clearedEngineGenerations:{torrent:{t1:2},nzb:{}}});

(async()=>{
 const s=screen();let feed=s.draw();feed.end.current={getBoundingClientRect:()=>({top:0})};s.hooks.commit();
 assert.equal(s.calls[0].url,'/api/download-history/snapshot');
 assert.equal(feed.items.length,0,'no incomplete groups or misleading status counts while the complete frame is pending');assert.equal(feed.busy,true);
 s.calls[0].pending.resolve(page([...rows(0),...rows(50,'failed',15)],false,65));await tick();feed=s.draw();s.hooks.commit();assert.equal(feed.items.length,65);assert.equal(feed.items.at(-1).item.state,'failed','older failures are present before any scroll');assert.equal(feed.more,false);assert.equal(feed.busy,false);
 const settled=s.calls.length;s.wheel();assert.equal(s.calls.length,settled,'scrolling no longer fetches history or jumps rows between groups');
 s.poll();const old=s.calls.at(-1);assert.equal(old.signal.aborted,false);
 feed.reset();assert.equal(old.signal.aborted,true,'clear acknowledgement immediately retires an old full-frame read before React effect cleanup');
 feed=s.draw();s.hooks.commit();const fresh=s.calls.at(-1);assert.notEqual(fresh,old);assert.equal(fresh.url,'/api/download-history/snapshot');const requests=s.calls.length;
 old.pending.resolve(page(rows(0),true,65,0));await tick();feed=s.draw();s.hooks.commit();assert.equal(feed.items.length,65,'the prior complete frame stays visible until the replacement is ready');
 fresh.pending.resolve(page(rows(100,'queued',15),false,15,7));await tick();feed=s.draw();s.hooks.commit();
 assert.equal(feed.activityRevision,7);assert.equal(feed.items.length,15);assert.equal(feed.items[0].item.id,'v100');assert.deepEqual(JSON.parse(JSON.stringify(feed.clearedEngineGenerations)),{torrent:{t1:2},nzb:{}});
 assert.equal(s.calls.length,requests,'clear refresh has no stale page-count cursor');assert.equal(feed.error,'');
 s.hooks.unmount();assert.equal(fresh.signal.aborted,true);assert.equal(s.timers.size,0);
 const restarted=screen();restarted.draw();restarted.hooks.commit();restarted.calls[0].pending.resolve(page([],false,0,7));await tick();const restored=restarted.draw();restarted.hooks.commit();assert.equal(restored.activityRevision,7);assert.equal(restored.items.length,0);assert.equal(restored.clearedEngineGenerations.torrent.t1,2,'a fresh mount reads durable visibility rather than browser storage');restarted.hooks.unmount();
 const loader=restarted.module.loadActivitySnapshot;
 let reads=0;
 const growing=await loader(async signal=>{assert.equal(signal.aborted,false);reads++;return page(rows(0,'queued',587),false,587,1);},new AbortController().signal);
 assert.equal(reads,1,'the complete 587-row snapshot never scans moving OFFSET windows');assert.equal(growing.items.length,587);
 const grown=await loader(async()=>page(rows(0,'searching',589),false,589,2),new AbortController().signal);
 assert.equal(grown.items.length,589);assert.equal(grown.activityRevision,2,'a later complete frame may grow and advance Clear revision');
 const mixed=await loader(async()=>page([{kind:'youtube',item:{id:'same'}},{kind:'discover',item:{id:'same'}}],false,2),new AbortController().signal);
 assert.equal(mixed.items.length,2,'identity includes the engine kind');
 await assert.rejects(loader(async()=>page([],false,5001),new AbortController().signal),/more than 5,000.*No partial list/);
 await assert.rejects(loader(async()=>page([],true,1),new AbortController().signal),/incomplete list/);
 await assert.rejects(loader(async()=>page(rows(0,'complete',1),false,2),new AbortController().signal),/incomplete list/);
 let duplicateReads=0;await assert.rejects(loader(async()=>{duplicateReads++;return page([rows(0)[0],rows(0)[0]],false,2);},new AbortController().signal),/inconsistent list/);assert.equal(duplicateReads,1,'a malformed complete response cannot start a moving-window retry');
 for(const invalid of [-1,1.5,NaN])await assert.rejects(loader(async()=>page([],false,0,invalid),new AbortController().signal),/invalid revision/);
 await assert.rejects(loader(async()=>page([{kind:'unexpected',item:{id:'bad'}}],false,1),new AbortController().signal),/incomplete list/);
 const beforeAbort=new AbortController();beforeAbort.abort();await assert.rejects(loader(()=>{throw new Error('must not read after cancellation');},beforeAbort.signal),{name:'AbortError'});
 const cancelled=new AbortController(),held=deferred(),loading=loader(()=>held.promise,cancelled.signal);cancelled.abort();held.resolve(page([],false,0));await assert.rejects(loading,{name:'AbortError'});
 console.log('Activity full history: complete single-read frames during queue growth, bounded count/revision validation, truthful cap/incomplete errors, held-read abort, durable clear metadata and lifecycle cleanup passed.');
})().catch(error=>{console.error(error);process.exitCode=1;});
