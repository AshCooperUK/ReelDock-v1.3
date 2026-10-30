const assert=require('node:assert/strict'),fs=require('node:fs'),vm=require('node:vm'),ts=require('typescript');
const React=require('react'),{renderToStaticMarkup:renderMarkup}=require('react-dom/server');
const {MantineProvider}=require('@mantine/core');
const renderToStaticMarkup=tree=>renderMarkup(React.createElement(MantineProvider,{forceColorScheme:'dark'},tree));
function compiled(file,resolve=()=>({})){
 const exports={};vm.runInNewContext(ts.transpileModule(fs.readFileSync(__dirname+'/src/viewMedia/'+file,'utf8'),{compilerOptions:{module:ts.ModuleKind.CommonJS,jsx:ts.JsxEmit.ReactJSX,esModuleInterop:true}}).outputText,{exports,require:name=>name.endsWith('/mediaDate')?require('./media-date-test-helper.cjs')():name.endsWith('/mediaDuration')?require('./media-duration-test-helper.cjs')():name==='../components/browse'?require('./media-browse-test-helper.cjs')():name==='@mantine/core'?require(name):name.endsWith('/HoverCard')?{__esModule:true,default:()=>null,useCardHover:()=>({anchor:{current:null},open:false,show:()=>{},hide:()=>{},failed:[],failImage:()=>{}}),titleHover:()=>({})}:name.endsWith('/TileActions')?Object.assign(()=>null,{mediaTileTarget:()=>({})}):name==='react'?React:name==='react/jsx-runtime'?require(name):name==='lucide-react'?new Proxy({},{get:()=>()=>null}):resolve(name)});return exports;
}
const ordering=compiled('ordering.ts'),actions=[];
const {MediaResults}=compiled('ViewMedia.tsx',name=>name==='./ordering'?ordering:name==='../components/ui'?{Button:({variant,...props})=>{actions.push(props);return React.createElement('button',props);}}:{});
const items=['movie','show','video'].flatMap(kind=>Array.from({length:60},(_,n)=>({id:kind+n,title:kind+n,kind,year:'2026',action:{type:'local',id:kind+n}})));
for(const source of ['discover','vod','local','subscriptions'])for(const layout of ['grid','list']){
 const limits={};
 const props={items,layout,searched:true,source,today:'2026-10-16',onOpen:()=>{},limits,onMore:group=>limits[group]=(limits[group]||ordering.searchPageSize)+ordering.searchPageSize};
 const render=()=>{actions.length=0;return renderToStaticMarkup(React.createElement(MediaResults,props));};
 let html=render();
 assert.equal((html.match(/<li\b/g)||[]).length,72,'show the first 24 of each real category');
 assert.equal(actions.map(action=>action['aria-label']).join('|'),'Load more Movies|Load more TV Shows|Load more YouTube');
 actions.length=0;renderToStaticMarkup(React.createElement(MediaResults,{...props,loading:true}));
 assert.ok(actions.every(action=>action.disabled),'cached results cannot expand while a replacement search is loading');
 render();
 const first=ordering.searchPage(items.filter(item=>item.kind==='movie')).items.map(item=>item.id);
 actions.find(action=>action['aria-label']==='Load more Movies').onClick();html=render();
 assert.equal((html.match(/<li\b/g)||[]).length,96,'only Movies gains another 24 results');
 assert.equal(limits['TV Shows'],undefined);assert.equal(limits.YouTube,undefined);
 const next=ordering.searchPage(items.filter(item=>item.kind==='movie'),limits.Movies);
 assert.equal(next.items.slice(0,24).map(item=>item.id).join('|'),first.join('|'),'revealing more preserves the existing sort prefix');
 actions.find(action=>action['aria-label']==='Load more Movies').onClick();html=render();
 assert.equal((html.match(/<li\b/g)||[]).length,108);
 assert.equal(actions.map(action=>action['aria-label']).join('|'),'Load more TV Shows|Load more YouTube','exhausted categories have no Load more action');
 actions.find(action=>action['aria-label']==='Load more YouTube').onClick();render();
 assert.equal(limits.YouTube,48);assert.equal(limits['TV Shows'],undefined);
 assert.equal([...html.matchAll(/<h3[^>]*>(.*?)<\/h3>/g)].map(match=>match[1]).join('|'),'Movies|TV Shows|YouTube');
 actions.length=0;
 renderToStaticMarkup(React.createElement(MediaResults,{...props,searched:false}));
 assert.equal(actions.length,0,'normal source browsing has no search Load more actions');
 actions.length=0;
 const complete=renderToStaticMarkup(React.createElement(MediaResults,{...props,onMore:undefined}));
 assert.equal((complete.match(/<li\b/g)||[]).length,180,'a saved complete result set renders every movie, TV and YouTube card immediately');
 assert.equal(actions.length,0,'complete saved sets have no Load more controls');
}
console.log('View Media: independent category Load more buttons, fixed ordering, exhaustion and both layouts passed.');
