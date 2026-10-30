const assert=require('node:assert/strict'),fs=require('node:fs'),vm=require('node:vm'),ts=require('typescript');
const React=require('react'),Mantine=require('@mantine/core'),{renderToStaticMarkup}=require('react-dom/server');
const jsx=(type,props,key)=>({type,props:props||{},key});
const nodes=node=>node==null?[]:Array.isArray(node)?node.flatMap(nodes):typeof node==='object'?[node,...nodes(node.props?.children)]:[];
const tick=()=>new Promise(setImmediate);
function compile(file,modules={},globals={}){const out={};vm.runInNewContext(ts.transpileModule(fs.readFileSync(__dirname+'/src/'+file,'utf8'),{compilerOptions:{module:ts.ModuleKind.CommonJS,target:ts.ScriptTarget.ES2022,jsx:ts.JsxEmit.ReactJSX}}).outputText,{exports:out,Map,Set,Date,Intl,URLSearchParams,AbortController,DOMException,Event,...globals,require:name=>modules[name]||new Proxy({},{get:(_,key)=>key})});return out;}
function hooks(){
 const slots=[];let index=0,pending=[],updates=[];
 const same=(a,b)=>a&&b&&a.length===b.length&&a.every((v,i)=>Object.is(v,b[i]));
 const react={memo:f=>f,useState(initial){const i=index++;slots[i]??={value:typeof initial==='function'?initial():initial};return[slots[i].value,next=>updates.push(()=>{slots[i].value=typeof next==='function'?next(slots[i].value):next;})];},useRef(initial){const i=index++;return(slots[i]??={value:{current:initial}}).value;},useMemo(fn,deps){const i=index++;if(!slots[i]||!same(slots[i].deps,deps))slots[i]={value:fn(),deps};return slots[i].value;},useEffect(fn,deps){const i=index++;if(!slots[i]||!same(slots[i].deps,deps))pending.push({i,fn,deps});}};
 react.useCallback=(fn,deps)=>react.useMemo(()=>fn,deps);
 return{react,flush(component,props){let tree,turn=0;do{const queued=updates;updates=[];queued.forEach(update=>update());index=0;tree=component(props);const work=pending;pending=[];for(const{i,fn,deps}of work){slots[i]?.cleanup?.();slots[i]={deps,cleanup:fn()};}assert.ok(++turn<20,'hook effects settle');}while(updates.length);return tree;},close(){slots.forEach(slot=>slot?.cleanup?.());}};
}
const sources=[{id:'all',label:'All sources',media:['all','movies','shows','youtube'],sorts:['new','oldest','title','title_desc']},{id:'subscriptions',label:'YouTube',media:['youtube'],sorts:['new','oldest','title','title_desc']},{id:'discover',label:'NZB',media:['movies','shows'],sorts:['source','release','title']},{id:'vod',label:'VOD',media:['movies','shows'],sorts:['new','oldest','title']},{id:'local',label:'Local Library',media:['movies','shows','youtube'],sorts:['new','oldest','title']}];
const card=id=>({id,title:'Title '+id,kind:'video',channel:'Creator',channelId:'UC'+'a'.repeat(22),action:{type:'youtube',id}});
const page=(ids,next=null,extra={})=>({source:'subscriptions',media:'youtube',items:ids.map(card),nextOffset:next,label:'YouTube',...extra});
const params=request=>new URL(request.url,'http://fixture').searchParams;
function scene(){
 const h=hooks(),calls=[],requests=[],timers=new Map(),bus=new EventTarget(),storageReads=[],storageWrites=[];let timer=0,edge;
 const location={search:'',href:'http://fixture/app/',pathname:'/app/',hash:''};
 const modules={react:h.react,'react/jsx-runtime':{jsx,jsxs:jsx,Fragment:'Fragment'},'../api':{api(url,signal,body){calls.push({url,signal,body});if(url==='/api/view-media/sources')return Promise.resolve({items:sources});let resolve,reject;const promise=new Promise((yes,no)=>{resolve=yes;reject=no;});const request={url,signal,resolve,reject};requests.push(request);return promise;}},'./filtering':compile('viewMedia/filtering.ts'),'./ordering':compile('viewMedia/ordering.ts'),'./movieOptions':compile('viewMedia/movieOptions.ts'),'./showOptions':compile('viewMedia/showOptions.ts'),'./refresh':compile('viewMedia/refresh.ts'),'./pagePreloader':compile('viewMedia/pagePreloader.ts'),'./localParent':{createParentOpener:callbacks=>({open:item=>callbacks.group(item),cancel(){}})},'./scrollPaging':{attachScrollPaging:(_window,_position,load)=>{edge=load;return()=>{if(edge===load)edge=undefined;};},trackPagingMomentum:()=>()=>{}},'../appNavigation':{mediaSearchQuery:search=>new URLSearchParams(search).get('q')||''},'../collections/TileActions':{default:'TileActions',mediaTileTarget:()=>({})}};
 const app=compile('viewMedia/ViewMedia.tsx',modules,{window:{scrollY:0,addEventListener:bus.addEventListener.bind(bus),removeEventListener:bus.removeEventListener.bind(bus)},location,localStorage:{getItem(key){storageReads.push(key);return key==='reeldock-channel-show-shorts'?'true':null;},setItem(...args){storageWrites.push(args);},removeItem(...args){storageWrites.push(args);}},setTimeout(fn,delay){timers.set(++timer,{fn,delay});return timer;},clearTimeout(id){timers.delete(id);}});
 const s={calls,requests,storageReads,storageWrites,app,location,draw:()=>h.flush(app.default,{csrf:'fixture'}),edge:()=>edge?.(),close:()=>h.close(),search(query){location.search='?q='+encodeURIComponent(query);bus.dispatchEvent(new Event('reeldock-media-search'));return s.draw();}};
 s.chooseSource=id=>{const tree=s.draw(),select=nodes(tree).find(n=>n.props?.['aria-label']==='Media source');select.props.onChange({target:{value:id}});return s.draw();};
 s.settle=async(request,value)=>{request.resolve(value);await tick();return s.draw();};
 s.start=async()=>{s.draw();await tick();s.draw();return s;};
 return s;
}
const toggle=tree=>nodes(tree).find(node=>node.type==='Switch'&&node.props.label==='Show Shorts');
const results=(s,tree)=>nodes(tree).find(node=>node.type===s.app.MediaResults).props;
const identifiers=(s,tree)=>Array.from(results(s,tree).items,item=>item.id);
const last=s=>s.requests.at(-1);
const change=(s,value)=>{toggle(s.draw()).props.onChange({currentTarget:{checked:value}});return s.draw();};

(async()=>{
 // React can defer a functional state updater after its response guard ran.
 // Toggle before render: that old updater must not repopulate the erased cache.
 for(const append of [false,true]){
  const race=await scene().start();let frame=race.chooseSource('subscriptions'),request=last(race);
  if(append){frame=await race.settle(request,page(['old-head'],24));request=last(race);race.edge();race.draw();}
  const previousToggle=toggle(frame).props.onChange;
  request.resolve(page([append?'old-tail':'old-head'],append?48:24));await tick();
  previousToggle({currentTarget:{checked:true}});frame=race.draw();
  assert.deepEqual(identifiers(race,frame),[]);
  frame=await race.settle(last(race),page(['current-on']));
  frame=change(race,false);
  assert.deepEqual(identifiers(race,frame),[],(append?'append':'head')+' updater queued before toggle cannot resurrect its old cache on return');
  assert.equal(params(last(race)).get('offset'),null);race.close();
 }
 const threshold=await scene().start();threshold.chooseSource('subscriptions');
 let thresholdTree=await threshold.settle(last(threshold),page(['long-video'],null,{minimumVideoMinutes:5}));
 assert.ok(!nodes(thresholdTree).some(n=>n.type==='Text'&&/^Off hides/.test(String(n.props.children??''))),'the switch has no always-visible minimum helper');
 assert.equal(toggle(thresholdTree).props.checked,false);
 change(threshold,true);thresholdTree=await threshold.settle(last(threshold),page(['short-video'],null,{minimumVideoMinutes:0}));
 assert.ok(!nodes(thresholdTree).some(n=>n.type==='Text'&&/^Off hides/.test(String(n.props.children??''))),'zero has no replacement helper');
 assert.equal(toggle(thresholdTree).props.checked,true);assert.deepEqual(identifiers(threshold,thresholdTree),['short-video']);threshold.close();
 const s=await scene().start();assert.equal(toggle(s.draw()),undefined,'All sources does not receive a Shorts control');
 // Featured-title reset belongs to the retained detailed Poster grid view.
 nodes(s.draw()).find(n=>n.props?.['aria-label']==='Poster grid').props.onClick();
 assert.equal(results(s,s.draw()).layout,'grid','exercise the existing featured panel explicitly instead of the artwork-only default');
 let tree=s.chooseSource('subscriptions'),head=last(s);
 assert.equal(toggle(tree).props.checked,false,'Discover defaults off even when the independent channel preference is true');
 assert.equal(params(head).get('show_shorts'),'0');assert.equal(params(head).get('offset'),null);assert.equal(params(head).get('sort'),'new');
 assert.equal(s.storageReads.includes('reeldock-channel-show-shorts'),false);
 const switchNode=toggle(tree),toolbar=nodes(tree).find(node=>node.type==='Group'&&node.props.mih===44&&nodes(node.props.children).includes(switchNode));
 assert.ok(toolbar);assert.equal(toolbar.props.wrap,'wrap');assert.equal(toolbar.props.align,'center');
 for(const scheme of ['light','dark'])for(const width of [320,768,1440]){
  const html=renderToStaticMarkup(React.createElement(Mantine.MantineProvider,{forceColorScheme:scheme},React.createElement('div',{style:{width}},React.createElement(Mantine.Switch,switchNode.props))));
  assert.match(html,/role="switch"/);assert.match(html,/Show Shorts/);assert.match(html,/min-height:44px/);assert.doesNotMatch(html,/<input[^>]*\schecked(?:=|\s|>)/);assert.equal((html.match(/<input\b/g)||[]).length,1);
 }
 tree=await s.settle(head,page(['normal-0','unknown-0'],24));
 const warm=last(s);assert.equal(params(warm).get('show_shorts'),'0');assert.equal(params(warm).get('offset'),'24');
 const count=s.calls.length;s.edge();s.edge();s.draw();assert.equal(s.calls.length,count,'prefetch and repeated scroll consume one shared GET');
 tree=await s.settle(warm,page(['normal-24'],48));assert.deepEqual(identifiers(s,tree),['normal-0','unknown-0','normal-24']);
 const oldTail=last(s);assert.equal(params(oldTail).get('offset'),'48');
 // Preserve exact media actions while opening the ordinary shared sheet.
 results(s,tree).onOpen(results(s,tree).items[0]);tree=s.draw();assert.ok(nodes(tree).some(n=>n.props?.video?.id==='normal-0'));
 tree=change(s,true);const withShorts=last(s);
 assert.equal(toggle(tree).props.checked,true);assert.deepEqual(identifiers(s,tree),[],'old cards and hero are cleared immediately');
 assert.equal(nodes(tree).some(n=>n.props?.video?.id==='normal-0'),false,'selected detail sheet closes on filter change');
 assert.equal(nodes(tree).some(n=>n.props?.['aria-label']==='Featured media'),false);
 assert.equal(head.signal.aborted,true);assert.equal(oldTail.signal.aborted,true,'old prefetch is cancelled');
 assert.equal(params(withShorts).get('show_shorts'),'1');assert.equal(params(withShorts).get('offset'),null,'new filter starts from its first server page');
 tree=await s.settle(withShorts,page(['short-on','normal-on'],24));assert.deepEqual(identifiers(s,tree),['short-on','normal-on']);
 const onWarm=last(s);assert.equal(params(onWarm).get('show_shorts'),'1');assert.equal(params(onWarm).get('offset'),'24');
 tree=await s.settle(oldTail,page(['must-not-leak'],null));assert.deepEqual(identifiers(s,tree),['short-on','normal-on'],'late old-filter append cannot cross the generation fence');
 const hero=nodes(tree).find(n=>n.props?.['aria-label']==='Featured media');assert.ok(nodes(hero).some(n=>n.type==='Title'&&n.props.children==='Title short-on'),'new mode resets featured index');
 tree=change(s,false);const withoutShorts=last(s);assert.equal(onWarm.signal.aborted,true);assert.equal(params(withoutShorts).get('show_shorts'),'0');assert.equal(params(withoutShorts).get('offset'),null);
 assert.deepEqual(identifiers(s,tree),[],'a cached paginated old filter never restores its tail while the first page reloads');
 // Race two initial filter reads, including an old coalesced response which
 // finishes after its owner has aborted. Only the final exact scope may win.
 change(s,true);const staleHead=last(s);change(s,false);const finalHead=last(s);
 assert.equal(staleHead.signal.aborted,true);assert.notEqual(staleHead.url,finalHead.url,'0/1 have different API/coalescing identities');
 tree=await s.settle(finalHead,page(['final-normal']));tree=await s.settle(staleHead,page(['stale-short']));tree=await s.settle(withoutShorts,page(['stale-normal-tail']));
 assert.deepEqual(identifiers(s,tree),['final-normal']);assert.equal(toggle(tree).props.checked,false);
 for(const source of ['discover','vod','local','all']){
  tree=s.chooseSource(source);assert.equal(toggle(tree),undefined,source+' has no unrelated switch');assert.equal(params(last(s)).has('show_shorts'),false,source+' query is unchanged');
 }
 tree=s.chooseSource('subscriptions');tree=s.search('shared query');assert.equal(toggle(tree),undefined,'global search has no source-specific switch');
 assert.match(last(s).url,/^\/api\/view-media\/search\?/);assert.equal(params(last(s)).has('show_shorts'),false);
 assert.equal(s.calls.some(call=>call.body!==undefined),false,'filtering performs GETs only');assert.deepEqual(s.storageWrites,[],'no settings, channel preference or browser preference writes');
 s.close();assert.equal(last(s).signal.aborted,true);console.log('Discover YouTube Shorts: default-off Mantine44px switch, strict0/1 head+prefetch+scroll queries, coalescing, stale-generation/cache/selection/hero reset and isolated GET-only scopes passed.');
})().catch(error=>{console.error(error);process.exitCode=1;});
