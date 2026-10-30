const assert=require('node:assert/strict'),fs=require('node:fs'),vm=require('node:vm'),ts=require('typescript');
const React=require('react'),M=require('@mantine/core'),{renderToStaticMarkup}=require('react-dom/server');
const h=React.createElement;
function hooks(){const slots=[],pending=[];let cursor=0;const effect=(run,deps)=>{const at=cursor++,old=slots[at];if(!old||deps.some((value,index)=>!Object.is(value,old.deps[index])))pending.push(()=>{old?.cleanup?.();slots[at]={deps,cleanup:run()};});};return{slots,reset(){cursor=0;},flush(){for(const run of pending.splice(0))run();},cleanup(){for(const slot of slots)slot?.cleanup?.();},react:{...React,useState(initial){const at=cursor++;if(!(at in slots))slots[at]=typeof initial==='function'?initial():initial;return[slots[at],next=>slots[at]=typeof next==='function'?next(slots[at]):next];},useRef(initial){const at=cursor++;return slots[at]||(slots[at]={current:initial});},useId(){const at=cursor++;return slots[at]||(slots[at]='cookie-details-'+at);},useCallback(fn,deps){const at=cursor++,old=slots[at];if(!old||deps.some((value,index)=>!Object.is(value,old.deps[index])))slots[at]={deps,value:fn};return slots[at].value;},useEffect:effect,useLayoutEffect:effect,useSyncExternalStore:(_subscribe,snapshot)=>snapshot()}};}
function load(file,react,modules={},globals={}){const out={};vm.runInNewContext(ts.transpileModule(fs.readFileSync(__dirname+'/src/'+file,'utf8'),{compilerOptions:{target:ts.ScriptTarget.ES2022,module:ts.ModuleKind.CommonJS,jsx:ts.JsxEmit.ReactJSX}}).outputText,{exports:out,URL,URLSearchParams,Event,AbortController,...globals,require:name=>name==='react'?react:name==='react/jsx-runtime'?require(name):name==='@mantine/core'?M:modules[name]||new Proxy({},{get:(_target,key)=>key})});return out;}
const nodes=value=>Array.isArray(value)?value.flatMap(nodes):value&&typeof value==='object'?[value,...nodes(value.props?.children),...nodes(value.props?.actions)]:[];
const text=value=>Array.isArray(value)?value.map(text).join(''):typeof value==='string'?value:value&&typeof value==='object'?text(value.props?.children):'';
const nav=load('appNavigation.ts',React);
const storage=new Map(),browserWindow={localStorage:{getItem:key=>storage.get(key)||null,setItem:(key,value)=>storage.set(key,value)}};
let opened=0,closed=0;
const bar=load('ActivityStatusFooter.tsx',React,{'./appNavigation':nav,'./app/CookieWarning.module.css':{default:{layer:'cookie-layer'}}},{window:browserWindow});
const warning={id:'cookie-session-a',message:'Your saved YouTube cookies have expired. Upload fresh cookies to continue authenticated downloads.'};
let tree=bar.CookieWarningBar({warning,onCookies:()=>opened++,onClose:()=>closed++}),all=nodes(tree),close=all.find(node=>node.props?.['aria-label']==='Close YouTube cookie warning');
assert.equal(close.props.size,44);assert.equal(close.props.c,'black','the close target keeps strong contrast on yellow');assert.equal(close.props['aria-expanded'],undefined);
assert.equal(tree.props.pb,'calc(4px + env(safe-area-inset-bottom))');
assert.equal(all.find(node=>node.props?.role==='status').props['aria-label'],warning.message,'the complete warning remains accessible in a narrow one-line bar');
let html=renderToStaticMarkup(h(M.MantineProvider,{forceColorScheme:'dark'},tree));
assert.match(html,/background:var\(--mantine-color-yellow-3\)/);assert.match(html,/<button[^>]*type="button"[^>]*aria-label="Close YouTube cookie warning"/);
assert.match(html,/<a[^>]*href="\/app\/\?view=Settings&amp;section=YouTube#cookies"/);assert.ok(html.includes(warning.message));
assert.equal(all.filter(node=>node.type===M.Anchor).length,1);close.props.onClick();assert.equal(closed,1,'the X explicitly closes rather than minimises');
const link=all.find(node=>node.props?.href===nav.youtubeCookiesUrl);
function click(extra={}){return{button:0,defaultPrevented:false,preventDefault(){this.defaultPrevented=true;},...extra};}
let event=click();link.props.onClick(event);assert.equal(event.defaultPrevented,true);assert.equal(opened,1);
for(const extra of [{ctrlKey:true},{metaKey:true},{shiftKey:true},{altKey:true},{button:1},{defaultPrevented:true}]){event=click(extra);link.props.onClick(event);assert.equal(opened,1,'modified/default-prevented actions keep native navigation');}

function footerHarness(unavailableStorage=false){
 let activity={cookie_warning:warning},footerHeight=118,barHeight=52,sizeCursor=0,feedReads=0,popovers=0,returnedFocus=0;
 const state=hooks(),heights=[],portals=[],css=new Map(),store=new Map(),host={id:'active-modal-focus-scope'},barFocus={},document={activeElement:null,body:{id:'body'},documentElement:{style:{setProperty:(key,value)=>css.set(key,value),removeProperty:key=>css.delete(key)}}};
 class Element{isConnected=true;focus(){returnedFocus++;document.activeElement=this;}}
 const browser={get localStorage(){if(unavailableStorage)throw Error('Storage unavailable');return{getItem:key=>store.get(key)||null,setItem:(key,value)=>store.set(key,value)};}};
 const footer=load('ActivityStatusFooter.tsx',state.react,{
  './appNavigation':nav,'./downloadFeed':{useDownloadFeed:()=>{feedReads++;return{activity};}},
  './notifications-state':{subscribeNotifications(){},notificationSnapshot:()=>({host}),emptyNotifications:{host:null}},
  './app/CookieWarning.module.css':{default:{layer:'cookie-layer'}},
  '@mantine/hooks':{useElementSize:()=>({height:sizeCursor++===0?footerHeight:barHeight,ref(){}})},
  'react-dom':{createPortal:(children,target)=>{portals.push(target);return h('portal',{target},children);}},
 },{document,window:browser,HTMLElement:Element});
 const report=height=>heights.push(height);
 function draw(mobile){state.reset();sizeCursor=0;const result=footer.default({mobile,onCookies(){},onHeightChange:report,children:h('nav',null,'Phone navigation')}),layer=nodes(result).find(node=>node.props?.['data-cookie-warning-layer']!==undefined);layer?.props.ref({showPopover(){popovers++;},matches:()=>false,contains:target=>target===barFocus});state.flush();return result;}
 return{draw,heights,portals,css,store,host,state,footer,setWarning:value=>activity={cookie_warning:value},setHeight:value=>footerHeight=value,get feedReads(){return feedReads;},get popovers(){return popovers;},get returnedFocus(){return returnedFocus;},focusLayer(result){const layer=nodes(result).find(node=>node.props?.['data-cookie-warning-layer']!==undefined);layer.props.onFocusCapture({relatedTarget:new Element(),currentTarget:{contains:()=>false}});document.activeElement=barFocus;},close(result){nodes(result).find(node=>node.type===footer.CookieWarningBar).props.onClose();},reload(){state.cleanup();state.slots.length=0;}};
}
const footer=footerHarness();tree=footer.draw(true);assert.equal(footer.feedReads,1);assert.equal(footer.heights.at(-1),118);assert.equal(footer.css.get('--reeldock-cookie-bar-height'),'52px');assert.equal(footer.portals.at(-1),footer.host);assert.equal(footer.popovers,1);
assert.equal(nodes(tree).find(node=>node.props?.['data-mobile-bottom-nav']!==undefined).props.h,'calc(66px + env(safe-area-inset-bottom))');assert.equal(nodes(tree).find(node=>node.props?.['data-cookie-footer-space']!==undefined).props.h,52);
footer.focusLayer(tree);footer.close(tree);assert.equal(footer.returnedFocus,1,'keyboard close restores the prior connected focus target');assert.equal(footer.draw(false),null);assert.equal(footer.heights.at(-1),0,'closing removes the entire desktop bar and reservation immediately');assert.equal(footer.css.get('--reeldock-cookie-bar-height'),'0px');
footer.setHeight(66);tree=footer.draw(true);assert.equal(footer.heights.at(-1),66);assert.ok(!nodes(tree).some(node=>node.props?.['data-cookie-warning-layer']!==undefined),'no empty popover survives closing');
footer.setWarning({...warning});tree=footer.draw(true);assert.ok(!nodes(tree).some(node=>node.type===footer.footer.CookieWarningBar),'the same poll cannot reopen a closed warning');footer.reload();tree=footer.draw(true);assert.ok(!nodes(tree).some(node=>node.type===footer.footer.CookieWarningBar),'browser-local close survives remount/reload');
footer.setWarning({id:'new-cookie-session',message:'Fresh warning.'});footer.setHeight(118);tree=footer.draw(true);assert.equal(nodes(tree).find(node=>node.type===footer.footer.CookieWarningBar).key,'new-cookie-session','a new warning opens again');
for(let i=0;i<20;i++){footer.setWarning({id:'expiry-'+i,message:'Expired cookies.'});footer.close(footer.draw(true));}assert.equal(footer.store.size,1);assert.equal(JSON.parse([...footer.store.values()][0]).length,16,'closed warning storage stays bounded');
footer.setWarning(null);footer.setHeight(66);tree=footer.draw(true);assert.equal(footer.heights.at(-1),66);assert.equal(footer.draw(false),null);footer.state.cleanup();assert.equal(footer.css.has('--reeldock-cookie-bar-height'),false);
const privateFooter=footerHarness(true);tree=privateFooter.draw(false);privateFooter.close(tree);assert.equal(privateFooter.draw(false),null,'close still works when browser storage is unavailable');privateFooter.state.cleanup();

// Removing the cookie toast leaves ordinary alerts, retries and feed errors.
const alerts=load('ActivityNotifications.tsx',{...React,useEffect(){},useState:initial=>[initial,()=>{}]}, {'./downloadFeed':{useDownloadFeed:()=>({activity:{cookie_warning:warning,alerts:[{id:'other-alert',title:'A failed download',message:'Retry is still available.',retry:{id:'video'}}]},error:'Feed unavailable'})}});
all=nodes(alerts.ActivityNotifications({csrf:'fixture'}));assert.deepEqual(all.filter(node=>node.type===alerts.AlertTile).map(node=>node.props.alert.id),['activity-feed-Feed unavailable','other-alert']);

// Exercise the real shell route: one history entry, stale overlays closed,
// repeated links still deliver a new focus intent, and no pre-auth warning.
const locationState={url:new URL('https://reeldock.test/app/?view=Discover')},entries=[locationState.url.pathname+locationState.url.search],order=[];
const place={get href(){return locationState.url.href;},get search(){return locationState.url.search;},get hash(){return locationState.url.hash;},get pathname(){return locationState.url.pathname;}};
const history={state:null,pushState(_state,_unused,url){order.push('push');locationState.url=new URL(url,locationState.url);entries.push(url);},replaceState(_state,_unused,url){order.push('replace');locationState.url=new URL(url,locationState.url);entries[entries.length-1]=url;}};
const shellState=hooks(),shellNav=load('appNavigation.ts',React,{}, {location:place,history});
const shell=load('LiveDashboard.tsx',{...shellState.react,useEffect(){},lazy:()=> 'LazySettings'}, {
 './appNavigation':shellNav,'./ActivityStatusFooter':{default:'ActivityStatusFooter',ActivityOverlayScope:'ActivityOverlayScope'},'./ActivityNotifications':{ActivityNotifications:'ActivityNotifications'},
 './dashboard/DashboardPage':{default:'DashboardPage',preloadPage(){}},'./app/App.module.css':{default:{}},'@mantine/hooks':{useMediaQuery:()=>false},
},{location:place,history,document:{},window:{scrollTo(){},dispatchEvent:event=>order.push(event.type)}});
function drawShell(data){shellState.slots[2]=data;shellState.reset();return shell.default();}
tree=drawShell(null);assert.ok(!nodes(tree).some(node=>node.type==='ActivityStatusFooter'||node.type==='ActivityNotifications'),'no warning/feed surfaces mount before authentication is ready');
const ready={state:'ready',csrf:'csrf',overview:{version:'fixture',yt_dlp_version:'fixture'}};tree=drawShell(ready);let statusFooter=nodes(tree).find(node=>node.type==='ActivityStatusFooter');statusFooter.props.onHeightChange(180);tree=drawShell(ready);assert.equal(tree.props.footer.height,180,'the shell reserves the actual complete footer');
nodes(tree).find(node=>node.type==='ActivityStatusFooter').props.onCookies();tree=drawShell(ready);
assert.equal(entries.length,2);assert.equal(entries.at(-1),nav.youtubeCookiesUrl);assert.ok(order.indexOf('reeldock-notification-settings')<order.indexOf('push'),'old media overlays close before Settings is recorded');
let settingsNode=nodes(tree).find(node=>node.props?.initial==='YouTube');const firstIntent=settingsNode.props.cookieFocusRequest;assert.ok(firstIntent>0);
nodes(tree).find(node=>node.type==='ActivityStatusFooter').props.onCookies();tree=drawShell(ready);settingsNode=nodes(tree).find(node=>node.props?.initial==='YouTube');assert.ok(settingsNode.props.cookieFocusRequest>firstIntent);assert.equal(entries.length,2,'an already-mounted Settings link does not create duplicate history');

// Initial layout and modal lock can settle after guide data arrives. Correct
// only this explicit intent, never refocus or fight later user interaction.
let loaded=false,settingsCursor=0,focuses=0,scrolls=0,uploads=0,locked=true,now=0,frameId=0;
const frames=new Map(),resizes=[],mutations=[],windowTarget=new EventTarget(),documentState={activeElement:null,body:{hasAttribute:name=>name==='data-scroll-locked'&&locked}};
class Section extends EventTarget{isConnected=true;top=1530;focus(options){assert.equal(options.preventScroll,true);focuses++;documentState.activeElement=this;}getBoundingClientRect(){return{top:this.top};}scrollIntoView(options){assert.equal(options.block,'start');assert.equal(options.behavior,'instant');scrolls++;this.top=80;}}
class Resize{constructor(callback){this.callback=callback;resizes.push(this);}observe(node){this.node=node;}disconnect(){this.disconnected=true;}fire(){if(!this.disconnected)this.callback();}}
class Mutation{constructor(callback){this.callback=callback;mutations.push(this);}observe(node,options){assert.deepEqual(Array.from(options.attributeFilter),['data-scroll-locked']);this.node=node;}disconnect(){this.disconnected=true;}fire(){if(!this.disconnected)this.callback();}}
const section=new Section(),layoutNode={},guide={use_google_api:false},focusState=hooks(),youtube=load('settings/youtube/YouTubeSettings.tsx',focusState.react,{
 '../../SettingsUI':{useSettings:()=>settingsCursor++===0?{data:loaded?guide:null}:{data:{saved:true}},useAction:()=>({busy:false})},
},{fetch:()=>{uploads++;throw Error('Focus must not upload');},window:windowTarget,document:documentState,ResizeObserver:Resize,MutationObserver:Mutation,Date:{now:()=>now},getComputedStyle:()=>({scrollMarginTop:'80px'}),requestAnimationFrame:fn=>{frames.set(++frameId,fn);return frameId;},cancelAnimationFrame:id=>frames.delete(id)});
function flushFrames(){for(const [id,fn] of [...frames]){frames.delete(id);fn();}}
function drawCookies(intent){focusState.reset();settingsCursor=0;const result=youtube.default({csrf:'fixture',cookieFocusRequest:intent}),region=nodes(result).find(node=>node.props?.id==='cookies'),layout=nodes(result).find(node=>node.props?.['data-youtube-settings-layout']!==undefined);if(region)region.props.ref.current=section;if(layout)layout.props.ref.current=layoutNode;focusState.flush();return region;}
assert.equal(drawCookies(1),undefined);assert.equal(focuses,0);loaded=true;let region=drawCookies(1);assert.equal(region.props.role,'region');assert.equal(region.props.tabIndex,-1);assert.equal(focuses,1);assert.equal(scrolls,0,'alignment waits while the closing modal still locks the body');assert.equal(resizes.at(-1).node,layoutNode,'the actual ancestor observes height above Cookies');
section.top=1700;resizes.at(-1).fire();flushFrames();assert.equal(scrolls,0);locked=false;mutations.at(-1).fire();flushFrames();assert.equal(scrolls,1);assert.equal(section.top,80,'unlock aligns the visible Cookies region');
section.top=600;resizes.at(-1).fire();flushFrames();assert.equal(scrolls,2,'late Preferences/UploadSchedule growth is realigned');assert.equal(focuses,1,'layout changes never refocus');resizes.at(-1).fire();flushFrames();assert.equal(scrolls,2,'an already aligned resize cannot feed back');drawCookies(1);assert.equal(focuses,1);
windowTarget.dispatchEvent(new Event('wheel'));assert.equal(resizes.at(-1).disconnected,true);section.top=900;resizes.at(-1).fire();flushFrames();assert.equal(scrolls,2,'manual scroll ends alignment ownership');
drawCookies(2);assert.equal(focuses,2);assert.equal(scrolls,3,'a repeated link receives a fresh explicit intent');section.dispatchEvent(new Event('blur'));section.top=800;resizes.at(-1).fire();flushFrames();assert.equal(scrolls,3,'blur ends the previous intent');
drawCookies(3);const beforeDeadline=scrolls;now=6000;section.top=500;resizes.at(-1).fire();flushFrames();assert.equal(scrolls,beforeDeadline,'late unrelated layout cannot revive an expired intent');assert.equal(resizes.at(-1).disconnected,true);
drawCookies(4);const beforeCleanup=scrolls;focusState.cleanup();section.top=600;resizes.at(-1).fire();mutations.at(-1).fire();flushFrames();assert.equal(scrolls,beforeCleanup);assert.equal(frames.size,0);assert.equal(uploads,0);
console.log('Cookie status bar: whole-bar browser-local close/reload/new issue, native link, bounded storage, measured offsets/modal scope, history and intent-owned async/lock-aware Cookies alignment passed.');
