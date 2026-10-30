const assert=require('node:assert/strict'),fs=require('node:fs'),vm=require('node:vm'),ts=require('typescript');
let subscribe,getSnapshot,timer,calls=0,hidden=false,resume=null,items=[{id:'a',title:'Current',state:'downloading',phase:'merging',progress:65,message:'Merging'}];
const events=new Map();
const code=ts.transpileModule(fs.readFileSync(require('node:path').join(__dirname,'src/downloadFeed.ts'),'utf8'),{compilerOptions:{module:ts.ModuleKind.CommonJS}}).outputText;
const out={};vm.runInNewContext(code,{exports:out,AbortController,setTimeout(fn,ms){assert.equal(ms,5000);timer=fn;return 1;},clearTimeout(){timer=null;},window:{addEventListener:(n,f)=>events.set(n,f),removeEventListener:n=>events.delete(n)},require(name){if(name==='./visibility')return {pageHidden:()=>hidden,onVisible(fn){resume=fn;return()=>{resume=null;};}};if(name==='react')return {useSyncExternalStore(s,g){subscribe=s;getSnapshot=g;return g();}};return {api:async url=>{calls++;return url==='/api/downloads'?{items}:{download:{id:'old',progress:0},queued:99,syncing:false};}};}});
const settle=()=>new Promise(resolve=>setImmediate(resolve));
(async()=>{
 out.useDownloadFeed();let a,b;
 const stopA=subscribe(()=>{a=getSnapshot();}),stopB=subscribe(()=>{b=getSnapshot();});await settle();
 assert.equal(calls,2);assert.equal(a,b);assert.equal(a.activity.download,a.items[0]);assert.equal(a.activity.download.progress,65);assert.equal(a.activity.queued,0);assert.equal(out.downloadLabel(a.items[0]),'Merging audio and video');
 items=[{id:'a',state:'complete'},{id:'b',state:'queued'}];timer();await settle();assert.equal(a,b);assert.equal(a.activity.download,null);assert.equal(a.activity.queued,1);
 items=[{id:'b',state:'cancelling',progress:40}];events.get('reeldock-library-changed')();await settle();assert.equal(a,b);assert.equal(out.downloadLabel(a.items[0]),'Cancelling');
 // Hidden tabs finish the current request but do not schedule another one.
 hidden=true;const before=calls;timer();await settle();assert.equal(calls,before+2);assert.equal(timer,null);
 hidden=false;resume();await settle();assert.equal(calls,before+4);assert.ok(timer);
 stopA();assert.ok(timer);stopB();assert.equal(timer,null);assert.equal(events.size,0);assert.equal(resume,null);
 console.log('Shared downloads: consistent snapshots, single polling loop, hidden-tab pause, completion, cancellation and cleanup passed.');
})().catch(e=>{console.error(e);process.exitCode=1;});
