const assert=require('node:assert/strict'),fs=require('node:fs'),vm=require('node:vm'),ts=require('typescript');
const compile=file=>ts.transpileModule(fs.readFileSync(__dirname+'/src/viewMedia/'+file,'utf8'),{compilerOptions:{module:ts.ModuleKind.CommonJS,jsx:ts.JsxEmit.ReactJSX}}).outputText;
const jsx=(type,props)=>({type,props:props||{}});
function surface(kind='viewport'){
 const listeners=new Map(),view={innerHeight:800,getComputedStyle:node=>({overflowY:node.overflowY||'visible'}),addEventListener:(name,fn)=>listeners.set(name,fn),removeEventListener:name=>listeners.delete(name)};
 const body={},html={},document={body,documentElement:html,defaultView:view};let top=1000;
 const root={parentElement:body,overflowY:kind==='nested'?'auto':'visible',matches:()=>kind==='dialog',getBoundingClientRect:()=>({top:100,bottom:500}),addEventListener:(name,fn)=>listeners.set('root-'+name,fn),removeEventListener:name=>listeners.delete('root-'+name)};
 const edge={ownerDocument:document,parentElement:kind==='viewport'?body:root,getBoundingClientRect:()=>({top,bottom:top+1})};
 return{edge,root,view,listeners,move(value){top=value;}};
}
const observers=[];
class Observer{constructor(callback,options){Object.assign(this,{callback,options});observers.push(this);}observe(node){this.node=node;}disconnect(){this.disconnected=true;}emit(visible=true){this.callback([{isIntersecting:visible}]);}}
function paging(withObserver=true){const out={};vm.runInNewContext(compile('scrollPaging.ts'),{exports:out,...withObserver?{IntersectionObserver:Observer}:{}});return out;}
const helper=paging();
for(const kind of ['viewport','nested','dialog']){
 const s=surface(kind);let calls=0;const cleanup=helper.observeAutoPaging(s.edge,()=>calls++),observer=observers.at(-1);
 assert.equal(observer.options.root,kind==='viewport'?null:s.root);assert.equal(observer.options.rootMargin,'180px 0px');assert.equal(calls,0);
 observer.emit(false);assert.equal(calls,0);observer.emit();observer.emit();assert.equal(calls,1,'repeated intersection entries consume one batch');
 cleanup();observer.emit();assert.equal(calls,1,'queued intersections cannot run after cleanup');assert.ok(observer.disconnected);
}
const slots=[];let cursor=0,pending=[];
const react={memo:fn=>fn,useRef(value){return(slots[cursor++]??={current:value});},useEffect(fn,deps){const index=cursor++;if(!slots[index]||deps.some((value,i)=>value!==slots[index].deps[i]))pending.push({index,fn,deps});}};
const out={};vm.runInNewContext(compile('ViewMedia.tsx'),{exports:out,require:name=>name==='react'?react:name==='react/jsx-runtime'?{jsx,jsxs:jsx}:name==='./scrollPaging'?helper:new Proxy({},{get:(_,key)=>key})});
const s=surface();let first=0,latest=0;
function draw(props){cursor=0;const tree=out.AutoMoreResults(props);tree.props.ref.current=s.edge;const work=pending;pending=[];for(const{index,fn,deps}of work){slots[index]?.cleanup?.();slots[index]={deps,cleanup:fn()};}return tree;}
draw({enabled:true,batch:'Movies:24:60',onMore:()=>first++});const initial=observers.at(-1),count=observers.length;
draw({enabled:true,batch:'Movies:24:60',onMore:()=>latest++});assert.equal(observers.length,count,'a new callback cannot rearm an unchanged batch');
initial.emit();initial.emit();assert.equal(first,0);assert.equal(latest,1,'the observer uses the current callback exactly once');
draw({enabled:true,batch:'Movies:48:60',onMore:()=>latest++});const next=observers.at(-1);initial.emit();assert.equal(latest,1);next.emit();assert.equal(latest,2,'only a new rendered batch rearms the sentinel');
draw({enabled:false,batch:'Movies:48:60',onMore:()=>latest++});next.emit();assert.equal(latest,2,'busy/hidden sheets pause reveal');
draw({enabled:true,batch:'Movies:48:60',onMore:()=>latest++});const resumed=observers.at(-1);resumed.emit();assert.equal(latest,3);
slots.forEach(slot=>slot?.cleanup?.());resumed.emit();assert.equal(latest,3,'unmount disconnects and fences stale callbacks');
for(const kind of ['viewport','nested']){
 const fallback=paging(false),f=surface(kind);let calls=0;
 const cleanup=fallback.observeAutoPaging(f.edge,()=>calls++);assert.equal(calls,0);
 f.move(kind==='viewport'?780:490);f.listeners.get(kind==='viewport'?'scroll':'root-scroll')();f.listeners.get('resize')();assert.equal(calls,1,'scroll/resize fallback also consumes one visible batch');
 cleanup();assert.equal(f.listeners.size,0);
}
console.log('Automatic paging: viewport/nested/dialog roots, once-per-batch, live callbacks, loading pause, stale cleanup and no-Observer fallback passed.');
