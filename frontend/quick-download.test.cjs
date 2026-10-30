const assert=require('node:assert/strict'),fs=require('node:fs'),vm=require('node:vm'),ts=require('typescript'),React=require('react'),Mantine=require('@mantine/core'),{renderToStaticMarkup}=require('react-dom/server');
const compile=file=>ts.transpileModule(fs.readFileSync(__dirname+'/src/'+file,'utf8'),{compilerOptions:{target:ts.ScriptTarget.ES2022,module:ts.ModuleKind.CommonJS,jsx:ts.JsxEmit.ReactJSX}}).outputText;
const flush=async()=>{for(let index=0;index<16;index++)await Promise.resolve();};
const response=(status,result)=>({ok:status>=200&&status<300,status,json:async()=>result,headers:{get:()=>null}});
const layout={};vm.runInNewContext(compile('components/DetailActions.tsx'),{exports:layout,require});
function scene(fetch){
 const cells=[],effects=[],requests=[],notifications=[],events=[];let cursor=0;
 const same=(a,b)=>a&&b&&a.length===b.length&&a.every((v,i)=>Object.is(v,b[i]));
 const hooks={useRef(initial){const at=cursor++;if(!(at in cells))cells[at]={current:initial};return cells[at];},useState(initial){const at=cursor++;if(!(at in cells))cells[at]=typeof initial==='function'?initial():initial;return[cells[at],next=>cells[at]=typeof next==='function'?next(cells[at]):next];},useEffect(effect,deps){const at=cursor++,old=cells[at];if(!old||!same(old.deps,deps))effects.push(()=>{old?.cleanup?.();cells[at]={deps,cleanup:effect()};});}};
 const api={};vm.runInNewContext(compile('api.ts'),{exports:api,AbortController,Error,Date,FormData,setTimeout,clearTimeout,fetch:(...args)=>{requests.push(args);return fetch(...args);},require:name=>name==='./plainNames'?{cleanDisplay:value=>value}:{notify:value=>notifications.push(value)}});
 const out={};vm.runInNewContext(compile('QuickDownload.tsx'),{exports:out,AbortController,Error,window:{dispatchEvent:event=>events.push(event)},CustomEvent:class{constructor(type,options){this.type=type;this.detail=options?.detail;}},require:name=>name==='react'?hooks:name==='./api'?api:name==='./components/DetailActions'?layout:require(name)});
 return{requests,notifications,events,draw(props){cursor=0;effects.length=0;const node=out.default(props);effects.splice(0).forEach(effect=>effect());return node;},close(){for(const cell of cells)cell?.cleanup?.();}};
}
function nodes(node){if(!node||typeof node!=='object')return[];return[node,...React.Children.toArray(node.props?.children).flatMap(nodes),...React.Children.toArray(node.props?.feedback).flatMap(nodes),...React.Children.toArray(node.props?.metadata).flatMap(nodes)];}
const action=node=>nodes(node).find(value=>value.type===Mantine.ActionIcon&&/^(Quick download|Choose an episode|Download all seasons)/.test(value.props['aria-label']||''));
const movie={id:'movie:1234567',kind:'movie',title:'Fixture movie',externalId:'1234567'};
const props={item:movie,csrf:'fixture-csrf',onChoices(){}};
(async()=>{
 let finish;const s=scene(()=>new Promise(resolve=>finish=resolve));let node=s.draw(props);
 assert.equal(action(node).props.size,44);assert.equal(action(node).props.miw,44);assert.equal(action(node).props.mih,44);
 assert.equal(s.requests.length,0,'Opening a detail header never starts acquisition');
 assert.equal(action(node).props['aria-label'],'Quick download with saved settings');assert.equal(action(node).props.type,'button');
 action(node).props.onClick();action(node).props.onClick();assert.equal(s.requests.length,1,'double activation makes one non-retried write');
 assert.equal(s.requests[0][0],'/api/quick-download');assert.deepEqual(JSON.parse(s.requests[0][1].body),{item:movie});
 assert.equal(s.requests[0][1].headers['X-CSRF-Token'],'fixture-csrf');assert.equal(s.requests[0][1].credentials,'same-origin');
 node=s.draw(props);assert.equal(action(node).props['aria-busy'],true);assert.equal(action(node).props.disabled,true);
 finish(response(200,{state:'queued',message:'Queued with your saved settings.'}));await flush();node=s.draw(props);
 assert.equal(action(node).props['aria-busy'],false);assert.equal(action(node).props.disabled,true);
 assert.equal(nodes(node).filter(n=>n.props?.role==='status').length,1);assert.equal(s.notifications.length,0);
 const html=renderToStaticMarkup(React.createElement(Mantine.MantineProvider,{forceColorScheme:'dark'},node));
 assert.match(html,/<button[^>]*aria-label="Quick download with saved settings"/);assert.match(html,/Queued with your saved settings/);
 // One wrapping unit contains all three44pxicons; a long duration badge and
 // asynchronous feedback occupy separate slots, never forcing Quick to wrap.
 const cluster=scene(async()=>response(200,{state:'queued',message:'Queued using saved settings.'}));
 const clusterProps={...props,metadata:React.createElement(Mantine.Badge,null,'Length unavailable'),actions:React.createElement(Mantine.Group,{gap:'xs'},React.createElement(Mantine.ActionIcon,{size:44,'aria-label':'Favourite'}),React.createElement(Mantine.ActionIcon,{size:44,'aria-label':'Watchlist'}))};
 const clusterNode=cluster.draw(clusterProps),structure=layout.default(clusterNode.props);
 const unit=nodes(structure).find(value=>value.props?.['data-detail-action-unit']);
 const iconRow=nodes(unit).find(value=>value.props?.['data-detail-action-icons']);
 assert.equal(iconRow.props.wrap,'nowrap');assert.equal(nodes(iconRow).filter(value=>value.type===Mantine.ActionIcon).length,3);
 assert.ok(nodes(structure).some(value=>value.props?.['data-detail-action-metadata']));
 assert.ok(!nodes(iconRow).some(value=>value.type===Mantine.Badge),'duration can wrap separately from the icon cluster');
 action(clusterNode).props.onClick();await flush();const settled=cluster.draw(clusterProps),settledLayout=layout.default(settled.props);
 const settledIcons=nodes(settledLayout).find(value=>value.props?.['data-detail-action-icons']);
 assert.equal(nodes(settledIcons).filter(value=>value.props?.role==='status').length,0,'feedback is below the nonwrapping controls');
 assert.equal(nodes(settledLayout).filter(value=>value.props?.role==='status').length,1);
 assert.match(renderToStaticMarkup(React.createElement(Mantine.MantineProvider,null,settled)),/data-detail-action-icons/);cluster.close();

 for(const restriction of[{csrf:''},{readOnly:true},{disabled:true}]){
  const restricted=scene(()=>{throw Error('A restricted header wrote a request');});const blocked=restricted.draw({...props,...restriction});
  assert.equal(action(blocked).props.disabled,true);action(blocked).props.onClick();assert.equal(restricted.requests.length,0);restricted.close();
 }
 let choices=0,finishShow;const series={kind:'show',id:'show:7',title:'Show',externalId:'7'};
 const show=scene(()=>new Promise(resolve=>finishShow=resolve)),showProps={...props,item:series,onChoices:()=>choices++};let showNode=show.draw(showProps);
 assert.equal(action(showNode).props['aria-label'],'Download all seasons with saved settings');assert.equal(show.requests.length,0);
 action(showNode).props.onClick();action(showNode).props.onClick();assert.equal(show.requests.length,1,'one series action accepts one whole-show write');
 assert.deepEqual(JSON.parse(show.requests[0][1].body),{item:series},'all seasons are a server policy, never the displayed episode page');assert.equal(show.requests[0][1].headers['X-CSRF-Token'],'fixture-csrf');
 assert.equal(action(show.draw(showProps)).props.disabled,true);finishShow(response(202,{state:'searching',downloadAll:true,message:'Requested all seasons using your saved Usenet & NZB settings.'}));await flush();showNode=show.draw(showProps);
 assert.equal(action(showNode).props.disabled,true);assert.equal(choices,0,'accepted series enrollment does not open an episode chooser');assert.equal(show.events.length,1);assert.equal(show.events[0].type,'reeldock-show-options-changed');assert.equal(JSON.stringify(show.events[0].detail),JSON.stringify({key:series.id,field:'downloadAll'}),'accepted whole-series writes invalidate mounted settings and collection state');assert.equal(nodes(showNode).filter(n=>n.props?.role==='status').length,1);
 const seriesHtml=renderToStaticMarkup(React.createElement(Mantine.MantineProvider,null,showNode));assert.match(seriesHtml,/aria-label="Download all seasons with saved settings"/);assert.match(seriesHtml,/Requested all seasons/);
 const unauthenticatedShow=scene(()=>{throw Error('A series without CSRF wrote a request');});const noToken=unauthenticatedShow.draw({...showProps,csrf:''});assert.equal(action(noToken).props.disabled,true);action(noToken).props.onClick();assert.equal(unauthenticatedShow.requests.length,0);unauthenticatedShow.close();
 const navigation=scene(()=>{throw Error('An explicit navigation-only control made an acquisition');});const navigationNode=navigation.draw({...showProps,csrf:'',navigateOnly:true});action(navigationNode).props.onClick();assert.equal(choices,1);assert.equal(navigation.requests.length,0);navigation.close();

 const choice=scene(async()=>response(200,{state:'choice_required',message:'Choose an episode or source.'}));const choiceProps={...props,onChoices:()=>choices++};
 action(choice.draw(choiceProps)).props.onClick();await flush();const choiceNode=choice.draw(choiceProps);
 assert.equal(choices,2);action(choiceNode).props.onClick();assert.equal(choices,3);assert.equal(choice.requests.length,1,'an already-known choice opens Downloads without another POST');assert.equal(choice.events.length,0,'choice-only feedback never announces a policy write');

 const failed=scene(async()=>response(503,{error:'The provider is unavailable.'}));action(failed.draw(props)).props.onClick();await flush();const failure=failed.draw(props);
 assert.equal(failed.requests.length,1,'writes are never retried on503');assert.equal(failed.events.length,0);assert.equal(failed.notifications.length,0);
 assert.equal(nodes(failure).filter(n=>n.props?.role==='alert').length,1);assert.equal(action(failure).props.disabled,false);

 let metadataResult;const metadata=scene(()=>new Promise(resolve=>metadataResult=resolve));action(metadata.draw(props)).props.onClick();
 const metadataSignal=metadata.requests[0][1].signal;
 metadata.draw({...props,item:{...movie,description:'Cached details have arrived',year:'2026'}});
 assert.equal(metadataSignal.aborted,false,'same-title metadata enrichment must not abandon an accepted action');
 metadataResult(response(200,{state:'queued',message:'Current title queued'}));await flush();
 assert.equal(nodes(metadata.draw({...props,item:{...movie,description:'Cached details have arrived',year:'2026'}})).filter(n=>n.props?.role==='status').length,1);
 metadata.close();

 let late;const switched=scene(()=>new Promise(resolve=>late=resolve));action(switched.draw(props)).props.onClick();const request=switched.requests[0][1];
 const next={...props,item:{...movie,id:'movie:7654321',externalId:'7654321'}};const changed=switched.draw(next);
 assert.equal(request.signal.aborted,true);assert.equal(action(changed).props['aria-busy'],false,'another identity never inherits an old spinner');
 late(response(200,{state:'queued',message:'Old title queued'}));await flush();assert.equal(nodes(switched.draw(next)).filter(n=>n.props?.role==='status').length,0,'late result never enters the next title');

 let closedResult;const closed=scene(()=>new Promise(resolve=>closedResult=resolve));action(closed.draw(props)).props.onClick();closed.close();
 assert.equal(closed.requests[0][1].signal.aborted,true);closedResult(response(200,{state:'queued',message:'Closed title queued'}));await flush();
 assert.equal(closed.notifications.length,0);assert.equal(closed.events.length,0);

 const malformed=scene(async()=>response(200,{state:'unknown',message:'Not a confirmed download'}));action(malformed.draw(props)).props.onClick();await flush();
 assert.equal(nodes(malformed.draw(props)).filter(n=>n.props?.role==='alert').length,1);assert.equal(malformed.requests.length,1);
 for(const current of[s,show,choice,failed,switched,malformed])current.close();
 console.log('Quick download:44px realMantinecontrol,exactCSRFonewrite,savedsettingspayload,whole-series enrollment and choice navigation,oneinlinefeedbackowner,restrictions,cancellation/identity guards.');
})().catch(error=>{console.error(error);process.exitCode=1;});
