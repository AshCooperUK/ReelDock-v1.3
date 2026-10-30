const assert=require('node:assert/strict'),fs=require('node:fs'),vm=require('node:vm'),ts=require('typescript');
const React=require('react'),{renderToStaticMarkup:renderMarkup}=require('react-dom/server');
const {MantineProvider}=require('@mantine/core');
const jsx=(type,props,key)=>({type,props:props||{},key});
const nodes=value=>value==null?[]:Array.isArray(value)?value.flatMap(nodes):typeof value==='object'?[value,...nodes(value.props?.children)]:[];
function compiled(file,resolve=()=>({}),runtime=require('react/jsx-runtime')){
 const exports={};vm.runInNewContext(ts.transpileModule(fs.readFileSync(__dirname+'/src/viewMedia/'+file,'utf8'),{compilerOptions:{module:ts.ModuleKind.CommonJS,jsx:ts.JsxEmit.ReactJSX,esModuleInterop:true}}).outputText,{exports,require:name=>name.endsWith('/mediaDate')?require('./media-date-test-helper.cjs')():name.endsWith('/mediaDuration')?require('./media-duration-test-helper.cjs')():name==='../components/browse'?require('./media-browse-test-helper.cjs')():name==='@mantine/core'?require(name):name.endsWith('/HoverCard')?{__esModule:true,default:()=>null,useCardHover:()=>({anchor:{current:null},open:false,show(){},hide(){},failed:[],failImage(){}}),titleHover:()=>({})}:name.endsWith('/TileActions')?Object.assign(()=>null,{mediaTileTarget:()=>({})}):name==='react'?React:name==='react/jsx-runtime'?runtime:name==='lucide-react'?new Proxy({},{get:()=>()=>null}):resolve(name)});return exports;
}
const ordering=compiled('ordering.ts'),modules=name=>name==='./ordering'?ordering:{};
const actual=compiled('ViewMedia.tsx',modules),presentation=compiled('ViewMedia.tsx',modules,{jsx,jsxs:jsx,Fragment:'Fragment'});
const items=['movie','show','video'].flatMap(kind=>Array.from({length:60},(_,n)=>({id:kind+n,title:kind+n,kind,year:'2026',action:{type:'local',id:kind+n}})));
for(const source of ['discover','vod','local','subscriptions'])for(const layout of ['tiles','grid','list']){
 const limits={},props={items,layout,searched:true,source,today:'2026-10-16',onOpen(){},limits,onMore:group=>limits[group]=(limits[group]||ordering.searchPageSize)+ordering.searchPageSize};
 const render=()=>renderMarkup(React.createElement(MantineProvider,{forceColorScheme:'dark'},React.createElement(actual.MediaResults,props)));
 const edges=(extra={})=>nodes(presentation.MediaResults({...props,...extra})).filter(node=>node.type===presentation.AutoMoreResults);
 let html=render();assert.equal((html.match(/<li\b/g)||[]).length,72,'first 24 of every cached category are mounted');
 assert.doesNotMatch(html,/Load more/,'search has no manual pagination control');assert.match(html,/24 of 60 Movies results shown/);assert.match(html,/aria-live="polite"/);
 assert.equal(edges().length,3);assert.ok(edges({loading:true}).every(edge=>edge.props.enabled===false),'replacement loads pause automatic reveal');
 const first=ordering.searchPage(items.filter(item=>item.kind==='movie')).items.map(item=>item.id);
 edges()[0].props.onMore();html=render();assert.equal((html.match(/<li\b/g)||[]).length,96,'only intersecting Movies gains another 24');
 assert.equal(limits['TV Shows'],undefined);assert.equal(limits.YouTube,undefined);
 assert.equal(ordering.searchPage(items.filter(item=>item.kind==='movie'),limits.Movies).items.slice(0,24).map(item=>item.id).join('|'),first.join('|'),'automatic reveal preserves the sorted prefix');
 edges()[0].props.onMore();html=render();assert.equal((html.match(/<li\b/g)||[]).length,108);
 assert.equal(edges().length,2,'exhausted Movies has no live sentinel');assert.match(html,/60 of 60 Movies results shown/);
 edges().find(edge=>edge.props.batch.startsWith('YouTube:')).props.onMore();assert.equal(limits.YouTube,48);assert.equal(limits['TV Shows'],undefined);
 assert.equal([...html.matchAll(/<h3[^>]*>(.*?)<\/h3>/g)].map(match=>match[1]).join('|'),'Movies|TV Shows|YouTube');
 assert.equal(edges({searched:false}).length,0,'ordinary browsing keeps its separate network paging');
 assert.equal(edges({onMore:undefined}).length,0,'a caller without local paging retains its complete presentation');
 assert.equal(items.length,180,'revealing cards never mutates or consumes the fixed result frame');
}
console.log('Search result batches: automatic category edges, no buttons, all sources/layouts, stable ordering, busy pause, accessibility and exhaustion passed.');
