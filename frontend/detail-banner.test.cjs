const assert=require('node:assert/strict'),fs=require('node:fs'),path=require('node:path'),vm=require('node:vm'),ts=require('typescript');
const React=require('react'),M=require('@mantine/core'),{renderToStaticMarkup}=require('react-dom/server'),h=React.createElement;
const nodes=value=>Array.isArray(value)?value.flatMap(nodes):value&&typeof value==='object'?[value,...nodes(value.props?.children)]:[];
function load(file,imports={},globals={},append=''){
 const out={};vm.runInNewContext(ts.transpileModule(fs.readFileSync(path.join(__dirname,'src',file),'utf8')+append,{compilerOptions:{target:ts.ScriptTarget.ES2022,module:ts.ModuleKind.CommonJS,jsx:ts.JsxEmit.ReactJSX}}).outputText,{exports:out,...globals,require:name=>name.endsWith('.module.css')?{default:{surface:'fixture-surface',bannerInformation:'fixture-banner'}}:name.endsWith('/mediaDuration')?require('./media-duration-test-helper.cjs')():imports[name]||require(name)});return out;
}
function stateHooks(){let index=0;const values=[];return {react:{...React,useState(initial){const slot=index++;if(!(slot in values))values[slot]=typeof initial==='function'?initial():initial;return[values[slot],next=>values[slot]=typeof next==='function'?next(values[slot]):next];},useEffect(){},useRef:current=>({current}),useId:()=> 'fixture-details'},draw(fn){index=0;return fn();}};}
const ui={Button:M.Button,Dialog:({children})=>children,DialogContent:({children})=>h('section',{role:'dialog'},h('button',{'aria-label':'Close window'},'×'),children),DialogTitle:({children})=>h(M.Title,{order:2,style:{overflowWrap:'anywhere'}},children),DialogDescription:({children})=>h(M.Text,{c:'dimmed'},children)};
let viewport=1440;
const common={'@mantine/core':M,'@mantine/hooks':{useMediaQuery:()=>viewport<768},'./SelectField':require('./select-field-test-helper.cjs')(),'./ui':ui,'../channelNavigation':{channelEvent:'fixture-channel'}};
const state=stateHooks(),header=load('components/DetailSheet.tsx',{...common,react:state.react},{},'\nexports.HeaderForTest=DetailHeader;').HeaderForTest;
const poster=h('img',{'data-role-poster':true,src:'/poster',alt:''}),actions=h('button',null,'Favourite');
const props={title:'A long title that must wrap within the phone header',subtitle:'A channel or movie',banner:'/banner',bannerFallback:'/poster',media:poster,actions};
const draw=()=>state.draw(()=>header(props));
let tree=draw(),banner=nodes(tree).find(node=>node.props?.['data-detail-banner']!==undefined);
assert.equal(banner.props.src,'/banner');assert.equal(banner.props.fit,'cover');assert.equal(banner.props.style.opacity,undefined,'a real banner is not reduced to a nearly invisible wash');
const information=nodes(tree).find(node=>node.props?.['data-detail-header-information']!==undefined);
assert.equal(information.props.bg,undefined,'the banner information panel uses the shared readable finish');
assert.equal(information.props['data-media-surface'],'banner-information');
assert.ok(information.props.className.includes('fixture-surface')&&information.props.className.includes('fixture-banner'));
assert.equal(information.props.style.opacity,undefined,'titles, poster and actions remain fully opaque');
assert.equal(information.props.style.minWidth,0);
const logo=nodes(tree).find(node=>node.props?.['data-detail-poster']!==undefined);
assert.equal(logo.props.children,poster);assert.equal(logo.props.w.base,56);assert.equal(logo.props.w.sm,80);assert.equal(logo.props.style.flexShrink,0,'poster/logo keeps its contained slot without transforms');
const target={hidden:false};banner.props.onError({currentTarget:target});assert.equal(target.hidden,true);
tree=draw();banner=nodes(tree).find(node=>node.props?.['data-detail-banner']!==undefined);assert.equal(banner.props.src,'/poster','failed backdrops fall back to the existing title image');
assert.equal(nodes(tree).filter(node=>node.props?.['data-detail-banner']!==undefined).length,1);
banner.props.onError({currentTarget:{hidden:false}});tree=draw();assert.ok(!nodes(tree).some(node=>node.props?.['data-detail-banner']!==undefined),'both failed sources leave no broken banner element');
assert.equal(nodes(tree).find(node=>node.type===M.Stack).props.pt,0,'unavailable banners leave no empty image bar');
assert.equal(nodes(tree).find(node=>node.type===ui.DialogTitle).props.style.paddingRight,64,'a missing-banner title preserves the existing close-control inset without adding another gutter');
assert.equal(nodes(tree).find(node=>node.props?.['data-detail-header-information']!==undefined).props.children[0].props.children[0].props.children,poster,'fallback preserves the original logo/poster role');
assert.equal(nodes(tree).find(node=>node.props?.['data-detail-header-information']!==undefined).props.bg,'var(--mantine-color-body)','no-art headers retain their original opaque surface');
assert.equal(nodes(tree).find(node=>node.props?.['data-detail-header-information']!==undefined).props.className,undefined,'failed or absent artwork creates no blur layer');
const Detail=load('components/DetailSheet.tsx',common).DetailSheet;
for(const scheme of ['light','dark'])for(const width of [320,768,1440]){
 viewport=width;
 const html=renderToStaticMarkup(h(M.MantineProvider,{forceColorScheme:scheme},h(Detail,{open:true,onOpenChange(){},...props,tabs:[{id:'overview',label:'Overview',content:'Information'},{id:'downloads',label:'Downloads',content:'Choices'}]})));
 assert.equal((html.match(/role="dialog"/g)||[]).length,1);assert.equal((html.match(/aria-label="Close window"/g)||[]).length,1);
 assert.equal((html.match(/data-detail-banner=/g)||[]).length,1);assert.equal((html.match(/data-role-poster=/g)||[]).length,1,'banner and poster each have one image role');
 assert.match(html,/data-media-surface="banner-information"/);assert.match(html,/fixture-surface fixture-banner/);assert.doesNotMatch(html,/opacity:0\.12|scale\(/);assert.ok(html.includes('min-width:0'),width+'px '+scheme+' allows title/content to shrink');
 if(width<768)assert.match(html,/role="combobox"/);else assert.match(html,/role="tab"/);
}

// Run the actual media components with inert data hooks and assert their header
// assets. Download permissions, IDs and actions are not supplied by these tests.
function mediaScene(file,kind,info={}){
 const state=stateHooks(),channelBanner='/channel-wide-banner';
 const generic=new Proxy({},{get:(_,key)=>key==='__esModule'?false:key==='default'?'Stub':key});
 const imports={react:state.react,'./useChannelBanner':{useChannelBanner:(_id,provided)=>provided||channelBanner},'../useChannelBanner':{useChannelBanner:()=>channelBanner},
  './MovieInformation':{useMovieInformation:()=>({info:kind==='movie'?info:null}),useShowInformation:()=>({info:kind==='show'?info:null})},
  '../discoverPayload':{acquisitionItem:item=>item},'./M3uDownloads':{useJobs:()=>({data:[],error:''})}};
 const module=load(file,new Proxy(imports,{get:(value,key)=>key in value?value[key]:key.startsWith('.')?generic:require(key)}),{Date,Intl,URLSearchParams,AbortController});
 return{module,draw:fn=>state.draw(fn)};
}
const id='UC'+'a'.repeat(22),video={id:'abcdefghijk',title:'Video',image:'/video-thumbnail',channelId:id};
let scene=mediaScene('VideoSheet.tsx','video');let sheet=scene.draw(()=>scene.module.default({csrf:'',open:true,onOpenChange(){},video}));
assert.equal(sheet.props.banner,'/channel-wide-banner','YouTube video uses its channel banner ahead of the video thumbnail');assert.equal(sheet.props.bannerFallback,'/video-thumbnail');assert.equal(sheet.props.media.props.src,'/video-thumbnail');
assert.equal(sheet.props.media.props.fit,'contain','the video still remains fully visible in its own poster slot');
scene=mediaScene('VideoSheet.tsx','video');sheet=scene.draw(()=>scene.module.default({csrf:'',open:true,onOpenChange(){},video:{...video,banner:'/supplied-channel-banner'}}));assert.equal(sheet.props.banner,'/supplied-channel-banner');
for(const kind of ['movie','show','video']){
 const info={backdrop:'/title-backdrop',image:'/title-poster'};scene=mediaScene('discovery/DiscoveryTitle.tsx',kind,info);
 sheet=scene.draw(()=>scene.module.default({item:{id:kind+':77',kind,title:'Title',image:'/initial-image',channelId:id},csrf:'',onClose(){}}));
 assert.equal(sheet.props.banner,kind==='video'?'/channel-wide-banner':'/title-backdrop');
 assert.equal(sheet.props.bannerFallback,kind==='video'?'/initial-image':'/title-poster');assert.equal(sheet.props.media.props.src,kind==='video'?'/initial-image':'/title-poster');
 assert.equal(sheet.props.media.props.fit,'contain');
}
scene=mediaScene('M3uLibrary.tsx','movie');sheet=scene.draw(()=>scene.module.M3uDetails({source:'exact-source',item:{title:'VOD film',kind:'movies',path:'Film.strm',artwork:'/vod-title-image'},onClose(){}}));
assert.equal(sheet.props.banner,'/vod-title-image');assert.equal(sheet.props.media.props.src,'/vod-title-image','VOD details use their actual existing title artwork');
assert.equal(sheet.props.media.props.fit,'contain');

// A newly discovered cache URL can respond 204 before its image worker finishes.
// Keep the current fallback while probing only the local artwork endpoint.
{
 let cursor=0,effect,cleanup;const values=[],timers=[],probes=[];
 const hooks={...React,useState(initial){const at=cursor++;if(!(at in values))values[at]=initial;return[values[at],next=>values[at]=typeof next==='function'?next(values[at]):next];},useEffect(fn,deps){const at=cursor++;if(!values[at]||deps.some((value,index)=>value!==values[at][index])){cleanup?.();effect=fn;values[at]=deps;}}};
 const cache='/artwork/'+'a'.repeat(64),window={setTimeout(fn,ms){assert.equal(ms,5000);timers.push(fn);return timers.length;},clearTimeout(){},Image:class{constructor(){probes.push(this);}}};
 const Header=load('components/DetailSheet.tsx',{...common,react:hooks},{window},'\nexports.HeaderForTest=DetailHeader;').HeaderForTest;
 const draw=()=>{cursor=0;effect=undefined;const node=Header({...props,banner:cache});if(effect)cleanup=effect();return node;};
 let node=draw();nodes(node).find(value=>value.props?.['data-detail-banner']!==undefined).props.onError({currentTarget:{hidden:false}});
 node=draw();assert.equal(nodes(node).find(value=>value.props?.['data-detail-banner']!==undefined).props.src,'/poster');
 timers.shift()();assert.equal(probes.length,1);assert.equal(probes[0].src,cache+'?artwork_retry=1');
 node=draw();assert.equal(nodes(node).find(value=>value.props?.['data-detail-banner']!==undefined).props.src,'/poster','pending cache checks do not flash an empty banner');
 probes[0].onload();node=draw();assert.equal(nodes(node).find(value=>value.props?.['data-detail-banner']!==undefined).props.src,cache,'the channel banner replaces the fallback once actual bytes load');
 nodes(node).find(value=>value.props?.['data-detail-banner']!==undefined).props.onError({currentTarget:{hidden:false}});draw();
 for(let retry=0;retry<12;retry++){assert.equal(timers.length,1);timers.shift()();probes.at(-1).onerror();draw();}
 assert.equal(timers.length,0,'an unavailable cache has a bounded retry budget');cleanup?.();
}

(async()=>{
 let cursor=0,effect,cleanup;const values=[],requests=[];const hooks={...React,useState(initial){const slot=cursor++;if(!(slot in values))values[slot]=initial;return[values[slot],next=>values[slot]=next];},useEffect(fn,deps){const slot=cursor++;if(!values[slot]||deps.some((value,index)=>value!==values[slot][index])){cleanup?.();effect=fn;values[slot]=deps;}}};
 const hook=load('useChannelBanner.ts',{react:hooks,'./api':{api:(url,signal,...rest)=>new Promise((resolve,reject)=>requests.push({url,signal,rest,resolve,reject}))}},{AbortController}).useChannelBanner;
 const draw=(channel,provided,enabled=true)=>{cursor=0;effect=undefined;const value=hook(channel,provided,enabled);if(effect)cleanup=effect();return value;};
 assert.equal(draw(id),undefined);assert.equal(requests.length,1);assert.equal(requests[0].url,'/api/guide/channel/'+id+'/info');assert.equal(requests[0].rest[2].feedback,'inline');
 const other='UC'+'b'.repeat(22);draw(other);assert.equal(requests[0].signal.aborted,true);requests[0].resolve({channel:{banner:'/stale-channel'}});await new Promise(resolve=>setImmediate(resolve));assert.equal(draw(other),undefined,'a retired response cannot become the new channel banner');
 requests[1].resolve({channel:{banner:'/other-channel-banner'}});await new Promise(resolve=>setImmediate(resolve));assert.equal(draw(other),'/other-channel-banner');
 assert.equal(draw(other,'/provided-banner'),'/provided-banner');assert.equal(requests.length,2,'supplied artwork needs no lookup');
 draw('invalid');draw(undefined);draw(id,undefined,false);assert.equal(requests.length,2,'invalid/no-channel and closed sheets never request imports or metadata');
 draw(id);requests.at(-1).reject(new Error('Unavailable'));await new Promise(resolve=>setImmediate(resolve));assert.equal(draw(id),undefined,'lookup failure leaves the existing caller fallback intact');cleanup?.();
 console.log('Detail banners: full-strength shared hero, readable shared finish with opaque no-art fallback, one image per role, error/empty collapse, correct YouTube/movie/TV/VOD assets and stale/cancelled lookup protection passed.');
})().catch(error=>{console.error(error);process.exitCode=1;});
