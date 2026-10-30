const assert=require('node:assert/strict'),fs=require('node:fs'),vm=require('node:vm'),ts=require('typescript');
const React=require('react'),runtime=require('react/jsx-runtime');
function load(file,imports,globals={}){const result={};vm.runInNewContext(ts.transpileModule(fs.readFileSync('src/'+file,'utf8'),{compilerOptions:{module:ts.ModuleKind.CommonJS,jsx:ts.JsxEmit.ReactJSX}}).outputText,{exports:result,...globals,require:name=>name==='react/jsx-runtime'?runtime:imports[name]});return result;}
let value=false,effect,cleanup,scheduled=new Map(),sequence=0;
const Status=load('components/LoadingStatus.tsx',{'react':{useState:()=>[value,next=>{value=next;}],useEffect:fn=>{effect=fn;}},'@mantine/core':{Group:'Group',Loader:'Loader',Text:'Text'}},{setTimeout:(fn,delay)=>{assert.equal(delay,180);scheduled.set(++sequence,fn);return sequence;},clearTimeout:id=>scheduled.delete(id)}).default;
const draw=active=>Status({active,label:'Loading channels…'});
assert.equal(draw(true),null,'a fast load never immediately paints a placeholder');cleanup=effect();assert.equal(scheduled.size,1);
cleanup();assert.equal(scheduled.size,0,'finishing before the threshold cancels loading feedback');
draw(true);cleanup=effect();scheduled.values().next().value();assert.equal(draw(true).props.role,'status','a longer wait has accessible feedback');assert.equal(draw(false),null,'completed requests hide feedback immediately');cleanup();
draw(true);cleanup=effect();cleanup();assert.equal(scheduled.size,0,'route unmount cancels pending feedback');
const calls=[],lazyLoaders=[];
const modules=new Proxy({}, {get:(_,key)=>({default:String(key),Channels:'Channels',DownloadList:'DownloadList'})});
const Page=load('dashboard/DashboardPage.tsx',new Proxy({'react':{lazy:fn=>{lazyLoaders.push(fn);return 'LazyPage';},Suspense:'Suspense'},'../components/LoadingStatus':{default:Status},'./HomeShortcuts':{default:'Home'}},{get:(target,name)=>name in target?target[name]:(calls.push(name),modules[name])}));
async function main(){
for(const view of ['Favourites','Watchlist','Discover','Local Library','Subscriptions','YouTube','Downloads','Activity','TV Guide','M3U Subscriptions']){
 calls.length=0;Page.preloadPage(view);await new Promise(resolve=>setImmediate(resolve));assert.equal(calls.length,1,view+' warms only one code module');assert.ok(!calls[0].includes('api'),'intent cannot fetch provider data');
}
calls.length=0;Page.preloadPage('Home');Page.preloadPage('Settings');assert.equal(calls.length,0,'unrelated routes do not warm every bundle');
const source=fs.readFileSync('src/dashboard/DashboardPage.tsx','utf8');assert.ok(!source.includes('Skeleton'),'lazy-page navigation cannot paint large pulsing slabs');
console.log('Navigation loading: fast/slow/complete/unmount feedback and intent-only code warming passed.');

}
main().catch(error=>{console.error(error);process.exitCode=1;});
