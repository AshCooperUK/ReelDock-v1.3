const assert=require('node:assert/strict'),fs=require('node:fs'),vm=require('node:vm'),ts=require('typescript'),React=require('react'),M=require('@mantine/core'),{renderToStaticMarkup}=require('react-dom/server');
const compile=file=>ts.transpileModule(fs.readFileSync(__dirname+'/src/'+file,'utf8'),{compilerOptions:{target:ts.ScriptTarget.ES2022,module:ts.ModuleKind.CommonJS,jsx:ts.JsxEmit.ReactJSX}}).outputText;
const flush=async()=>{for(let index=0;index<16;index++)await Promise.resolve();};
const response=(status,result)=>({ok:status>=200&&status<300,status,json:async()=>result,headers:{get:()=>null}});
function setup(fetch){
 const notifications=[],api={},state=[],effects=[],requests=[];let cursor=0;
 vm.runInNewContext(compile('api.ts'),{exports:api,AbortController,Error,Date,FormData,fetch:(...args)=>{requests.push(args);return fetch(...args);},setTimeout,clearTimeout,require:name=>name==='./plainNames'?{cleanDisplay:value=>value}:{notify:value=>notifications.push(value)}});
 const react={useState:initial=>{const at=cursor++;if(!(at in state))state[at]=initial;return[state[at],next=>state[at]=typeof next==='function'?next(state[at]):next];},useEffect:effect=>effects.push(effect),useRef:current=>({current})};
 const exports={};vm.runInNewContext(compile('discovery/MovieInformation.tsx'),{exports,AbortController,URLSearchParams,require:name=>name==='react'?react:name==='../api'?api:name==='../mediaDate'?require('./media-date-test-helper.cjs')():name==='../components/ui'?{Button:M.Button}:require(name)});
 return{exports,notifications,requests,state,effects,render(item,kind='movie'){cursor=0;effects.length=0;return kind==='movie'?exports.useMovieInformation(item):exports.useShowInformation(item);},run(){return effects.at(-1)();}};
}
const movie={id:'movie:1234567',kind:'movie',title:'Movie',externalId:'1234567'},show={id:'show:77',kind:'show',title:'Show',externalId:'77'};
(async()=>{
 for(const [status,result,unavailable]of [
  [200,{facts:{},cast:[],source:'',unavailable:true},true],
  [200,{facts:{Runtime:'98 min'},cast:[],source:'Saved TMDB'},false],
  [200,{facts:{Genres:'Crime'},cast:[],source:'TMDB'},false],
  [400,{error:'TMDB rejected the API credential'},true],
  [502,{error:'Optional provider unavailable'},true],
 ])for(const kind of ['movie','show']){
  const item=kind==='movie'?movie:show,s=setup(async()=>response(status,result));
  s.render(item,kind);const cleanup=s.run();await flush();const state=s.render(item,kind);
  assert.equal(state.busy,false);assert.equal(state.unavailable,unavailable);
  assert.equal(s.notifications.length,0,'an optional read never creates a global duplicate toast');
  assert.equal(s.requests.length,1);assert.equal(s.requests[0][1].credentials,'same-origin');
  const html=renderToStaticMarkup(React.createElement(M.MantineProvider,null,s.exports.MovieCast({...state,titleKind:kind})));
  assert.equal((html.match(/Extra (?:movie|TV show) information is unavailable right now\./g)||[]).length,unavailable?1:0,'the detail panel is the sole feedback owner');
  if(status===200&&!unavailable)assert.equal(state.info.source,result.source);
  cleanup();assert.equal(s.requests[0][1].signal.aborted,false,'settled reads need no transport cancellation');
 }
 let finish;
 const cancelled=setup(()=>new Promise(resolve=>finish=resolve));
 cancelled.render(movie);const close=cancelled.run();close();finish(response(200,{facts:{Runtime:'98 min'},cast:[],source:'TMDB'}));await flush();
 assert.equal(cancelled.state[0],null,'a closed request cannot commit late optional metadata');assert.equal(cancelled.notifications.length,0);
 cancelled.render(show,'movie');cancelled.run();assert.equal(cancelled.render(show,'movie').busy,false,'changing to a nonmatching kind clears an old busy state without another request');

 const pending=[];const switched=setup(()=>new Promise(resolve=>pending.push(resolve)));
 switched.render(movie);const old=switched.run();old();
 const next={...movie,id:'movie:7654321',externalId:'7654321'};switched.render(next);switched.run();
 pending[1](response(200,{facts:{Runtime:'42 min'},cast:[],source:'Current cached title'}));await flush();
 pending[0](response(200,{facts:{Runtime:'98 min'},cast:[],source:'Old title'}));await flush();
 assert.equal(switched.render(next).info.source,'Current cached title','late results cannot overwrite another exact identity');
 assert.equal(switched.notifications.length,0);
 console.log('Optional movie/show metadata: keyless/cached/valid/invalid/provider failures have one inline feedback owner; cancellation and identity changes retain no stale result.');
})().catch(error=>{console.error(error);process.exitCode=1;});
