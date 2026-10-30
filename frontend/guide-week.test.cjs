const assert=require('node:assert/strict'),fs=require('node:fs'),vm=require('node:vm'),ts=require('typescript');
const states=[],effects=[],calls=[];let index=0,timer,hidden=false,resume=null;
const react={useMemo:fn=>fn(),useState(value){const i=index++;if(!(i in states))states[i]=typeof value==='function'?value():value;return[states[i],next=>{states[i]=typeof next==='function'?next(states[i]):next;}];},useEffect(fn,deps){effects.push({fn,deps});}};
const dataCode=ts.transpileModule(fs.readFileSync(__dirname+'/src/guideData.ts','utf8'),{compilerOptions:{module:ts.ModuleKind.CommonJS}}).outputText;
const data={};vm.runInNewContext(dataCode,{exports:data,require:name=>name==='./mediaDuration'?require('./media-duration-test-helper.cjs')():require(name)});
const code=ts.transpileModule(fs.readFileSync(__dirname+'/src/LiveGuide.tsx','utf8'),{compilerOptions:{module:ts.ModuleKind.CommonJS,jsx:ts.JsxEmit.ReactJSX}}).outputText;
const navCode=ts.transpileModule(fs.readFileSync(__dirname+'/src/appNavigation.ts','utf8'),{compilerOptions:{module:ts.ModuleKind.CommonJS}}).outputText;
const nav={};vm.runInNewContext(navCode,{exports:nav,URL,URLSearchParams});
const location={href:'https://reeldock.test/app/?view=TV+Guide',pathname:'/app/',search:'?view=TV+Guide',hash:''},replaced=[];
const history={state:null,replaceState(_s,_u,url){replaced.push(url);const next=new URL(url,'https://reeldock.test');Object.assign(location,{href:next.href,pathname:next.pathname,search:next.search,hash:next.hash});}};
const out={};vm.runInNewContext(code,{exports:out,AbortController,location,history,localStorage:{getItem(){return null;}},setInterval(){return 1;},clearInterval(){},setTimeout(fn){timer=fn;return 2;},clearTimeout(){timer=null;},require(name){if(name==='react')return react;if(name==='react/jsx-runtime')return{jsx:(type,props)=>props};if(name==='./appNavigation')return nav;if(name==='./warmSnapshots')return require('./warm-snapshots-test-helper.cjs')();if(name==='./visibility')return{pageHidden:()=>hidden,onVisible(fn){resume=fn;return()=>{resume=null;};}};if(name==='./guideData')return{...data,londonClock:()=>({date:'2026-10-03',now:12})};if(name==='./api')return{api:async(url,signal,body)=>{calls.push({url,body});return{channels:[],uploads:[],forecasts:[],truncated:false};}};return{};}});
const settle=()=>new Promise(resolve=>setImmediate(resolve));
(async()=>{
 let props=out.default({csrf:'test'});assert.equal(props.live.date,'2026-09-28');
 let load=effects.find(e=>e.deps?.length===3&&e.deps[0]==='2026-09-28').fn;let cleanup=load();await settle();
 assert.equal(calls.length,2);assert.equal(calls[0].url,'/api/guide/week');assert.equal(calls[0].body.start,'2026-09-28');assert.equal(calls[1].url,'/api/guide?start=2026-09-28&end=2026-10-04');
 await timer();assert.equal(calls.length,3);assert.equal(calls[2].body,undefined); // Polls only the database, never requests another provider import.
 hidden=true;await timer();assert.equal(calls.length,4);assert.equal(timer,null); // Hidden tabs stop polling.
 hidden=false;resume();await settle();assert.equal(calls.length,5);assert.ok(timer);calls.splice(3,2);
 props.live.changeDay(-7);cleanup();index=0;effects.length=0;props=out.default({csrf:'test'});
 cleanup=effects.find(e=>e.deps?.length===3&&e.deps[0]==='2026-09-21').fn();await settle();
 assert.equal(calls[3].body.start,'2026-09-21');assert.equal(calls[4].url,'/api/guide?start=2026-09-21&end=2026-09-27');cleanup();
 // The address bar follows the week shown, and a ?date= link opens the week containing that day.
 effects.find(e=>e.deps?.length===1&&e.deps[0]==='2026-09-21').fn();assert.equal(location.search,'?view=TV+Guide&date=2026-09-21');
 states.length=0;index=0;effects.length=0;location.search='?view=TV+Guide&date=2026-10-15';
 assert.equal(out.default({csrf:'test'}).live.date,'2026-10-12');
 effects.find(e=>e.deps?.length===1).fn();assert.equal(location.search,'?view=TV+Guide&date=2026-10-12');
 states.length=0;index=0;effects.length=0;
 const current=out.default({csrf:'test'});assert.equal(current.live.date,'2026-10-12');current.live.changeDay(0);index=0;effects.length=0;
 out.default({csrf:'test'});effects.find(e=>e.deps?.length===1).fn();assert.equal(location.search,'?view=TV+Guide'); // This week needs no date.
 states.length=0;index=0;effects.length=0;location.search='?view=TV+Guide&date=2026-02-30';
 assert.equal(out.default({csrf:'test'}).live.date,'2026-09-28'); // Invalid dates are ignored.
 console.log('Guide links: ?date= opens that week, the address bar follows the week, this week and invalid dates are clean.');
 console.log('Guide weeks: current week only, selected-week request, database-only polling, hidden-tab pause and cleanup passed.');
})().catch(e=>{console.error(e);process.exitCode=1;});
