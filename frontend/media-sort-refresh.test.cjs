const assert=require('node:assert/strict'),fs=require('node:fs'),vm=require('node:vm'),ts=require('typescript');
const jsx=(type,props,key)=>({type,props:props||{},key});
const nodes=n=>n==null?[]:Array.isArray(n)?n.flatMap(nodes):typeof n==='object'?[n,...nodes(n.props?.children)]:[];
const tick=()=>new Promise(setImmediate);
const deferred=()=>{let resolve;return{promise:new Promise(done=>resolve=done),resolve};};
function hooks(){
 const slots=[];let cursor=0,queue=[];
 const same=(a,b)=>a&&b&&a.length===b.length&&a.every((v,i)=>Object.is(v,b[i]));
 const react={memo:f=>f,Fragment:'Fragment',useState(v){const i=cursor++;if(!slots[i])slots[i]={value:typeof v==='function'?v():v};return[slots[i].value,n=>slots[i].value=typeof n==='function'?n(slots[i].value):n];},useRef(v){const i=cursor++;return(slots[i]??={value:{current:v}}).value;},useMemo(f,d){const i=cursor++;if(!slots[i]||!same(slots[i].deps,d))slots[i]={value:f(),deps:d};return slots[i].value;},useEffect(f,d){const i=cursor++;if(!slots[i]||!same(slots[i].deps,d))queue.push({i,f,d});}};
 react.useCallback=(f,d)=>react.useMemo(()=>f,d);
 return{react,draw(f,p){cursor=0;return f(p);},commit(){const work=queue;queue=[];for(const{i,f,d}of work){slots[i]?.cleanup?.();slots[i]={deps:d,cleanup:f()};}},close(){slots.forEach(s=>s?.cleanup?.());}};
}
const generic=new Proxy({},{get:(_,k)=>k==='__esModule'?false:k});
function load(file,modules={},globals={}){
 const out={};vm.runInNewContext(ts.transpileModule(fs.readFileSync(__dirname+'/src/'+file,'utf8'),{compilerOptions:{target:ts.ScriptTarget.ES2022,module:ts.ModuleKind.CommonJS,jsx:ts.JsxEmit.ReactJSX}}).outputText,{exports:out,AbortController,URL,URLSearchParams,Date,Intl,Map,Set,Event,setTimeout:()=>1,clearTimeout(){},...globals,require:n=>n==='react/jsx-runtime'?{jsx,jsxs:jsx,Fragment:'Fragment'}:modules[n]||generic});return out;
}
const ordering=load('viewMedia/ordering.ts'),movies=load('viewMedia/movieOptions.ts'),shows=load('viewMedia/showOptions.ts');
(async()=>{
 for(const[source,media]of[['discover','movies'],['discover','shows'],['vod','movies'],['vod','shows'],['local','movies'],['local','shows'],['local','youtube'],['subscriptions','youtube']]){
  const h=hooks(),calls=[],pending=[],bus=new EventTarget();
  const window={scrollX:0,scrollY:0,addEventListener:bus.addEventListener.bind(bus),removeEventListener:bus.removeEventListener.bind(bus),scrollTo(){}};
  const sorts=['new','oldest','title','title_desc','release','release_asc','source','year','year_asc','rating','rating_asc'];
  const modules={react:h.react,'../api':{api:(url,signal)=>{calls.push({url,signal});if(url==='/api/view-media/sources')return Promise.resolve({items:[{id:source,label:source,media:['movies','shows','youtube'],sorts}]});const p=deferred();pending.push(p);return p.promise;}},'./ordering':ordering,'./movieOptions':movies,'./showOptions':shows,
   './pagePreloader':{createPagePreloader:()=>({clear(){},load(){throw Error('No scroll prefetch expected');}})},'./refresh':{mergeCards:x=>x,refreshedPage:(_old,x)=>x,pendingPage:(_old,x)=>x,refreshDelay:()=>null},'./localParent':{createParentOpener:()=>({open(){},cancel(){}})},'./scrollPaging':{attachScrollPaging:()=>()=>{},trackPagingMomentum:()=>()=>{}},'../appNavigation':{mediaSearchQuery:()=>''},'../collections/TileActions':{default:'TileActions',mediaTileTarget:()=>({})}};
  const out=load('viewMedia/ViewMedia.tsx',modules,{window,location:{href:'http://fixture/app/',search:'',pathname:'/app/',hash:''},localStorage:{getItem:()=>null}});
  const props={csrf:'fixture',initialSource:source,initialMedia:media,sourceLocked:true};
  let tree=h.draw(out.default,props);h.commit();await tick();tree=h.draw(out.default,props);h.commit();
  assert.equal(pending.length,1,source+'/'+media+' starts its first database/provider view without scrolling');
  const first=pending.shift(),firstSignal=calls.at(-1).signal;
  const label=source==='discover'?(media==='shows'?'TV sort order':'Movie sort order'):'Sort by';
  const control=nodes(tree).find(n=>n.props?.['aria-label']===label);
  assert.ok(control,label+' is available');control.props.onChange({target:{value:'title'}});
  tree=h.draw(out.default,props);h.commit();
  assert.equal(firstSignal.aborted,true,'changing order retires the old request immediately');
  assert.equal(pending.length,1,'sort starts offset zero without a wheel, sentinel or scroll event');
  const request=calls.at(-1),url=new URL(request.url,'http://fixture');
  assert.equal(url.searchParams.get('source'),source);assert.equal(url.searchParams.get('media'),media);assert.equal(url.searchParams.get('sort'),'title');assert.equal(url.searchParams.get('offset'),null);
  const page=id=>({items:[{id,title:id,kind:media==='youtube'?'video':media==='shows'?'show':'movie',action:{type:'local',id}}],nextOffset:null,label:source});
  first.resolve(page('retired sort'));pending.shift().resolve(page('fresh title order'));await tick();
  tree=h.draw(out.default,props);h.commit();
  const results=nodes(tree).find(n=>n.props?.items&&n.props?.source===source);
  assert.equal(results.props.items[0].id,'fresh title order','a retired response cannot restore the old sort');h.close();
 }
 // Favourites and Watchlist use the same whole-collection shelf. Reordering
 // happens before rendering, with no page loading or scroll dependency.
 for(const kind of['movie','show','video','channel'])for(const collection of['favourites','watchlist']){
  const h=hooks(),out=load('collections/CollectionShelf.tsx',{react:h.react}),items=[{id:'z',title:'Zulu',kind},{id:'a',title:'Alpha',kind}];
  const props={id:'shelf',title:'Media',items,kind,collection,open(){},sort:'az'};
  let tree=h.draw(out.default,props),rows=nodes(tree).filter(n=>n.props?.item&&n.props?.collection===collection);assert.equal(rows.map(n=>n.props.item.id).join(','),'a,z');
  tree=h.draw(out.default,{...props,sort:'za'});rows=nodes(tree).filter(n=>n.props?.item&&n.props?.collection===collection);assert.equal(rows.map(n=>n.props.item.id).join(','),'z,a');
  assert.equal(items[0].id,'z','the source collection is never mutated');h.close();
 }
 console.log('All-media sorting: eager replacement GETs for movie/TV/VOD/local/YouTube; retired requests and immediate shared Favourite/Watchlist ordering passed.');
})().catch(error=>{console.error(error);process.exitCode=1;});
