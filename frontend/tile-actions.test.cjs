const assert=require('node:assert/strict'),fs=require('node:fs'),vm=require('node:vm'),ts=require('typescript');
function compile(file,resolve,globals={}){const out={};vm.runInNewContext(ts.transpileModule(fs.readFileSync(__dirname+'/src/collections/'+file,'utf8'),{compilerOptions:{module:ts.ModuleKind.CommonJS,jsx:ts.JsxEmit.ReactJSX}}).outputText,{exports:out,Set,JSON,encodeURIComponent,URLSearchParams,AbortController,...globals,require:resolve});return out;}
const identities=compile('tileIdentity.ts',()=>({}));
assert.equal(identities.collectionIdentity({id:'imdb:movie:0077',kind:'movie',title:'Movie'}),'movie:77');
assert.equal(identities.collectionIdentity({id:'tmdb:movie:100',kind:'movie',externalId:'77',title:'Different display'}),'movie:77');
const direct=title=>identities.collectionIdentity({id:'show:77',kind:'show',title});
assert.equal(direct('One.Documentary.2026.720p.WEB'),direct('One Documentary 2026 1080p WEB'));
assert.notEqual(direct('One.Documentary.2026.720p.WEB'),direct('Other.Documentary.2026.720p.WEB'));
for(const kind of ['movie','show','video','channel'])assert.match(identities.watchlistHint({kind,id:'item',title:'Title'},false),/download automatically|download.*automatically/);
assert.match(identities.watchlistHint({kind:'show',id:'show:77',title:'Title'},true),/turn off future automatic downloads/);
const calls=[],events=[],values=[],refs=[];let cursor=0,refCursor=0,refreshes=0;
const item={id:'movie:77',kind:'movie',title:'A movie',automatic:true};
const rows={watchlist:{movies:[{...item,watchlistKey:'movie:77'}],shows:[],videos:[],channels:[],csrf:'fixture-token'},favourites:{movies:[],shows:[],videos:[],channels:[]},error:''};
const writes=compile('collectionFavourites.ts',n=>n==='../discoverPayload'?{acquisitionItem:v=>({id:v.id,kind:v.kind,title:v.title,videoId:v.videoId,channelId:v.channelId})}:{});
class CustomEvent{constructor(type,{detail}){this.type=type;this.detail=detail;}}
const out=compile('TileActions.tsx',n=>{
 if(n==='react')return {useEffect:()=>{},useRef:value=>refs[refCursor++]??={current:value},useState:value=>{const i=cursor++;if(!(i in values))values[i]=value;return[values[i],next=>values[i]=next];}};
 if(n==='react/jsx-runtime')return{jsx:(type,props)=>({type,props}),jsxs:(type,props)=>({type,props})};
 if(n==='../api')return {api:async(url,signal,body,csrf)=>{calls.push({url,body,csrf});return {key:'movie:77',favourite:true,watchlisted:false};}};
 if(n==='../discoverPayload')return{acquisitionItem:v=>({id:v.id,kind:v.kind,title:v.title,videoId:v.videoId,channelId:v.channelId})};
 if(n==='./collectionFavourites')return writes;
 if(n==='./tileIdentity')return identities;
 if(n==='./tileCollectionStore')return{useTileCollections:()=>rows,refreshTileCollections:async()=>refreshes++};
 return new Proxy({},{get:()=>()=>null});
},{window:{dispatchEvent:event=>events.push(event)},CustomEvent});
const nodes=v=>v==null?[]:Array.isArray(v)?v.flatMap(nodes):typeof v==='object'?[v,...nodes(v.props?.children)]:[];
function render(props={item}){cursor=0;refCursor=0;return nodes(out.default(props));}
const flush=()=>new Promise(setImmediate);
const movieFlow=(async()=>{
 let buttons=render().filter(v=>v.props?.['aria-label']);
 const heart=buttons.find(v=>v.props['aria-label']==='Add to favourites · A movie');
 assert.equal(heart.props['aria-pressed'],false);heart.props.onClick();heart.props.onClick();await flush();
 assert.equal(calls.length,1,'Rapid clicks create one favourite write');assert.equal(calls[0].csrf,'fixture-token');assert.deepEqual(JSON.parse(JSON.stringify(calls[0].body.settings)),{favourite:true});
 assert.equal(events[0].type,'reeldock-movie-options-changed');assert.equal(refreshes,1);
 buttons=render().filter(v=>v.props?.['aria-label']);
 buttons.find(v=>v.props['aria-label']==='Remove from Watchlist · A movie').props.onClick();await flush();
 assert.deepEqual(JSON.parse(JSON.stringify(calls.at(-1).body)),{key:'movie:77',watchlisted:false});
 assert.equal(events.at(-1).type,'reeldock-watchlist-changed');
 assert.equal(out.mediaTileTarget({id:'vod:item',title:'VOD',action:{type:'vod',source:'saved-source',item:{path:'title.strm'}}}).resolve.source,'saved-source');
 assert.equal(out.mediaTileTarget({id:'local:item',title:'Local',action:{type:'local',id:'local-id'}}).resolve.local,'local-id');
 for(const kind of ['show','shows']){const target=out.mediaTileTarget({id:'local:episode',title:'Episode',kind,season:2,number:3,action:{type:'local',id:'episode-id'}});assert.equal(target.episode,true,'group children keep their episode Favourite semantics');assert.equal(target.resolve.local,'episode-id');}
 assert.equal(out.mediaTileTarget({id:'local:unknown',title:'Unknown show file',kind:'show',action:{type:'local',id:'unknown'}}).episode,false,'missing episode identity is never invented');
 assert.equal(out.mediaTileTarget({id:'search:video',title:'Video',action:{type:'youtube',id:'abcdefghijk'}}).item.videoId,'abcdefghijk');
 assert.equal(out.mediaTileTarget({id:'search:video',title:'Video',dateApproximate:true,action:{type:'youtube',id:'abcdefghijk'}}).item.publishedApproximate,true,'Approximate YouTube dates must stay labelled through tile registration');
 console.log('Shared tile controls: canonical identities, separated documentaries, truthful future-download hints, CSRF, rapid-click gate and existing per-kind routes passed.');
})().catch(e=>{console.error(e);process.exitCode=1;});
(async()=>{
 const requests=[],stops=[],listeners=new Map();
 const store=compile('tileCollectionStore.ts',name=>name==='react'?{useSyncExternalStore:(subscribe,get)=>{stops.push(subscribe(()=>{}));return get();}}:name==='../api'?{api:async(url)=>{requests.push(url);return url==='/api/watchlist'?rows.watchlist:rows.favourites;}}:{},{window:{addEventListener:(name,fn)=>listeners.set(name,fn),removeEventListener:name=>listeners.delete(name)},document:{hidden:false},setInterval:()=>1,clearInterval:()=>{}});
 for(let i=0;i<100;i++)store.useTileCollections();
 assert.deepEqual(requests,['/api/watchlist','/api/favourites'],'One shared pair of reads for every visible tile');
 await flush();stops.splice(0).forEach(stop=>stop());assert.equal(listeners.size,0,'Unmount removes shared events and cached account state');
 console.log('Tile state: 100 tiles share two collection reads, with unmount cleanup.');
})().catch(e=>{console.error(e);process.exitCode=1;});

(async()=>{
 const effects=[],lookups=[],state=[];let slot=0;
 const passive=compile('TileActions.tsx',name=>{
  if(name==='react')return{useEffect:fn=>effects.push(fn),useRef:()=>({current:{}}),useState:value=>{const i=slot++;if(!(i in state))state[i]=value===false?true:value;return[state[i],next=>state[i]=next];}};
  if(name==='react/jsx-runtime')return{jsx:(type,props)=>({type,props}),jsxs:(type,props)=>({type,props})};
  if(name==='../api')return{api:async(...args)=>{lookups.push(args);throw Error('Match this local file first.');}};
  if(name==='./tileCollectionStore')return{useTileCollections:()=>rows};
  if(name==='./tileIdentity')return identities;
  return new Proxy({},{get:()=>()=>null});
 },{Error,IntersectionObserver:class{observe(){}disconnect(){}}});
 passive.default({resolve:{local:'unmatched-file'},label:'Other local film'});
 for(const effect of effects.splice(0))effect();await flush();
 assert.equal(lookups[0][0],'/api/favourites/local-title/unmatched-file');
 assert.deepEqual(JSON.parse(JSON.stringify(lookups[0][4])),{expectedReadStatuses:[400]});
 slot=0;const tree=nodes(passive.default({resolve:{local:'unmatched-file'},label:'Other local film'}));
 const buttons=tree.filter(v=>v.props?.['aria-label']);assert.equal(buttons.length,2);
 assert.ok(buttons.every(v=>v.props.disabled&&v.props.title==='Match this local file first.'),'unmatched identity stays disabled with explanatory tooltip');
 assert.ok(!tree.some(v=>v.props?.message==='Match this local file first.'),'passive expected identity does not create a page-wide error notification');
 console.log('Passive unmatched identity: scoped400 handling, disabled explanatory controls, no page-wide error.');
})().catch(e=>{console.error(e);process.exitCode=1;});

(async()=>{
 // A saved/global channel result may share the youtube action shape, but it is never a video.
 const id='UC'+'c'.repeat(22),channelCard={id:'youtube:'+id,kind:'channel',channelId:id,title:'Nature channel',image:'/avatar',action:{type:'youtube',id}};
 const target=out.mediaTileTarget(channelCard).item;
 assert.equal(target.kind,'channel');assert.equal(target.channelId,id);assert.equal(target.videoId,undefined);
 assert.equal(identities.collectionIdentity(target),'channel:'+id);
 assert.match(identities.watchlistHint(target,false),/Future uploads/);assert.doesNotMatch(identities.watchlistHint(target,false),/This video/);
 assert.equal(writes.favouriteWrite(target).url,'/api/channel-favourite/'+id);
 const fromDiscover=out.mediaTileTarget({...channelCard,action:{type:'discover',item:{id:'youtube:channel:'+id,kind:'channel',channelId:id,title:'Nature channel'}}}).item;
 assert.equal(fromDiscover.id,'youtube:channel:'+id);assert.equal(fromDiscover.channelId,id);
 const absent=out.mediaTileTarget({...channelCard,channelId:undefined,action:{type:'youtube',id:'abcdefghijk'}});
 assert.equal(absent.item,undefined,'a channel card cannot reinterpret an eleven-character video id as a channel');
 await movieFlow;
 const before=calls.length,eventsBefore=events.length;
 let buttons=render({item:target}).filter(v=>v.props?.['aria-label']);
 buttons.find(v=>v.props['aria-label']==='Add to favourites · Nature channel').props.onClick();await flush();
 const added=calls.slice(before);assert.equal(added.length,1);assert.equal(added[0].url,'/api/channel-favourite/'+id);assert.equal(added[0].body.favourite,true);assert.equal(added[0].csrf,'fixture-token');
 assert.equal(events[eventsBefore].type,'reeldock-channel-changed');
 buttons=render({item:target}).filter(v=>v.props?.['aria-label']);buttons.find(v=>v.props['aria-label']==='Add to Watchlist · Nature channel').props.onClick();await flush();
 assert.equal(calls.at(-1).url,'/api/watchlist/item');assert.equal(calls.at(-1).body.item.kind,'channel');assert.equal(calls.at(-1).body.item.channelId,id);assert.equal(calls.at(-1).body.watchlisted,true);assert.ok(!calls.slice(before).some(call=>call.url.includes('/youtube/register')),'channel actions never register a video');
 console.log('Channel cards: exact channel identity, channel-favourite endpoint, future-upload Watchlist hints and no video registration passed.');
})().catch(error=>{console.error(error);process.exitCode=1;});
