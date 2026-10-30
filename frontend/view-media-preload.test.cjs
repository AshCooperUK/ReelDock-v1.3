const assert=require('node:assert/strict'),fs=require('node:fs'),vm=require('node:vm'),ts=require('typescript');
const React=require('react'),{renderToStaticMarkup:renderMarkup}=require('react-dom/server');
const {MantineProvider}=require('@mantine/core');
const renderToStaticMarkup=tree=>renderMarkup(React.createElement(MantineProvider,{forceColorScheme:'dark'},tree));
function compiled(file,resolve=()=>({}),react=React){
 const exports={};vm.runInNewContext(ts.transpileModule(fs.readFileSync(__dirname+'/src/viewMedia/'+file,'utf8'),{compilerOptions:{module:ts.ModuleKind.CommonJS,jsx:ts.JsxEmit.ReactJSX,esModuleInterop:true}}).outputText,{exports,Error,AbortController,DOMException,setTimeout,clearTimeout,Date,require:name=>name.endsWith('/mediaDate')?require('./media-date-test-helper.cjs')():name.endsWith('/mediaDuration')?require('./media-duration-test-helper.cjs')():name==='../components/browse'?require('./media-browse-test-helper.cjs')():name==='@mantine/core'?require(name):name.endsWith('/HoverCard')?{__esModule:true,default:()=>null,useCardHover:()=>({anchor:{current:null},open:false,show:()=>{},hide:()=>{},failed:[],failImage:()=>{}}),titleHover:()=>({})}:name.endsWith('/TileActions')?Object.assign(()=>null,{mediaTileTarget:()=>({})}):name==='react'?react:name==='react/jsx-runtime'?require(name):name==='lucide-react'?new Proxy({},{get:()=>()=>null}):resolve(name)});return exports;
}
const ordering=compiled('ordering.ts');
const {MediaTile,MediaResults}=compiled('ViewMedia.tsx',name=>name==='../ChannelIdentity'?({name})=>React.createElement('span',{},name):name==='./ordering'?ordering:{});
function tile(kind,searched=true){return renderToStaticMarkup(React.createElement(MediaTile,{item:{id:kind,kind,title:kind,date:'2026-10-16T12:00:00Z',dateApproximate:true,image:'https://example.test/art.jpg',channel:kind==='video'?'Channel':undefined},layout:'grid',searched,onOpen:()=>{}}));}
for(const kind of ['movie','show','video','youtube']){
 const html=tile(kind);assert.match(html,/<li[^>]*mantine-Card-root/,'every source uses the real shared Mantine media card');assert.match(html,/loading="lazy"/,'artwork loads on demand');assert.match(html,/aria-label="Open /,'the card keeps an accessible details action');
}
assert.match(tile('video'),/Approximate upload date/);assert.match(tile('video'),/title="video"/);assert.match(tile('video'),/16\.10\.2026/,'upload dates use the shared UK display format');assert.doesNotMatch(tile('video'),/≈/);assert.match(tile('video'),/hover-card-title/);assert.match(tile('video'),/Channel/,'channel identity remains available in search');
assert.match(tile('video',false),/Channel/,'channel identity remains available in source browsing');

// The same accumulated global results are grouped for every starting source.
const mixed=[['video','Clip Z'],['show','Show Z'],['movie','Film Z'],['video','Clip A'],['movie','Film A'],['episode','Episode A']].map(([kind,title],i)=>({id:String(i),kind,title,action:{type:kind==='video'?'youtube':'local',id:String(i)}}));
const ordered=ordering.orderCards(mixed,'title'),sections=ordering.searchSections(ordered);
assert.equal(sections.map(group=>group.label).join('|'),'Movies|TV Shows|YouTube');
assert.equal(sections[0].items.map(item=>item.title).join('|'),'Film A|Film Z','selected title sort is retained inside Movies');
assert.equal(sections.flatMap(group=>group.items).length,mixed.length,'grouping loses no provider results');
assert.equal(mixed[0].title,'Clip Z','grouping and sorting do not mutate pagination data');
assert.equal(ordering.searchSections([mixed[0]])[0].label,'YouTube','a partial YouTube batch does not invent empty movie/show matches');
assert.equal(ordering.searchSections([...ordered,{id:'late',kind:'movie',title:'Later film',action:{type:'vod'}}])[0].items.length,3,'arriving pages merge into their existing type group');
for(const source of ['discover','vod','local','subscriptions'])for(const layout of ['grid','list']){
 const props={items:ordered,layout,searched:true,source,today:'2026-10-16',onOpen:()=>{}};
 const html=renderToStaticMarkup(React.createElement(MediaResults,props));
 assert.equal([...html.matchAll(/<h3[^>]*>(.*?)<\/h3>/g)].map(match=>match[1]).join('|'),'Movies|TV Shows|YouTube');
 assert.equal((html.match(/<li\b/g)||[]).length,6,'all search cards remain interactive in each layout');
 const browse=renderToStaticMarkup(React.createElement(MediaResults,{...props,searched:false}));
 assert.doesNotMatch(browse,/<h3[ >]/,'normal browsing has no search-only headings');
}

const {createPagePreloader}=compiled('pagePreloader.ts');
const page=(id,nextOffset=null)=>({source:'global',media:'all',items:[{id}],nextOffset,label:'Search results'});
async function run(){
 // Download controls and status hooks cannot mount until public registration succeeds.
 const result={id:'youtube:abcdefghijk',kind:'video',title:'Public clip',date:'2026-10-01',dateApproximate:true,channel:'Creator',channelId:'UC'+'r'.repeat(22),action:{type:'youtube',id:'abcdefghijk'}};
 let stateIndex=0,effects=[],states=[],complete,fail;const requests=[];
 const fakeReact={...React,useState:initial=>{const key=stateIndex++;if(!(key in states))states[key]=initial;return [states[key],value=>states[key]=typeof value==='function'?value(states[key]):value];},useEffect:effect=>effects.push(effect)};
 const player=compiled('SearchYouTubeDownload.tsx',name=>name==='../api'?{api:(url,signal,body,csrf)=>{requests.push({url,signal,body,csrf});return new Promise((resolve,reject)=>{complete=resolve;fail=reject;});}}:name==='../notifications'?{NotificationMessage:({message,kind,actions})=>React.createElement('div',{'data-notification-kind':kind},message,actions)}:name==='../VideoDownloadControls'?props=>React.createElement('div',{'data-player':props.id}):name==='../components/ui'?{Button:({children,...props})=>React.createElement('button',props,children)}:{},fakeReact);
 const render=()=>{stateIndex=0;return renderToStaticMarkup(player.default({item:result,csrf:'token'}));};
 assert.match(render(),/Preparing download options/);assert.equal(requests.length,0);
 const cleanup=effects[0]();assert.equal(requests[0].url,'/api/discover/youtube/register');assert.equal(requests[0].csrf,'token');assert.equal(requests[0].body.item.videoId,'abcdefghijk');assert.equal(requests[0].body.item.publishedApproximate,true);
 assert.doesNotMatch(render(),/data-player/,'in-flight registration must not mount download/status requests');
 complete({id:'abcdefghijk'});await new Promise(resolve=>setImmediate(resolve));
 assert.match(render(),/data-player="abcdefghijk"/,'successful registration unlocks the shared download controls');cleanup();assert.equal(requests[0].signal.aborted,true);
 states=[];effects=[];requests.length=0;assert.match(render(),/Preparing download options/);const cancelled=effects[0]();cancelled();complete({id:'abcdefghijk'});await new Promise(resolve=>setImmediate(resolve));assert.doesNotMatch(render(),/data-player/,'closing before registration completes cannot mount the download controls');
 states=[];effects=[];requests.length=0;render();const failedCleanup=effects[0]();fail(new Error('Registration unavailable'));await new Promise(resolve=>setImmediate(resolve));const failedHtml=render();assert.match(failedHtml,/data-notification-kind="error"/,'registration failure uses the shared error balloon');assert.match(failedHtml,/Registration unavailable/);assert.match(failedHtml,/Try again/);assert.doesNotMatch(failedHtml,/data-player/);failedCleanup();

 let reads=0,release;
 const fetched=new Promise(resolve=>release=resolve),buffer=createPagePreloader(async()=>{reads++;return fetched;});
 const controller=new AbortController(),warm=buffer.load('/search?q=first&cursor=1',controller.signal);
 assert.equal(reads,1,'starts warming before a scroll gesture');
 const consume=buffer.load('/search?q=first&cursor=1',controller.signal);
 assert.equal(reads,1,'scrolling shares the same in-flight request');
 release(page('second',2));assert.equal((await consume).items[0].id,'second');await warm;
 assert.equal((await buffer.load('/search?q=first&cursor=1',controller.signal)).nextOffset,2);
 assert.equal(reads,1,'a ready batch is reused immediately without refetching');

 let resume,iteration=0,waiting;const progress=[],polling=new Promise(resolve=>waiting=resolve);
 const staged=createPagePreloader(async()=>++iteration===1?{...page('partial'),preparing:true}:page('complete',2),()=>new Promise(resolve=>{resume=resolve;waiting();}));
 const warming=staged.load('/partial',controller.signal);await polling;
 const demand=staged.load('/partial',controller.signal,result=>progress.push(result.items[0].id));
 assert.equal(progress.join(','),'partial','arriving cards are available when the user reaches a pending batch');
 resume();assert.equal((await demand).items[0].id,'complete');await warming;

 let oldSignal;const switched=createPagePreloader(async(url,signal)=>{if(url==='/old'){oldSignal=signal;return new Promise((resolve,reject)=>signal.addEventListener('abort',()=>reject(new DOMException('Aborted','AbortError'))));}return page('new');});
 const old=switched.load('/old',controller.signal);const rejected=assert.rejects(old,{name:'AbortError'});
 assert.equal((await switched.load('/new',controller.signal)).items[0].id,'new');assert.equal(oldSignal.aborted,true,'changing scope cancels the old batch');await rejected;

 let tries=0;const retry=createPagePreloader(async()=>{if(++tries===1)throw Error('Temporary failure');return page('retry');});
 await assert.rejects(retry.load('/retry',controller.signal),/Temporary failure/);
 assert.equal((await retry.load('/retry',controller.signal)).items[0].id,'retry','background failure does not poison later demand');
 let pendingSignal;const aborted=new AbortController(),cancel=createPagePreloader(async(url,signal)=>{pendingSignal=signal;return new Promise((resolve,reject)=>signal.addEventListener('abort',()=>reject(new DOMException('Aborted','AbortError'))));});
 const pending=cancel.load('/pending',aborted.signal);const stopped=assert.rejects(pending,{name:'AbortError'});aborted.abort();await stopped;assert.equal(pendingSignal.aborted,true);
 buffer.clear();staged.clear();switched.clear();retry.clear();cancel.clear();
 console.log('View Media: Mantine search cards, one-batch preload, shared demand, partial progress, retry and scope cancellation passed.');
}
run().catch(error=>{console.error(error);process.exitCode=1;});
