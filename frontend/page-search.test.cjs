const assert=require('node:assert/strict'),fs=require('node:fs'),vm=require('node:vm'),ts=require('typescript');
const nav={},search={},listeners=new Map(),states=[],effects=[];
function code(name){return ts.transpileModule(fs.readFileSync(__dirname+'/src/'+name,'utf8'),{compilerOptions:{target:ts.ScriptTarget.ES2022,module:ts.ModuleKind.CommonJS}}).outputText;}
vm.runInNewContext(code('appNavigation.ts'),{exports:nav,URL,URLSearchParams});
const location={href:'http://127.0.0.1:8822/app/?view=Subscriptions',search:'?view=Subscriptions'};
const changes=[];
const history={state:{keep:true},replaceState(state,title,url){changes.push({state,title,url});location.href='http://127.0.0.1:8822'+url;location.search=new URL(location.href).search;}};
const window={addEventListener:(key,fn)=>listeners.set(key,fn),removeEventListener:(key,fn)=>{assert.equal(listeners.get(key),fn);listeners.delete(key);},dispatchEvent:event=>listeners.get(event.type)?.()};
vm.runInNewContext(code('pageSearch.ts'),{exports:search,location,history,window,URL,URLSearchParams,Event,require:name=>name==='./appNavigation'?nav:{useState:init=>[typeof init==='function'?init():init,value=>states.push(value)],useEffect:fn=>effects.push(fn)}});
assert.equal(search.pageSearchQuery('?view=YouTube&filter=workshop','Subscriptions'),'workshop','aliases retain scoped filtering');
assert.equal(search.pageSearchQuery('?view=Watchlist&filter=workshop','Subscriptions'),'','queries never leak into another page');
assert.equal(search.pageSearchQuery('?view=Discover&q=film','Discover'),'','global q stays separate from a local filter');
for(const view of ['Favourites','Watchlist','Subscriptions','TV Guide']){
 const url=search.pageSearchUrl('http://127.0.0.1:8822/app/?view=Discover&q=old&date=2026-10-24',view,'  space & symbols  ');
 const parsed=new URL('http://127.0.0.1:8822'+url);assert.equal(parsed.searchParams.get('view'),view);assert.equal(parsed.searchParams.get('filter'),'space & symbols');assert.equal(parsed.searchParams.has('q'),false);
}
assert.equal(new URL('http://x'+search.pageSearchUrl(location.href,'Subscriptions','')).searchParams.has('filter'),false,'empty query clears filtering');
assert.equal(search.usePageSearch('Subscriptions'),'');const cleanup=effects[0]();
assert.equal(listeners.size,2,'shared filters follow submits and browser history');
search.setPageSearch('Subscriptions','Nature');assert.equal(states.at(-1),'Nature');assert.deepEqual(changes.at(-1).state,{keep:true});
search.setPageSearch('Subscriptions','');assert.equal(states.at(-1),'','Reset view clears the page and shell through one event');
location.search='?view=Subscriptions&filter=Workshop';listeners.get('popstate')();assert.equal(states.at(-1),'Workshop','Back/Forward restores scoped query');
cleanup();assert.equal(listeners.size,0);
for(const target of ['Home','Settings','Discover','TV Guide'])assert.equal(new URL('http://x'+nav.viewUrl('http://x/app/?view=Subscriptions&filter=stale',target)).searchParams.has('filter'),false,'navigation clears the prior page filter');
assert.equal(new URL('http://x'+nav.mediaSearchUrl('http://x/app/?view=Subscriptions&filter=old','film')).searchParams.has('filter'),false,'all-media search clears the old page scope');
console.log('One shared search: scoped query identity, encoded URLs, history, reset, cleanup and global separation passed.');
