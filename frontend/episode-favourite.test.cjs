const assert=require('node:assert/strict'),fs=require('node:fs'),vm=require('node:vm'),ts=require('typescript');
const states=[],requests=[],events=[],out={},effects=[];let cursor=0;
const listeners=new Map();
const item={id:'show:77',kind:'show',title:'Show',externalId:'77'},episode={key:'show:77:2026:20',title:'An episode',season:2026,number:20,date:'2026-01-01',file:false,requested:false};
class CustomEvent{constructor(type,{detail}){this.type=type;this.detail=detail;}}
vm.runInNewContext(ts.transpileModule(fs.readFileSync(__dirname+'/src/discovery/DiscoveryTitle.tsx','utf8'),{compilerOptions:{module:ts.ModuleKind.CommonJS,jsx:ts.JsxEmit.ReactJSX}}).outputText,{exports:out,Date,CustomEvent,AbortController,setTimeout:()=>0,clearTimeout:()=>{},window:{addEventListener:(name,fn)=>listeners.set(name,fn),removeEventListener:name=>listeners.delete(name),dispatchEvent:event=>events.push(event)},require:name=>{if(name.endsWith('/mediaDuration'))return require('./media-duration-test-helper.cjs')();
  if(name.endsWith('/useChannelBanner'))return {useChannelBanner:()=>undefined};
 if(name==='react')return {useState:initial=>{const i=cursor++;if(!(i in states))states[i]=i===0?{item,items:[episode],loading:false,nextOffset:null}:initial;return [states[i],value=>states[i]=typeof value==='function'?value(states[i]):value];},useEffect:fn=>effects.push(fn),useRef:initial=>({current:initial})};
 if(name==='react/jsx-runtime')return require(name);
 if(name==='../api')return {api:async(url,signal,body,csrf)=>{requests.push({url,body,csrf});if(url==='/api/favourites')return {episodes:[]};if(url==='/api/discovery-engine/title')return {item,items:[episode],loading:false,nextOffset:null};return {key:body.key,favourite:body.favourite};}};
 if(name==='../discoverPayload')return {acquisitionItem:item=>item};
 if(name==='../mediaDate')return {mediaDate:value=>value};
 if(name==='./MovieInformation')return {useMovieInformation:()=>({info:null}),useShowInformation:()=>({info:null})};
 return new Proxy({},{get:(_,key)=>key});
}});
// DetailSheet carries tab content in props.tabs — walk those plus the header
// action row so episode rows stay reachable.
function nodes(value){return value==null?[]:Array.isArray(value)?value.flatMap(nodes):typeof value==='object'?[value,...nodes(value.props?.children),...nodes(value.props?.actions),...nodes(value.props?.tabs?.flatMap(t=>[t.label,t.content]))]:[];}
function render(){cursor=0;return nodes(out.default({item,csrf:'fixture-csrf',onClose(){}}));}
const flush=()=>new Promise(setImmediate);
(async()=>{
 let tree=render();tree.find(n=>n.props?.['aria-label']==='Episode').props.onChange({currentTarget:{value:episode.key}});tree=render();assert.equal(requests.length,0);let heart=tree.find(n=>n.props?.['aria-label']?.includes('episode')&&n.props?.['aria-pressed']!==undefined);assert.equal(heart.props.disabled,true,'Wait for known favourite state');
 for(const effect of effects.splice(0))effect();await flush();tree=render();effects.length=0;
 assert.ok(requests.some(r=>r.url==='/api/favourites'),'favourite state loads through its real effect rather than a hard-coded hook index');
 const reads=requests.length;const group=tree.find(n=>n.type==='Group'&&n.props?.justify==='space-between'&&nodes(n).some(child=>child.props?.['aria-pressed']!==undefined));heart=nodes(group).find(n=>n.props?.['aria-label']?.includes('episode')&&n.props?.['aria-pressed']!==undefined);assert.match(heart.props['aria-label'],/Favourite episode · S2026E20/);heart.props.onClick();await flush();
 const writes=()=>requests.filter(r=>r.url==='/api/favourites/episode');assert.equal(requests.length,reads+1);assert.equal(writes()[0].url,'/api/favourites/episode');assert.equal(writes()[0].body.key,episode.key);assert.equal(writes()[0].body.favourite,true);assert.equal(writes()[0].csrf,'fixture-csrf');
 tree=render();heart=tree.find(n=>n.props?.['aria-label']?.includes('episode')&&n.props?.['aria-pressed']!==undefined);assert.equal(heart.props['aria-pressed'],true);assert.equal(events.at(-1).type,'reeldock-episode-favourite-changed');
 heart.props.onClick();await flush();assert.equal(writes()[1].body.favourite,false);assert.equal(requests.length,reads+2,'Favourite actions only add their canonical writes');assert.ok(!requests.some(r=>/acquire|downloads|download$/.test(r.url)),'Favourite actions never call the downloader');
 console.log('Episode favourites: individual canonical key, heart-before-download, pending state, CSRF, toggle and collection events passed.');
})().catch(e=>{console.error(e);process.exitCode=1;});
