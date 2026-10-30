const assert=require('node:assert/strict'),fs=require('node:fs'),vm=require('node:vm'),ts=require('typescript');
const hoverListeners=new Map(),browserWindow={innerHeight:900,innerWidth:1200,addEventListener:(name,handler)=>hoverListeners.set(name,handler),removeEventListener:name=>hoverListeners.delete(name)};
const out={};vm.runInNewContext(ts.transpileModule(fs.readFileSync(__dirname+'/src/downloadArtwork.ts','utf8'),{compilerOptions:{module:ts.ModuleKind.CommonJS}}).outputText,{exports:out});
for(const url of ['https://i.ytimg.com/vi/kffacxfA7G4/hqdefault.jpg','/artwork/'+'a'.repeat(64),'/api/m3u/source/artwork?id=1'])assert.equal(out.downloadPicture(url),url);
for(const url of ['https://i.ytimg.com.evil.test/vi/kffacxfA7G4/a.jpg','https://evil.test/a.jpg','javascript:alert(1)','//i.ytimg.com/vi/kffacxfA7G4/a.jpg',''])assert.equal(out.downloadPicture(url),'');
console.log('Download artwork: immediate YouTube thumbnail, cached artwork and source restrictions pass.');
const jsx=(type,props)=>({type,props:props||{}}),component={};
const status={};vm.runInNewContext(ts.transpileModule(fs.readFileSync(__dirname+'/src/components/ActivityStatus.tsx','utf8'),{compilerOptions:{module:ts.ModuleKind.CommonJS,jsx:ts.JsxEmit.ReactJSX}}).outputText,{exports:status,require});
const imports={'./components/ActivityStatus':status,'@mantine/core':{Image:'Image',Box:'Box',Text:'Text',Stack:'Stack',HoverCard:{Target:'Target',Dropdown:'Dropdown'}},'./downloadFeed':{downloadLabel:()=> 'Downloading'},'./downloadArtwork':out,react:{lazy:()=>"LazyPanel",Suspense:"Suspense",useRef:()=>({current:{getBoundingClientRect:()=>({bottom:80,left:40})}}),useState:v=>[Array.isArray(v)?v:true,()=>{}],useEffect:()=>{}},'react-dom':{createPortal:v=>v},'lucide-react':{Film:'Film'}};
vm.runInNewContext(ts.transpileModule(fs.readFileSync(__dirname+'/src/DownloadActivityRow.tsx','utf8'),{compilerOptions:{module:ts.ModuleKind.CommonJS,jsx:ts.JsxEmit.ReactJSX}}).outputText,{exports:component,require:n=>n.endsWith('.module.css')?{__esModule:true,default:{surface:'surface',tile:'tile',preview:'preview',bannerInformation:'bannerInformation'}}:n==='react/jsx-runtime'?{jsx,jsxs:jsx}:imports[n]||{},window:browserWindow,document:{body:{}}});
function nodes(t){return Array.isArray(t)?t.flatMap(nodes):t&&typeof t==='object'?[t,...nodes(t.props?.children)]:[];}
const url='https://i.ytimg.com/vi/kffacxfA7G4/hqdefault.jpg';
const entryNode=component.YouTubeActivityRow({entry:{kind:'download',state:'running',item:{id:'kffacxfA7G4',title:'Video',video:{thumbnail:url},banner:'/artwork/'+'b'.repeat(64)}}});
const entry=nodes(entryNode.props.title);
const hover=entry.find(n=>n.type===component.MediaHover);
assert.equal(hover.props.card.image,url);
assert.equal(hover.props.card.fallbackImage,url);
const tooltip=nodes(component.MediaHover(hover.props));
assert.equal(tooltip.find(n=>n.type==='Image').props.src,url);
console.log('Hover card renders the selected video thumbnail before metadata enrichment.');

let failed=[];
imports.react.useState=v=>[Array.isArray(v)?failed:true,fn=>{failed=fn(failed);}];
const cached='/artwork/'+'c'.repeat(64);
const props={name:'Video',card:{title:'Video',image:cached,fallbackImage:url}};
let rendered=nodes(component.MediaHover(props));
assert.equal(rendered.find(n=>n.type==='Image').props.src,cached);
rendered.find(n=>n.type==='Image').props.onError();
rendered=nodes(component.MediaHover(props));
assert.equal(rendered.find(n=>n.type==='Image').props.src,url);
rendered.find(n=>n.type==='Image').props.onError();
assert.equal(nodes(component.MediaHover(props)).some(n=>n.type==='Image'),false);
console.log('Missing cached artwork falls back to the video thumbnail; double failure shows a themed icon.');

// A new detail surface retires the hover; pointer/keyboard interaction inside
// an actionable hover must remain available to stock Mantine focus handling.
const stateValues=[],effects=[];let cursor=0;
imports.react.useState=initial=>{const index=cursor++;if(!(index in stateValues))stateValues[index]=initial;return[stateValues[index],value=>stateValues[index]=typeof value==='function'?value(stateValues[index]):value];};
imports.react.useEffect=fn=>effects.push(fn);
const drawHover=()=>{cursor=0;return component.MediaHover(props);};
let hoverRoot=drawHover();const cleanup=effects[0]();assert.equal(hoverRoot.props.disabled,false);
assert.equal(hoverRoot.props.interactive,true);assert.equal(hoverRoot.props.role,'dialog');assert.equal(hoverListeners.has('pointerdown'),false);assert.equal(hoverListeners.has('keydown'),false,'Tab/Space and pointer actions are not intercepted');
hoverListeners.get('reeldock-overlay-open')();hoverRoot=drawHover();assert.equal(hoverRoot.props.disabled,true,'a detail surface closes the hover without stacking');const target=nodes(hoverRoot).find(node=>typeof node.props?.onMouseEnter==='function');target.props.onMouseEnter();assert.equal(drawHover().props.disabled,false,'later intentional focus/hover can reopen its preview');
cleanup();assert.equal(hoverListeners.size,0,'hover listeners do not survive unmount');
console.log('Framework hover: overlay closes previews, stock interactive focus remains reachable, and listeners clean up.');
