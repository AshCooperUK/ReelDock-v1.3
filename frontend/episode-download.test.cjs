const assert=require('node:assert/strict'),fs=require('node:fs'),vm=require('node:vm'),ts=require('typescript');
const states=[],effects=[],requests=[],exportsFor={};let cursor=0,queued=0;
const item={id:'show:77',kind:'show',title:'A Show',externalId:'77'},episode={key:'show:77:1:2',title:'Second episode',season:1,number:2};
const nzb=[{id:'a',title:'Show.S01E02.1080p',source:'Indexer A',size:123,reason:''},{id:'b',title:'Show.S01E02.720p',source:'Indexer B',size:124,reason:''},{id:'bad',title:'Show.S01E02.Sample',source:'Indexer B',size:1,reason:'Sample content excluded'}];
const vod={sources:[{type:'vod',label:'VOD provider',entries:[{source:'vod-source',path:'S01E02.strm',title:'Matching episode',season:1,number:2},{source:'vod-source',path:'S02E02.strm',title:'Wrong season',season:2,number:2}]}]};
vm.runInNewContext(ts.transpileModule(fs.readFileSync(__dirname+'/src/discovery/EpisodeDownload.tsx','utf8'),{compilerOptions:{module:ts.ModuleKind.CommonJS,jsx:ts.JsxEmit.ReactJSX}}).outputText,{exports:exportsFor,AbortController,URLSearchParams,Event,window:{dispatchEvent:()=>{}},require:name=>{
 if(name==='react')return {useState:value=>{const i=cursor++;if(!(i in states))states[i]=value;return [states[i],next=>states[i]=typeof next==='function'?next(states[i]):next];},useEffect:fn=>effects.push(fn)};
 if(name==='react/jsx-runtime')return require(name);
 if(name==='../api')return {api:async(url,signal,body,csrf)=>{requests.push({url,signal,body,csrf});return url.includes('download-sources')?vod:body.action==='releases'?{items:nzb,message:''}:{};}};
 if(name==='./MovieReleaseSelect')return {default:'ReleaseSelect'};
 return new Proxy({},{get:()=>()=>null});
}});
function render(){cursor=0;return exportsFor.default({item,episode,csrf:'csrf-fixture',globalSearch:true,onQueued:async()=>queued++});}
function nodes(value){return value==null?[]:Array.isArray(value)?value.flatMap(nodes):typeof value==='object'?[value,...nodes(value.props?.children),...nodes(value.props?.source),...nodes(value.props?.file),...nodes(value.props?.action)]:[];}
const flush=()=>new Promise(setImmediate);
(async()=>{
 render();const cleanups=effects.splice(0).map(fn=>fn());await flush();
 let tree=nodes(render());assert.equal(requests.filter(r=>r.body?.action==='search').length,0,'Viewing releases must not queue downloads');
 const select=tree.find(n=>Array.isArray(n.props?.sources));assert.deepEqual(Array.from(select.props.sources),['Indexer A','Indexer B','VOD provider']);select.props.onChange('Indexer B');
 tree=nodes(render());let file=tree.find(n=>n.props?.label==='Download file');assert.equal(file.props.value,'b');assert.equal(file.props.releases.length,2);
 assert.ok(file.props.releases.find(r=>r.id==='bad').reason,'Rejected releases keep their reason');
 const action=tree.find(n=>n.props?.['aria-label']==='Download · Second episode');assert.equal(action.props.disabled,false);action.props.onClick();await flush();
 const search=requests.find(r=>r.body?.action==='search');assert.equal(search.body.key,episode.key);assert.equal(search.body.value,'b');assert.equal(search.csrf,'csrf-fixture');assert.equal(queued,1);
 tree=nodes(render());tree.find(n=>Array.isArray(n.props?.sources)).props.onChange('VOD provider');
 file=nodes(render()).find(n=>n.props?.label==='Download file');assert.equal(file.props.releases.length,1);assert.equal(file.props.releases[0].title,'Matching episode');
 for(const cleanup of cleanups)cleanup?.();assert.ok(requests[0].signal.aborted);
 console.log('Episode source selection: exact episode, pinned file, rejected releases, CSRF, VOD filtering and cancellation passed.');
})().catch(e=>{console.error(e);process.exitCode=1;});
