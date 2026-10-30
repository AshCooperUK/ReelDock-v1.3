const assert=require('node:assert/strict'),fs=require('node:fs'),vm=require('node:vm'),ts=require('typescript');
const states=[],refs=[],deps=[],effects=[],cleanups=[],listeners=new Map(),requests=[],events=[],out={};let cursor=0,refCursor=0,effectCursor=0;
class CustomEvent{constructor(type,{detail}){this.type=type;this.detail=detail;}}
const window={addEventListener:(name,fn)=>listeners.set(name,fn),removeEventListener:name=>listeners.delete(name),dispatchEvent:event=>{events.push(event);listeners.get(event.type)?.(event);}};
vm.runInNewContext(ts.transpileModule(fs.readFileSync(__dirname+'/src/collections/WatchlistButton.tsx','utf8'),{compilerOptions:{module:ts.ModuleKind.CommonJS,jsx:ts.JsxEmit.ReactJSX}}).outputText,{exports:out,AbortController,CustomEvent,window,URLSearchParams,Error,require:name=>{
 if(name==='react')return {useRef:initial=>{const i=refCursor++;return refs[i]??=( {current:initial});},useState:initial=>{const i=cursor++;if(!(i in states))states[i]=typeof initial==='function'?initial():initial;return [states[i],next=>states[i]=typeof next==='function'?next(states[i]):next];},useEffect:(fn,next)=>{const i=effectCursor++;if(!deps[i]||next.some((v,j)=>v!==deps[i][j])){deps[i]=next;effects.push(()=>{cleanups[i]?.();cleanups[i]=fn();});}}};
 if(name==='react/jsx-runtime')return require(name);
 if(name==='../discoverPayload')return {acquisitionItem:item=>item};
 if(name==='./tileIdentity')return {watchlistHint:()=>''};
 if(name==='../notifications')return {NotificationMessage:'NotificationMessage'};
 if(name==='../api')return {api:async(url,signal,body,csrf)=>new Promise((resolve,reject)=>requests.push({url,signal,body,csrf,resolve,reject}))};
 return new Proxy({},{get:()=>()=>null});
}});
function render(props){cursor=0;refCursor=0;effectCursor=0;const tree=out.default(props);effects.splice(0).forEach(fn=>fn());return tree;}
function nodes(v){return v==null?[]:Array.isArray(v)?v.flatMap(nodes):typeof v==='object'?[v,...nodes(v.props?.children)]:[];}
const notificationType='NotificationMessage';
const button=tree=>nodes(tree).find(v=>v.props?.['aria-pressed']!==undefined&&typeof v.props?.onClick==='function');
const flush=()=>new Promise(setImmediate);
(async()=>{
 const item={id:'movie:77',kind:'movie',title:'Upcoming movie'},props={item};
 let tree=render(props);assert.equal(button(tree).props.disabled,true);assert.match(requests[0].url,/^\/api\/watchlist\/item\?/);
 requests[0].resolve({key:'movie:77',watchlisted:false,csrf:'fixture-token'});await flush();tree=render(props);
 assert.equal(button(tree).props['aria-label'],'Add to Watchlist · Upcoming movie');assert.equal(button(tree).props['aria-pressed'],false);
 button(tree).props.onClick();button(tree).props.onClick();assert.equal(requests.length,2,'Rapid clicks must create one membership write');
 assert.equal(requests[1].body.watchlisted,true);assert.equal(requests[1].csrf,'fixture-token');tree=render(props);assert.equal(button(tree).props.disabled,true);
 requests[1].resolve({key:'movie:77',watchlisted:true,csrf:'fixture-token'});await flush();tree=render(props);
 assert.equal(button(tree).props['aria-pressed'],true);assert.equal(events.at(-1).type,'reeldock-watchlist-changed');
 window.dispatchEvent(new CustomEvent('reeldock-watchlist-changed',{detail:{key:'movie:77',watchlisted:false}}));tree=render(props);assert.equal(button(tree).props['aria-pressed'],false);
 tree=render({...props,disabled:true});button(tree).props.onClick();assert.equal(requests.length,2,'Read-only controls must not write');
 tree=render(props);button(tree).props.onClick();requests[2].reject(new Error('Could not save.'));await flush();tree=render(props);assert.equal(button(tree).props['aria-pressed'],false);assert.ok(nodes(tree).some(v=>v.type===notificationType&&v.props.kind==='error'&&v.props.message==='Could not save.'),'failed membership writes produce the shared error balloon without changing selection');
 // An old response must not select a new title after navigation.
 button(tree).props.onClick();const old=requests.at(-1);const next={item:{id:'show:88',kind:'show',title:'Another title'},compact:true};render(next);tree=render(next);assert.equal(button(tree).props.disabled,true);
 old.resolve({key:'movie:77',watchlisted:true,csrf:'fixture-token'});await flush();tree=render(next);assert.equal(button(tree).props.disabled,true);
 requests.at(-1).resolve({key:'show:88',watchlisted:true,csrf:'next-token'});await flush();tree=render(next);assert.equal(button(tree).props['aria-label'],'Remove from Watchlist · Another title');assert.equal(button(tree).props['aria-pressed'],true);
 button(tree).props.onClick();assert.equal(requests.at(-1).body.watchlisted,false);assert.equal(requests.at(-1).csrf,'next-token');
 requests.at(-1).resolve({key:'show:88',watchlisted:false,csrf:'next-token'});await flush();tree=render(next);assert.equal(button(tree).props['aria-pressed'],false);
 // Automatic policy saves can add membership while the shared popup stays open.
 window.dispatchEvent(new CustomEvent('reeldock-show-options-changed',{detail:{key:'show:88',field:'downloadFuture'}}));
 tree=render(next);tree=render(next);assert.equal(button(tree).props.disabled,true);
 requests.at(-1).resolve({key:'show:88',watchlisted:true,csrf:'next-token'});await flush();tree=render(next);assert.equal(button(tree).props['aria-pressed'],true);
 for(const cleanup of cleanups)cleanup?.();
 console.log('Watchlist controls: pending/read-only gates, CSRF, explicit add/remove, error recovery, shared events and stale-response protection passed.');
})().catch(e=>{console.error(e);process.exitCode=1;});
