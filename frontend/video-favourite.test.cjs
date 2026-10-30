const assert=require('node:assert/strict'),fs=require('node:fs'),vm=require('node:vm'),ts=require('typescript');
const states=[],deps=[],effects=[],cleanups=[],listeners=new Map(),requests=[],events=[],out={};let cursor=0,effectCursor=0,complete;
class CustomEvent{constructor(type,{detail}){this.type=type;this.detail=detail;}}
const window={addEventListener:(name,fn)=>listeners.set(name,fn),removeEventListener:name=>listeners.delete(name),dispatchEvent:event=>{events.push(event);listeners.get(event.type)?.(event);}};
vm.runInNewContext(ts.transpileModule(fs.readFileSync(__dirname+'/src/VideoFavourite.tsx','utf8'),{compilerOptions:{module:ts.ModuleKind.CommonJS,jsx:ts.JsxEmit.ReactJSX}}).outputText,{exports:out,AbortController,CustomEvent,window,require:name=>{
 if(name==='react')return {useState:initial=>{const i=cursor++;if(!(i in states))states[i]=typeof initial==='function'?initial():initial;return [states[i],next=>states[i]=typeof next==='function'?next(states[i]):next];},useEffect:(fn,next)=>{const i=effectCursor++;if(!deps[i]||next.some((v,j)=>v!==deps[i][j])){deps[i]=next;effects.push(()=>{cleanups[i]?.();cleanups[i]=fn();});}}};
 if(name==='react/jsx-runtime')return require(name);
 if(name==='./api')return {api:async(url,signal,body,csrf)=>{requests.push({url,body,csrf});return new Promise(resolve=>complete=resolve);}};
 return new Proxy({},{get:()=>()=>null});
}});
function render(props){cursor=0;effectCursor=0;const tree=out.default(props);effects.splice(0).forEach(fn=>fn());return tree;}
function nodes(v){return v==null?[]:Array.isArray(v)?v.flatMap(nodes):typeof v==='object'?[v,...nodes(v.props?.children)]:[];}
const button=tree=>nodes(tree).find(v=>v.props?.['aria-pressed']!==undefined&&typeof v.props?.onClick==='function');
const flush=()=>new Promise(setImmediate);
(async()=>{
 const props={id:'abcdefghijk',compact:true,known:false,csrf:'fixture-csrf',title:'Upload'};
 let tree=render(props),control=button(tree);assert.equal(requests.length,0,'Known batch state must not trigger a request for each upload');
 assert.equal(control.props['aria-label'],'Favourite video · Upload');assert.equal(control.props['aria-pressed'],false);assert.equal(control.props.disabled,false);assert.ok(!nodes(tree).some(v=>v.props?.children==='Favourite'));
 control.props.onClick();tree=render(props);assert.equal(button(tree).props.disabled,true);button(tree).props.onClick();assert.equal(requests.length,1);
 assert.equal(requests[0].url,'/api/favourites/video/abcdefghijk');assert.equal(requests[0].body.favourite,true);assert.equal(requests[0].csrf,'fixture-csrf');
 complete({id:props.id,favourite:true,csrf:props.csrf});await flush();tree=render(props);
 assert.equal(button(tree).props['aria-pressed'],true);assert.equal(events.at(-1).detail.favourite,true);
 window.dispatchEvent(new CustomEvent('reeldock-video-favourite-changed',{detail:{id:props.id,favourite:false}}));tree=render(props);assert.equal(button(tree).props['aria-pressed'],false);
 tree=render({...props,disabled:true});button(tree).props.onClick();assert.equal(requests.length,1,'Read-only controls cannot write');
 tree=render({...props,known:null});assert.equal(button(tree).props.disabled,true);assert.equal(requests.length,1,'Pending batch state cannot start per-row requests');
 for(const cleanup of cleanups)cleanup?.();
 console.log('Upload favourites: compact icon, batch state, CSRF persistence, pending/read-only gates and shared change events passed.');
})().catch(e=>{console.error(e);process.exitCode=1;});
