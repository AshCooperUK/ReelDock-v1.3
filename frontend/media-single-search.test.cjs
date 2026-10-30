const assert=require('node:assert/strict'),fs=require('node:fs'),vm=require('node:vm'),ts=require('typescript');
const jsx=(type,props)=>({type,props:props||{}});
const sample={movies:[{id:'m1',kind:'movie',title:'Matching movie'}],shows:[],episodes:[],channels:[],videos:[]};
const nodes=node=>node==null?[]:Array.isArray(node)?node.flatMap(nodes):typeof node==='object'?[node,...nodes(node.props?.children)]:[];
for(const view of ['Favourites','Watchlist']){
 let hook=0;const scopes=[];
 const source=fs.readFileSync(__dirname+'/src/collections/'+view+'.tsx','utf8'),out={};
 vm.runInNewContext(ts.transpileModule(source,{compilerOptions:{module:ts.ModuleKind.CommonJS,jsx:ts.JsxEmit.ReactJSX}}).outputText,{exports:out,AbortController,require:name=>{
  if(name==='react/jsx-runtime')return {jsx,jsxs:jsx,Fragment:'Fragment'};
  if(name==='react')return {useEffect(){},useRef:value=>({current:value}),useState:value=>[hook++===0?sample:typeof value==='function'?value():value,()=>{}]};
  if(name==='../pageSearch')return {usePageSearch:scope=>{scopes.push(scope);return 'movie channel';}};
  if(name==='./Favourites')return {collectionSorts:[{value:'az',label:'A–Z'}]};
  return new Proxy({},{get:(_,key)=>key==='__esModule'?false:key==='default'?'CollectionShelf':key});
 }});
 const tree=nodes(out.default({csrf:'fixture'}));
 assert.deepEqual(scopes,[view],'each page consumes its own shell search context');
 const shelves=tree.filter(node=>node.type==='CollectionShelf');assert.ok(shelves.length>=2);
 assert.ok(shelves.every(node=>node.props.query==='movie channel'),'the shell query reaches every media family without changing identity/action props');
 const toolbar=tree.find(node=>node.type==='FilterBar');assert.ok(toolbar);
 assert.equal(toolbar.props.onSearch,undefined,'the page toolbar has no duplicate search owner');
 assert.equal(toolbar.props.search,undefined);assert.ok(toolbar.props.onSort,'sorting remains available');
 assert.ok(!tree.some(node=>node.type==='TextInput'),'closed mainpage workflows contain no second search input');
 assert.doesNotMatch(source,/Movies, TV shows and YouTube you love|Upcoming and followed media\. Kept until you remove it\./,'the shell header owns introductory page copy');
}
const discover=fs.readFileSync(__dirname+'/src/viewMedia/ViewMedia.tsx','utf8');
assert.doesNotMatch(discover,/<TextInput|searchDraft|submitMediaSearch/,'Discover results use the one shell search');
assert.match(discover,/mediaSearchQuery\(location.search\)/,'Discover still reads deep-linked all-media queries');
assert.match(discover,/addEventListener\('reeldock-media-search'/,'new header searches still refresh real Discover results');
console.log('Single media search: shell query feeds all collection shelves; Discover retains deep links; sort/filter controls stay separate.');
