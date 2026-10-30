const assert=require('node:assert/strict'),fs=require('node:fs'),vm=require('node:vm'),ts=require('typescript');
const React=require('react'),Mantine=require('@mantine/core'),{renderToStaticMarkup}=require('react-dom/server');
const h=React.createElement;
function compile(file,imports={}){
 const out={};
 vm.runInNewContext(ts.transpileModule(fs.readFileSync(__dirname+'/src/'+file,'utf8'),{compilerOptions:{module:ts.ModuleKind.CommonJS,jsx:ts.JsxEmit.ReactJSX}}).outputText,{exports:out,require:name=>imports[name]||(name.endsWith('.module.css')?{default:{}}:require(name))});
 return out;
}
const status=compile('components/ActivityStatus.tsx');
const rows=compile('components/ListRow.tsx',{'@mantine/hooks':{useMediaQuery:()=>false}});
let jobs=[];const writes=[];
const ui=new Proxy({Button:({variant,...props})=>h(Mantine.Button,{...props,variant:variant==='secondary'?'default':variant})},{get:(target,key)=>target[key]||(()=>null)});
const hooks={...React,useState:initial=>[Array.isArray(initial)?jobs:initial,()=>{}],useEffect:()=>{},useCallback:fn=>fn};
const generic=new Proxy({__esModule:true,default:()=>null},{get:(target,key)=>target[key]||(()=>null)});
const source={};
vm.runInNewContext(ts.transpileModule(fs.readFileSync(__dirname+'/src/NzbDownloads.tsx','utf8'),{compilerOptions:{module:ts.ModuleKind.CommonJS,jsx:ts.JsxEmit.ReactJSX}}).outputText,{exports:source,require:name=>({react:hooks,'react/jsx-runtime':require('react/jsx-runtime'),'@mantine/core':Mantine,'./components/ui':ui,'./components/ListRow':rows,'./components/ActivityStatus':status,'./api':{api:async(...args)=>{writes.push(args);return{};}},'./mediaDate':{mediaDate:value=>value},'./TorrentDownloads':{TorrentName:({name})=>h('span',null,name),nativeMetadataSummary:()=>''}}[name]||generic)});
const display=source.nzbActivityStatus;
const observed=Object.freeze({state:'downloading',generation:71,attempt:'a'.repeat(64)});
const base={id:'job/exact',name:'Fixture NZB',state:'downloading',progress:1,files:[],placements:[],published:[],servers:[],down_rate:17_000_000,connections:30,error:'',stop_reason:'',activity_observation:observed};
const phases={assembling:'Assembling',repairing:'Repairing',unpacking:'Extracting',extracting:'Extracting',processing:'Processing',publishing:'Publishing'};
const markup=tree=>renderToStaticMarkup(h(Mantine.MantineProvider,{forceColorScheme:'dark'},tree));
function children(node){return Array.isArray(node)?node.flatMap(children):node&&typeof node==='object'?[node,...children(node.props?.children)]:[];}
const controller=source.useNzbActivity('exact-csrf');
for(const [phase,label] of Object.entries(phases)){
 const job=Object.freeze({...base,progress_stage:phase});
 assert.equal(display(job).state,'processing');assert.equal(display(job).label,label);
 assert.equal(controller.stateOf(job),'processing');
 const row=controller.card(job);
 assert.equal(row.props.badge.props.state,'processing');assert.equal(row.props.badge.props.phaseLabel,label);
 assert.equal(row.props.statusColour,'violet');assert.equal(row.props.progress.props.value,100,'article fetch progress is retained');
 const html=markup(h(rows.ActivityQueueRows,null,row));
 assert.match(html,/data-state="processing"/);assert.ok(html.includes('>'+label+'</span>'));
 assert.ok(!html.includes('>Downloading</span>'),'post-download work is not labelled as active transfer');
 jobs=[job];const summaryTree=source.NzbProgress({csrf:'exact-csrf'}),summary=markup(summaryTree);
 assert.ok(summary.includes('>'+label+'</span>'),'summary and main Activity share the exact stage label');
 const summaryRow=children(summaryTree).find(node=>node.type===rows.default);
 assert.match(markup(summaryRow.props.details),/0 B\/s/,'completed transfer does not advertise stale download speed');
 const entry={identity:'nzb:'+job.id,state:'processing',phase,data:{kind:'nzb',item:job}};
 const captured=require('./activity-clear-test-helper.cjs')().captureActivityClear(entry);
 assert.equal(captured.observed.state,'downloading','Clear uses the original server state, never the presentation label');
 assert.equal(captured.observed.generation,71);assert.equal(captured.observed.attempt,observed.attempt);
 assert.equal(job.activity_observation,observed);assert.equal(job.state,'downloading');assert.equal(job.progress_stage,phase);
}
for(const phase of [undefined,'','downloading','unknown'])assert.equal(display({...base,progress_stage:phase}).state,'downloading','only known post-download stages become Processing');
for(const [raw,state,label] of [['failed','failed','Failed'],['paused','queued','Paused'],['choose','queued','Choose type'],['queued','queued',undefined],['stopped','complete','Completed']]){
 const job={...base,state:raw,progress_stage:'publishing'};
 assert.equal(display(job).state,state);assert.equal(display(job).label,label,'authoritative terminal/paused/queued state outranks stale stage');
 const row=controller.card(job);assert.equal(row.props.badge.props.state,state);assert.equal(row.props.statusColour,raw==='failed'?'red':raw==='stopped'?'green':'yellow');
}
for(const [reason,label]of [['vpn','Waiting for VPN'],['downloads','Downloads paused'],['hold','Downloads stopped'],['user','Paused'],['unknown','Paused'],['','Paused']]){
 const observed=Object.freeze({state:'paused',generation:71,attempt:'b'.repeat(64)});
 const job=Object.freeze({...base,state:'paused',pause_reason:reason,progress:.37,activity_observation:observed});
 assert.equal(display(job).state,'queued');assert.equal(display(job).label,label);
 const row=controller.card(job);
 assert.equal(row.props.badge.props.state,'queued');assert.equal(row.props.badge.props.phaseLabel,label);assert.equal(row.props.statusColour,'yellow');
 const details=markup(row.props.details);
 assert.equal(details.includes('Downloads resume automatically when the VPN connection is ready.'),reason==='vpn');
 assert.match(details,/0 B\/s/,'waiting transfer never advertises a stale speed');
 jobs=[job];const summaryTree=source.NzbProgress({csrf:'exact-csrf'});
 const summaryRow=children(summaryTree).find(node=>node.type===rows.default);
 assert.equal(summaryRow.props.badge.props.phaseLabel,label);assert.equal(summaryRow.props.progress.props.value,37);
 assert.match(markup(summaryRow.props.details),/37%.*0 B\/s/,'summary preserves progress and stops stale-rate display');
 const actions=children(row.props.actions).filter(node=>node.props?.onClick);
 assert.deepEqual(actions.map(node=>node.props.children),['Resume','Remove'],'pause reason changes no existing actions');
 const captured=require('./activity-clear-test-helper.cjs')().captureActivityClear({identity:'nzb:'+job.id,state:'queued',data:{kind:'nzb',item:job}});
 assert.equal(captured.observed.state,'paused');assert.equal(captured.observed.attempt,observed.attempt);
 assert.equal(job.state,'paused');assert.equal(job.progress,.37);assert.equal(job.activity_observation,observed);
}
for(const state of ['queued','downloading','failed','stopped'])assert.notEqual(display({...base,state,pause_reason:'vpn'}).label,'Waiting for VPN','only a paused job can carry a waiting reason');
(async()=>{
 const row=controller.card({...base,progress_stage:'publishing'});
 const actions=children(row.props.actions).filter(node=>node.props?.onClick);
 assert.equal(actions.length,2);assert.equal(actions[0].props.children,'Pause');assert.equal(actions[1].props.children,'Remove');
 actions.forEach(action=>action.props.onClick());await new Promise(setImmediate);
 assert.equal(writes.length,2);
 for(const [index,args] of writes.entries()){assert.equal(args[0],'/api/nzb/job%2Fexact');assert.equal(args[2].action,index===0?'pause':'remove');assert.equal(args[3],'exact-csrf');}
 console.log('NZB stage presentation: real queue/summary SSR, Processing phases, authoritative state precedence, unchanged 100% progress/Clear observation and exact actions passed.');
})();
