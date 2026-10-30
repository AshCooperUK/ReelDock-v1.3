const assert=require('node:assert/strict'),fs=require('node:fs'),vm=require('node:vm'),ts=require('typescript');
const jsx=(type,props)=>({type,props:props||{}}),code=file=>ts.transpileModule(fs.readFileSync(__dirname+'/src/'+file,'utf8'),{compilerOptions:{target:ts.ScriptTarget.ES2022,module:ts.ModuleKind.CommonJS,jsx:ts.JsxEmit.ReactJSX}}).outputText;
function load(file,imports={},globals={}){const out={};vm.runInNewContext(code(file),{exports:out,Error,Date,Intl,AbortController,URLSearchParams,setTimeout:()=>1,clearTimeout(){},setInterval:()=>1,clearInterval(){},...globals,require:name=>name==='react/jsx-runtime'?{jsx,jsxs:jsx,Fragment:'Fragment'}:imports[name]||new Proxy({},{get:(_,key)=>key==='__esModule'?false:key==='default'?name.split('/').pop():key})});return out;}
const nodes=node=>node==null?[]:Array.isArray(node)?node.flatMap(nodes):typeof node==='object'?[node,...nodes(node.props?.children),...nodes(node.props?.actions)]:[];
const store=load('notifications-state.ts'),actualNotifications=load('notifications.tsx',{react:{useRef:value=>({current:value}),useEffect:fn=>fn()},'./notifications-state':store});
function scene(notice,error='',noticeKind){let cursor=0;const values=[],react={useState:initial=>{const index=cursor++;if(!(index in values))values[index]=typeof initial==='function'?initial():initial;return[values[index],next=>values[index]=typeof next==='function'?next(values[index]):next];},useRef:value=>({current:value}),useEffect(){}};let retries=0;
 const guide=load('Guide.tsx',{react,'@mantine/hooks':{useMediaQuery:()=>false},'./pageSearch':{usePageSearch:()=>''},'./guideData':{shiftDay:(date,count)=>new Date(Date.parse(date+'T12:00Z')+count*86400000).toISOString().slice(0,10),sortChannels:rows=>rows},'./mediaDate':{mediaDate:date=>date},'./notifications':{NotificationMessage:'NotificationMessage'}});
 const live={csrf:'fixture',channels:[],date:'2026-10-19',today:'2026-10-24',now:12,loading:false,error,notice,noticeKind,truncated:false,saving:false,changeDay(){},selectDate(){},favourite(){},retry(){retries++;}};
 return{guide,live,get retries(){return retries;},draw(){cursor=0;return guide.default({live,favouritesOnTop:true,onFavouritesOnTop(){}});}};
}
function publish(tree){for(const node of nodes(tree).filter(node=>node.type==='NotificationMessage'))actualNotifications.NotificationMessage(node.props);}
for(const notice of [
 'Updating this week: 299 channels remaining. Showing saved videos as they arrive. Using upload feeds and yt-dlp.',
 'Updating this week: 298 channels remaining. Showing saved videos as they arrive.',
 'YouTube updates are paused. Showing saved videos for this week.',
 'Some channels could not update yet. Showing saved videos; retries are queued.',
])for(let visit=0;visit<2;visit++){
 const view=scene(notice),tree=view.draw(),progress=nodes(tree).filter(node=>node.props?.['data-guide-progress']!==undefined);
 assert.equal(progress.length,1,'one quiet progress owner per Guide');assert.equal(progress[0].props.role,'status');assert.equal(progress[0].props['aria-live'],'polite');assert.equal(progress[0].props.children,notice);assert.equal(nodes(tree).filter(node=>node.type==='NotificationMessage').length,0,'ordinary progress has no outcome notifier or Retry');publish(tree);assert.equal(store.notificationSnapshot().items.length,0,'opening/polling Guide progress never creates a toast');
}
const warning='YouTube connection needs attention. Check saved access settings.',failed=scene(warning);
let tree=failed.draw(),notification=nodes(tree).find(node=>node.type==='NotificationMessage');assert.equal(notification.props.kind,'warning');assert.equal(notification.props.id,'guide-warning-2026-10-19');notification.props.actions.props.onClick();assert.equal(failed.retries,1);publish(tree);publish(failed.draw());assert.equal(store.notificationSnapshot().items.length,1,'repeated provider failures share one stable warning');assert.equal(store.notificationSnapshot().items[0].revision,1);
const read=scene('', 'Could not read this week.');tree=read.draw();notification=nodes(tree).find(node=>node.type==='NotificationMessage');assert.equal(notification.props.id,'guide-error-2026-10-19');assert.equal(notification.props.kind,'error');notification.props.actions.props.onClick();assert.equal(read.retries,1);publish(tree);publish(read.draw());assert.equal(store.notificationSnapshot().items.filter(item=>item.kind==='error').length,1,'read failure has a single retry owner');
tree=scene('Updating this week: a request failed.','','warning').draw();assert.ok(nodes(tree).some(node=>node.type==='NotificationMessage'),'a data-hook request failure cannot be hidden by progress wording');

// The data hook owns background request failures inline, so the API transport
// cannot emit a duplicate global balloon before Guide classifies the message.
const state=[],effects=[],calls=[];let index=0;
const react={useMemo:fn=>fn(),useState(initial){const i=index++;if(!(i in state))state[i]=typeof initial==='function'?initial():initial;return[state[i],next=>state[i]=typeof next==='function'?next(state[i]):next];},useEffect:(fn,deps)=>effects.push({fn,deps})};
const live=load('LiveGuide.tsx',{react,'./warmSnapshots':require('./warm-snapshots-test-helper.cjs')(),'./guideData':{londonClock:()=>({date:'2026-10-24',now:12}),weekStart:()=> '2026-10-19',shiftDay:()=> '2026-10-25',mapGuide:()=>[]},'./appNavigation':{guideDate:()=>null},'./visibility':{pageHidden:()=>false,onVisible:()=>()=>{}},'./api':{api:(...args)=>{calls.push(args);return args[2]?Promise.reject(Error('Week preparation failed.')):Promise.resolve({channels:[],uploads:[],forecasts:[],truncated:false,notice:'Updating this week: 299 channels remaining.'});}}},{location:{search:''},window:{addEventListener(){},removeEventListener(){}},localStorage:{getItem(){return null;}}});
(async()=>{
 live.default({csrf:'fixture'});const cleanup=effects.find(effect=>effect.deps?.length===3).fn();await new Promise(setImmediate);index=0;const value=live.default({csrf:'fixture'});assert.equal(value.props.live.notice,'Week preparation failed.');assert.equal(value.props.live.noticeKind,'warning');assert.equal(calls.length,2);assert.ok(calls.every(call=>call[4].feedback==='inline'),'both prepare and saved-week reads suppress duplicate transport outcomes');cleanup();
 console.log('Guide feedback: pending/paused/queued-retry progress stays inline across opens/count changes, actual warning/read failures retain deduplicated Retry, and background API feedback has one owner.');
})().catch(error=>{console.error(error);process.exitCode=1;});
