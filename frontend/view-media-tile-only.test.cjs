const assert=require('node:assert/strict'),fs=require('node:fs'),path=require('node:path'),vm=require('node:vm'),ts=require('typescript');
const React=require('react'),M=require('@mantine/core'),{renderToStaticMarkup}=require('react-dom/server');
const browse=require('./media-browse-test-helper.cjs')();
const jsx=(type,props,key)=>({type,props:props||{},key});
const nodes=value=>value==null?[]:Array.isArray(value)?value.flatMap(nodes):typeof value==='object'?[value,...nodes(value.props?.children)]:[];
const text=value=>typeof value==='string'?value:Array.isArray(value)?value.map(text).join(''):value&&typeof value==='object'?text(value.props?.children):'';
function compile(file,modules={},globals={}){
 const out={};vm.runInNewContext(ts.transpileModule(fs.readFileSync(path.join(__dirname,'src',file),'utf8'),{compilerOptions:{module:ts.ModuleKind.CommonJS,target:ts.ScriptTarget.ES2022,jsx:ts.JsxEmit.ReactJSX,esModuleInterop:true}}).outputText,{exports:out,Map,Set,Date,Intl,URLSearchParams,AbortController,DOMException,...globals,require:name=>{
  if(name in modules)return modules[name];
  if(name.endsWith('.module.css'))return{__esModule:true,default:new Proxy({},{get:(_,key)=>String(key)})};
  if(!name.startsWith('.'))return require(name);
  return new Proxy({__esModule:true,default:'FixtureComponent'},{get:(object,key)=>key in object?object[key]:'FixtureComponent'});
 }});return out;
}
const helpers=Object.fromEntries(['filtering','ordering','refresh','movieOptions','showOptions','pagePreloader','localParent'].map(name=>['./'+name,compile('viewMedia/'+name+'.ts')]));
const detailTypes=Object.fromEntries(['DiscoveryTitle','VideoSheet','DetailSheet','M3uDetails','MediaManagement'].map(name=>[name,()=>null]));
function moduleFor(react=React,runtime=require('react/jsx-runtime'),calls={hover:0,bindings:0,reads:[],channels:[]}){
 const hover={anchor:{current:null},open:false,show(){},hide(){},failed:[],failImage(){}};
 const actions=()=>React.createElement('span',{'data-fixture-actions':true});
 const app=compile('viewMedia/ViewMedia.tsx',{
  ...helpers,react,'react/jsx-runtime':runtime,'@mantine/core':M,
  '../components/browse':browse,'../mediaDuration':require('./media-duration-test-helper.cjs')(),'../mediaDate':require('./media-date-test-helper.cjs')(),
  '../HoverCard':{__esModule:true,default:()=>React.createElement('span',{'data-fixture-hover':true}),useCardHover(){calls.hover++;return hover;},titleHover(){calls.bindings++;return{};}},
  '../collections/TileActions':{__esModule:true,default:actions,mediaTileTarget:()=>({})},
  '../ChannelIdentity':{__esModule:true,default:({name})=>React.createElement('span',{},name)},
  '../api':{api(url,signal,body){calls.reads.push({url,signal,body});return Promise.resolve({target:null});}},
  '../appNavigation':{mediaSearchQuery:search=>new URLSearchParams(search).get('q')||''},
  '../channelNavigation':{openChannel:target=>calls.channels.push(target)},
  '../discovery/DiscoveryTitle':{__esModule:true,default:detailTypes.DiscoveryTitle},
  '../VideoSheet':{__esModule:true,default:detailTypes.VideoSheet},
  '../components/DetailSheet':{DetailSheet:detailTypes.DetailSheet},
  '../M3uLibrary':{M3uDetails:detailTypes.M3uDetails},
  '../MediaManagement':{__esModule:true,default:detailTypes.MediaManagement},
  '../components/ui':{Button:'FixtureButton',Dialog:'FixtureDialog',DialogContent:'FixtureDialogContent',PopupShell:'FixturePopup'},
 },{location:{search:''},localStorage:{getItem:()=>null}});
 return{app,calls};
}
function scene(items=[],props={}){
 const slots=[];let index=0;
 const react={...React,memo:fn=>fn,useState(initial){const slot=index++;if(!(slot in slots))slots[slot]=typeof initial==='function'?initial():initial;return[slots[slot],next=>{slots[slot]=typeof next==='function'?next(slots[slot]):next;}];},useRef(initial){const slot=index++;return slots[slot]??={current:initial};},useEffect(){},useMemo:fn=>fn(),useCallback:fn=>fn};
 const fixture=moduleFor(react,{jsx,jsxs:jsx,Fragment:'Fragment'});
 return{...fixture,draw(){index=0;return fixture.app.default({csrf:'fixture',initialSource:'discover',initialMedia:'movies',sourceLocked:true,initial:{source:'discover',media:'movies',items,nextOffset:null,label:'Movies'},...props});}};
}
const sample=(kind='movie',action={type:'discover',item:{id:'movie:42',kind:'movie',title:'Exact title'}})=>({id:'saved:'+kind,title:'Exact title',kind,image:'/fixture-poster.jpg',backdrop:'/fixture-backdrop.jpg',year:'2026',rating:8.5,date:'2026-10-30',description:'Hidden synopsis',channel:'Creator',channelId:'UC'+'a'.repeat(22),durationSeconds:84,runtimeMinutes:123,count:12,origin:'NZB',origins:['NZB','Local Library'],action});
const result=(fixture,tree)=>nodes(tree).find(node=>node.type===fixture.app.MediaResults);
const invokeTile=(Tile,props)=>typeof Tile==='function'?Tile(props):Tile.type(props);

const actual=moduleFor();
for(const kind of ['movie','show','video','youtube','channel','shows','movies'])for(const image of ['/fixture-poster.jpg','']){
 const item={...sample(kind),image,backdrop:image?'/fixture-backdrop.jpg':undefined};
 const opened=[],before=actual.calls.bindings;
 const tree=invokeTile(actual.app.MediaTile,{item,layout:'tiles',searched:true,upcoming:true,label:'Source',onOpen:value=>opened.push(value)});
 assert.equal(tree.type,M.Paper,'artwork-only items use actual Mantine Paper');
 assert.equal(tree.props.component,'li','every title remains one semantic list item');
 const button=nodes(tree).filter(node=>node.type===M.UnstyledButton);
 assert.equal(button.length,1,'one button owns both artwork and hover title');
 assert.equal(button[0].props['aria-label'],'Open '+item.title,'the hidden title remains available to assistive technology');
 assert.equal(button[0].props.title,undefined,'native title tooltips do not leak an always-visible second title');
 const art=nodes(tree).filter(node=>node.type===browse.MediaArtwork);
 assert.equal(art.length,1,'one shared artwork slot handles poster or missing-art fallback');
 assert.equal(art[0].props.src,image||undefined);
 assert.equal(browse.MediaArtwork(art[0].props).props.ratio,2/3,'all sources retain a stable poster aspect ratio');
 assert.equal(art[0].props.fit||'cover','cover');
 const overlay=nodes(button[0]).find(node=>node.props?.['data-artwork-title']!==undefined);
 assert.ok(overlay);assert.equal(overlay.props['aria-hidden'],'true');assert.equal(text(overlay),item.title);
 assert.equal(button[0].props.className,'button');assert.equal(overlay.props.className,'title','the hidden title uses the scoped CSS class');
 for(const handler of ['onMouseEnter','onMouseLeave','onFocus','onBlur','onPointerEnter'])assert.equal(button[0].props[handler],undefined,'visual hover/focus never schedules a metadata read');
 assert.equal(actual.calls.bindings,before,'the card binds no detailed-preview hover/focus handlers');
 button[0].props.onClick();assert.deepEqual(opened,[item],'click passes the exact source action and identity unchanged');
 for(const scheme of ['light','dark']){
  const html=renderToStaticMarkup(React.createElement(M.MantineProvider,{forceColorScheme:scheme},React.createElement(actual.app.MediaTile,{item,layout:'tiles',searched:true,upcoming:true,label:'Source',onOpen(){}})));
  const body=html.match(/<li\b[\s\S]*?<\/li>/)?.[0];assert.ok(body);
  assert.match(body,/mantine-Paper-root/);assert.equal((body.match(/<button\b/g)||[]).length,1);
  assert.equal((body.match(/<img\b/g)||[]).length,image?1:0);
  if(image){assert.match(body,/loading="lazy"/);assert.match(body,/object-fit:cover/);}
  assert.doesNotMatch(body,/mantine-Badge-root|data-media-(?:duration|actions|meta|channel|status|kind-caption)|data-fixture-(?:actions|hover)|Hidden synopsis|Creator|NZB|Local Library|Upcoming/,'ratings, lengths, kinds, source, synopsis, badges, actions and hover cards stay absent, including channel artwork');
 }
}
for(const layout of ['grid','list']){
 const item=sample('video',{type:'youtube',id:'abcdefghijk'});
 const html=renderToStaticMarkup(React.createElement(M.MantineProvider,null,React.createElement(actual.app.MediaTile,{item,layout,searched:true,upcoming:true,label:'Source',onOpen(){}})));
 assert.match(html,/<li[^>]*mantine-Card-root/,'existing layouts retain the shared detailed Card');
 for(const attribute of ['data-media-duration','data-media-meta','data-media-channel','data-media-status','data-fixture-actions','data-fixture-hover'])assert.ok(html.includes(attribute),layout+' preserves '+attribute);
}
assert.deepEqual(actual.calls.reads,[],'rendering and interacting with artwork-only cards never fetches metadata or writes');

const workspace=scene([sample()]);let frame=workspace.draw();
const originalCards=Array.from(result(workspace,frame).props.items);
assert.equal(result(workspace,frame).props.layout,'tiles','ViewMedia defaults to the new mode');
assert.ok(!nodes(frame).some(node=>node.props?.['aria-label']==='Featured media'),'the default mode is artwork only, with no featured metadata panel');
for(const [label,layout]of [['Poster grid','grid'],['Media list','list'],['Tile only','tiles']]){
 const controls=nodes(frame).filter(node=>['Tile only','Poster grid','Media list'].includes(node.props?.['aria-label']));
 assert.equal(controls.length,3,'all three distinct layout controls remain available');
 assert.equal(controls.filter(node=>node.props['aria-pressed']===true).length,1);
 const control=controls.find(node=>node.props['aria-label']===label);assert.ok(control);
 control.props.onClick();frame=workspace.draw();
 assert.equal(result(workspace,frame).props.layout,layout);
 assert.equal(nodes(frame).find(node=>node.props?.['aria-label']===label).props['aria-pressed'],true);
 assert.deepEqual(Array.from(result(workspace,frame).props.items),originalCards,'layout selection preserves the existing card objects');
 assert.equal(nodes(frame).some(node=>node.props?.['aria-label']==='Featured media'),layout!=='tiles','featured media remains available in the existing detailed views');
}
assert.deepEqual(workspace.calls.reads,[],'presentation changes do not reload, enqueue or modify settings');
const allSources=scene([],{sourceLocked:false});assert.equal(result(allSources,allSources.draw()).props.layout,'tiles','the ordinary All sources Discover entry defaults to tiles');
const library=scene([sample()],{initialSource:'local'}),libraryTree=library.draw();
assert.equal(result(library,libraryTree).props.layout,'grid','Library retains its existing grid default');
assert.ok(!nodes(libraryTree).some(node=>node.props?.['aria-label']==='Tile only'),'the new layout control is scoped to Discover');

// The poster button uses the ordinary click router, preserving every provider's
// exact identifiers instead of constructing another details/download action.
for(const [kind,action,expected]of [
 ['movie',{type:'discover',item:{id:'movie:42',kind:'movie',title:'Film'}},'DiscoveryTitle'],
 ['show',{type:'discover',item:{id:'show:42',kind:'show',title:'Series'}},'DiscoveryTitle'],
 ['video',{type:'youtube',id:'abcdefghijk'},'VideoSheet'],
 ['movie',{type:'vod',source:'playlist:42',item:{kind:'movies',path:'exact-path.strm'}},'M3uDetails'],
 ['movies',{type:'local',id:'exact-local-id'},'DetailSheet'],
]){
 const item=sample(kind,action),fixture=scene([item]);let tree=fixture.draw();
 const tile=invokeTile(fixture.app.MediaTile,{item,layout:'tiles',searched:false,upcoming:false,label:'Source',onOpen:result(fixture,tree).props.onOpen});
 nodes(tile).find(node=>node.type===M.UnstyledButton).props.onClick();tree=fixture.draw();
 const detail=nodes(tree).find(node=>node.type===detailTypes[expected]);assert.ok(detail,kind+' opens its established details surface');
 if(expected==='DiscoveryTitle')assert.equal(detail.props.item,action.item);
 if(expected==='VideoSheet'){assert.equal(detail.props.video.id,action.id);assert.equal(detail.props.video.title,item.title);}
 if(expected==='M3uDetails'){assert.equal(detail.props.source,action.source);assert.equal(detail.props.item,action.item);}
 if(action.type==='local')assert.equal(detail.props.tabs.find(tab=>tab.id==='manage').content.props.localId,action.id,'Manage retains the exact local file identity');
 assert.deepEqual(fixture.calls.reads,[],'opening existing ordinary details performs no acquisition write');
}
const channel=sample('channel',{type:'youtube',id:'channel'}),channelScene=scene([channel]);
result(channelScene,channelScene.draw()).props.onOpen(channel);
assert.deepEqual(channelScene.calls.channels.map(value=>JSON.parse(JSON.stringify(value))),[{id:channel.channelId,name:channel.title,avatar:channel.image}]);
assert.deepEqual(channelScene.calls.reads,[]);

const presentation=moduleFor({...React,memo:fn=>fn},{jsx,jsxs:jsx,Fragment:'Fragment'});
for(const layout of ['tiles','grid','list']){
 const rows=[sample(),{...sample('show'),id:'second'},{...sample('video'),id:'third'}];
 const grid=presentation.app.MediaResults({items:rows,layout,searched:false,source:'discover',today:'2026-10-30',onOpen(){}});
 assert.equal(grid.type,M.SimpleGrid);assert.equal(grid.props.component,'ul');
 assert.equal(grid.props.spacing,layout==='tiles'?'xs':'md');
 assert.deepEqual(JSON.parse(JSON.stringify(grid.props.cols)),layout==='tiles'?{base:2,sm:3,md:4,lg:5,xl:6}:layout==='grid'?{base:2,md:3,xl:4}:1,'tight poster geometry does not alter existing view columns');
 assert.deepEqual(nodes(grid).filter(node=>node.type===presentation.app.MediaTile).map(node=>node.props.item),rows,'every existing result, including the third, survives the layout');
}
const movies=Array.from({length:3},(_,i)=>({...sample(),id:'movie:'+i})),more=[];
const grouped=presentation.app.MediaResults({items:[...movies,sample('show'),sample('video')],layout:'tiles',searched:true,source:'all',today:'2026-10-30',onOpen(){},limits:{Movies:1},onMore:group=>more.push(group)});
assert.deepEqual(nodes(grouped).filter(node=>node.props?.component==='section').map(node=>node.props['aria-label']),['Movies','TV Shows','YouTube']);
assert.equal(nodes(grouped).filter(node=>node.type===presentation.app.MediaTile).length,3,'the existing per-category search page limit remains effective');
const automatic=nodes(grouped).find(node=>node.type===presentation.app.AutoMoreResults);assert.ok(automatic);automatic.props.onMore();assert.deepEqual(more,['Movies']);
assert.ok(!nodes(grouped).some(node=>node.props?.['aria-label']==='Load more Movies'),'search pagination no longer mounts a manual button');

// Inspect the scoped cascade rather than a string snapshot: only intentional
// hover or keyboard focus may expose the otherwise hidden title. Root's real
// browser gate additionally checks computed visibility and 320px overflow.
const parseCss=require('node:module').createRequire(require.resolve('vite/package.json'))('postcss').parse;
const css=parseCss(fs.readFileSync(path.join(__dirname,'src/viewMedia/ArtworkTiles.module.css'),'utf8'));
const rules=[];css.walkRules(rule=>rules.push(rule));
const declarations=rule=>Object.fromEntries(rule.nodes.filter(node=>node.type==='decl').map(node=>[node.prop,node.value]));
const base=rules.find(rule=>rule.selector==='.button .title'&&rule.parent.type==='root');assert.ok(base,'two scoped classes outrank stock Mantine Text padding/position/color independent of stylesheet order');
const titleStyle=declarations(base);
assert.equal(titleStyle.visibility,'hidden');assert.equal(titleStyle['pointer-events'],'none');
assert.equal(titleStyle.background,'var(--mantine-color-dark-9)');assert.equal(titleStyle.color,'var(--mantine-color-white)');
assert.equal(titleStyle['overflow-wrap'],'anywhere');assert.equal(titleStyle['max-height'],'100%');
const visible=rules.filter(rule=>declarations(rule).visibility==='visible');
assert.equal(visible.length,2);assert.ok(visible.every(rule=>rule.selector==='.button:hover .title'||rule.selector==='.button:focus-visible .title'));
assert.equal(visible.find(rule=>rule.selector.includes(':hover')).parent.params,'(hover: hover)','touch devices do not acquire a sticky hover title');
const focus=declarations(rules.find(rule=>rule.selector==='.button:focus-visible'&&rule.parent.type==='root'));
assert.equal(focus.outline,'2px solid var(--mantine-color-text)');assert.equal(focus['outline-offset'],'3px');
const frosted=rules.find(rule=>rule.selector==='.button .title'&&rule.parent.type==='atrule'&&rule.parent.name==='supports');assert.ok(frosted);
assert.equal(declarations(frosted).background,'color-mix(in srgb, var(--mantine-color-dark-9) 94%, transparent)');
const reduced=rules.find(rule=>rule.selector==='.button .title'&&rule.parent.params==='(prefers-reduced-transparency: reduce), (prefers-contrast: more)');assert.ok(reduced);
assert.equal(declarations(reduced).background,'var(--mantine-color-dark-9)','reduced transparency and higher contrast restore a solid readable title');
assert.ok(css.nodes.indexOf(reduced.parent)>css.nodes.indexOf(frosted.parent),'equal-specificity accessibility fallback follows the optional translucent enhancement');
const forced=rules.filter(rule=>rule.parent.type==='atrule'&&rule.parent.params==='(forced-colors: active)');
assert.ok(forced.some(rule=>declarations(rule).background==='Canvas'&&declarations(rule).color==='CanvasText'));
assert.ok(forced.some(rule=>declarations(rule)['outline-color']==='Highlight'));
css.walkDecls(declaration=>assert.ok(!['opacity','animation','transition','transform','filter','backdrop-filter','will-change'].includes(declaration.prop),'titles do not fade, animate, blur or transform content'));
const luminance=color=>color.map(value=>value/255).map(value=>value<=.04045?value/12.92:((value+.055)/1.055)**2.4).reduce((sum,value,index)=>sum+value*[.2126,.7152,.0722][index],0);
const dark=M.DEFAULT_THEME.colors.dark[9].match(/[a-f\d]{2}/gi).map(value=>parseInt(value,16));
for(const artwork of [[0,0,0],[255,255,255]]){
 const background=dark.map((value,index)=>.94*value+.06*artwork[index]);
 assert.ok(1.05/(luminance(background)+.05)>=4.5,'white title text stays AA-readable over the brightest/darkest poster in either theme');
}
console.log('Discover Tile only: default and scoped three-mode controls, one accessible real Mantine artwork button, no metadata/actions/hover reads, exact provider detail routing, unchanged grid/list/search paging, and hover/focus/forced-colour readable titles passed.');
