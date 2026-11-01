const assert=require('node:assert/strict'),fs=require('node:fs'),vm=require('node:vm'),ts=require('typescript'),{test}=require('node:test');
const React=require('react'),M=require('@mantine/core'),runtime=require('react/jsx-runtime'),{renderToStaticMarkup}=require('react-dom/server');
const h=React.createElement,none=()=>null;
const nodes=node=>node==null?[]:Array.isArray(node)?node.flatMap(nodes):typeof node==='object'?[node,...nodes(node.props?.children),...nodes(node.props?.content),...nodes(node.props?.downloads)]:[];
function load(file,imports,globals={}){const out={};vm.runInNewContext(ts.transpileModule(fs.readFileSync(__dirname+'/src/'+file,'utf8'),{compilerOptions:{module:ts.ModuleKind.CommonJS,jsx:ts.JsxEmit.ReactJSX}}).outputText,{exports:out,...globals,require:name=>imports[name]||(name.endsWith('.module.css')?{default:{}}:require(name))});return out;}
const guideData=load('guideData.ts',{'./mediaDuration':require('./media-duration-test-helper.cjs')()});
const programme=(id,date,start,status='Uploaded',extra={})=>({id,videoId:status==='Uploaded'?id.replace(/\W/g,'_').padEnd(11,'_').slice(0,11):undefined,title:id,date,start,status,note:'',description:'Fixture '+id,category:'Test',...extra});
const channel=(id,name,programmes,extra={})=>({id,name,initials:name[0],subscribers:'1',favourite:false,programmes,...extra});
const monday='2026-10-26',saturday='2026-10-31',sunday='2026-11-01';
const fixtures=[
 channel('alpha','Alpha',[programme('Monday midnight',monday,0),programme('Sunday early',sunday,9)],{subscriberCount:10,sortMetrics:{latest:10,upcoming:50,activity:2}}),
 channel('zulu','Zulu',[programme('Sunday late',sunday,15),programme('Sunday forecast',sunday,23,'Expected'),programme('Saturday scheduled',saturday,22,'Scheduled')],{subscriberCount:100,sortMetrics:{latest:20,upcoming:10,activity:9}}),
 channel('favourite','Favourite',[programme('Monday favourite',monday,23),programme('Sunday favourite',sunday,7)],{favourite:true,subscriberCount:40,sortMetrics:{latest:1,upcoming:30,activity:4}}),
 channel('title','Date-only title',[programme('Date-only',sunday,0,'Release date',{allDay:true})],{movie:{id:'movie:123',kind:'movie',title:'Date-only title',externalId:'123'}}),
];
function scene({phone=false,channels=fixtures,sort='latest',favourites=false,query=''}={}){
 const state=[],refs=[],slots=[];let index=0,refIndex=0,effectIndex=0,dirty=false,pending=[];
 const hooks={useState(initial){const i=index++;if(!(i in state))state[i]=typeof initial==='function'?initial():initial;return[state[i],value=>{const next=typeof value==='function'?value(state[i]):value;if(!Object.is(next,state[i])){state[i]=next;dirty=true;}}];},useRef(initial){const i=refIndex++;return refs[i]||=( {current:initial});},useEffect(fn,deps){const i=effectIndex++,old=slots[i];if(!old||deps.some((value,k)=>!Object.is(value,old[k]))){slots[i]=deps;pending.push(fn);}}};
 const actions=[],storage=new Map([['reeldock-guide-sort',sort]]);
 const imports={react:hooks,'react/jsx-runtime':runtime,'@mantine/core':M,'@mantine/hooks':{useMediaQuery:()=>phone},'./components/SelectField':{SelectField:({data,children,...props})=>h('select',props,children||data?.map(item=>h('option',{key:item.value,value:item.value},item.label)))},'./components/ui':{Button:({variant,...props})=>h(M.Button,{...props,variant:variant==='secondary'?'default':variant}),Switch:none},'./TextField':{TextInput:props=>h('input',props)},'./pageSearch':{usePageSearch:()=>query,setPageSearch(_view,value){query=value;}},'./mediaDate':{mediaDate:value=>value},'./mediaDuration':require('./media-duration-test-helper.cjs')(),'./LiveArtwork':{__esModule:true,default:none},'./ChannelAvatar':{__esModule:true,default:none},'./channelNavigation':{openChannel:value=>actions.push(['source',value])},'./guideData':guideData,'./VideoSheet':{__esModule:true,default:none},'./VideoDownloadControls':{__esModule:true,default:none},'./discovery/DiscoveryTitle':{__esModule:true,default:none},'./collections/TileActions':{__esModule:true,default:none},'./notifications':{NotificationMessage:none},'./components/states':{SkeletonRow:none},'./components/LoadingStatus':{__esModule:true,default:none}};
 const subject=load('Guide.tsx',imports,{localStorage:{getItem:key=>storage.get(key)||null,setItem:(key,value)=>storage.set(key,value)},IntersectionObserver:class{observe(){}disconnect(){}}});
 const live={csrf:'fixture-csrf',channels,date:monday,today:sunday,now:16,loading:false,error:'',truncated:false,saving:false,changeDay:value=>actions.push(['week',value]),retry(){},selectDate:value=>actions.push(['date',value]),favourite:(...value)=>actions.push(['favourite',...value])};
 return{live,actions,storage,setFavourites(value){favourites=value;},setQuery(value){query=value;},draw(){let tree;for(let pass=0;pass<4;pass++){index=refIndex=effectIndex=0;dirty=false;pending=[];tree=subject.default({live,favouritesOnTop:favourites,onFavouritesOnTop:value=>{favourites=value;}});pending.forEach(fn=>fn());if(!dirty)return tree;}throw Error('Fixture state did not settle');}};
}
const sections=tree=>nodes(tree).filter(node=>String(node.props?.['aria-label']||'').startsWith('Programmes for '));
const rows=tree=>nodes(tree).filter(node=>node.props?.['data-guide-programme']);
const ids=tree=>rows(tree).map(node=>node.key);
const control=(tree,label)=>nodes(tree).find(node=>node.props?.['aria-label']===label);
const choose=(scene,label,value)=>{control(scene.draw(),label).props.onChange({currentTarget:{value}});return scene.draw();};
const markup=(tree,scheme='dark')=>renderToStaticMarkup(h(M.MantineProvider,{forceColorScheme:scheme},h('div',{style:{width:320}},tree)));

test('Latest upload renders newest days and actual upload times before forecasts/date-only entries',()=>{
 for(const phone of [false,true]){
  const s=scene({phone}),tree=s.draw();
  assert.deepEqual(sections(tree).map(node=>node.props['aria-label']),['Programmes for '+sunday,'Programmes for '+monday,'Programmes for '+saturday]);
  assert.deepEqual(ids(sections(tree)[0]),['Sunday late','Sunday early','Sunday favourite','Sunday forecast','Date-only']);
  for(const scheme of ['light','dark']){
   const html=markup(tree,scheme);
   assert.ok(html.indexOf('Programmes for '+sunday)<html.indexOf('Programmes for '+monday),'actual rendered sections put Sunday before Monday');
   assert.ok(html.indexOf('Open Sunday late')<html.indexOf('Open Sunday forecast'),'an unannounced23:00 forecast does not masquerade as the latest actual15:00 upload');
   assert.match(html,/Expected/);assert.match(html,/Date only/);assert.match(html,/Release date/);
  }
  const dateRow=rows(tree).find(node=>node.key==='Date-only');assert.match(markup(dateRow),/2026-11-01 · Date only/);assert.doesNotMatch(markup(dateRow),/00:00/,'native date-only entries gain no fabricated midnight time');
 }
});

test('Agenda sorts respect favourites within dates, channel choices, Upcoming and native title dates',()=>{
 const s=scene({favourites:true});let tree=s.draw();
 assert.deepEqual(ids(sections(tree)[0]),['Sunday favourite','Sunday late','Sunday early','Sunday forecast','Date-only']);
 assert.equal(sections(tree)[0].props['aria-label'],'Programmes for '+sunday,'Monday favourite cannot move the whole week backwards');
 s.setFavourites(false);tree=choose(s,'Sort channels','upcoming');
 assert.deepEqual(sections(tree).map(node=>node.props['aria-label']),['Programmes for '+monday,'Programmes for '+saturday,'Programmes for '+sunday]);
 assert.deepEqual(ids(sections(tree).at(-1)),['Date-only','Sunday favourite','Sunday early','Sunday late','Sunday forecast']);
 tree=choose(s,'Sort channels','name-desc');assert.deepEqual(ids(sections(tree).at(-1)),['Sunday late','Sunday forecast','Sunday favourite','Date-only','Sunday early']);
 tree=choose(s,'Sort channels','subscribers-desc');assert.deepEqual(ids(sections(tree).at(-1)),['Sunday late','Sunday forecast','Sunday favourite','Sunday early','Date-only']);
 tree=choose(s,'Sort channels','favourites');assert.deepEqual(ids(tree),['Monday favourite','Sunday favourite']);
 tree=choose(s,'Sort channels','latest');tree=choose(s,'Filter guide','Uploaded');assert.deepEqual(ids(sections(tree)[0]),['Sunday late','Sunday early','Sunday favourite']);
 s.setQuery('Alpha');assert.deepEqual(ids(s.draw()),['Sunday early','Monday midnight'],'shared search keeps matching exact source programmes and selected ordering');
});

test('Live uploads outrank favourite forecasts; a future week retains planned times and exact date-only events',()=>{
 const actual=channel('live','Live source',[programme('Actual live',sunday,10,'Live now')]);
 const forecast=channel('forecast','Forecast source',[programme('Favourite forecast',sunday,23,'Expected')],{favourite:true});
 const native=fixtures[3];
 const current=scene({channels:[forecast,native,actual],favourites:true});let tree=current.draw();
 assert.deepEqual(ids(tree),['Actual live','Favourite forecast','Date-only']);
 tree=choose(current,'Filter guide','Live now');assert.deepEqual(ids(tree),['Actual live']);
 const nextMonday='2026-11-02',nextTuesday='2026-11-03';
 const future=scene({channels:[channel('future','Future source',[programme('Outside previous week',sunday,21,'Expected'),programme('Future early',nextMonday,9,'Expected'),programme('Future late',nextMonday,23,'Scheduled'),programme('Future Tuesday',nextTuesday,12,'Expected'),programme('Outside next week','2026-11-09',20,'Expected')])]});
 future.live.date=nextMonday;tree=future.draw();
 assert.deepEqual(ids(tree),['Future Tuesday','Future late','Future early'],'forecast-only weeks use planned dates/times without converting estimates into uploads');
 assert.ok(nodes(tree).filter(node=>node.props?.['data-guide-upload']).every(node=>!JSON.stringify(node.props.children).includes('Published')));
});

test('Latest keeps actual-bearing days ahead of future-only days, including before the40-row slice',()=>{
 const wednesday='2026-10-28';
 const current=channel('current','Current source',[programme('Monday actual',monday,10),programme('Wednesday actual',wednesday,15),programme('Wednesday forecast',wednesday,23,'Expected')]);
 const future=channel('future','Future favourite',[programme('Sunday future only',sunday,23,'Expected')],{favourite:true});
 const native=channel('native','Native title',[programme('Wednesday native',wednesday,0,'Release date',{allDay:true}),programme('Saturday native',saturday,0,'Release date',{allDay:true})],{movie:fixtures[3].movie});
 const s=scene({channels:[future,native,current],favourites:true});s.live.today=wednesday;let tree=s.draw();
 assert.deepEqual(sections(tree).map(node=>node.props['aria-label']),['Programmes for '+wednesday,'Programmes for '+monday,'Programmes for '+sunday,'Programmes for '+saturday]);
 assert.deepEqual(ids(sections(tree)[0]),['Wednesday actual','Wednesday forecast','Wednesday native']);
 const html=markup(tree);assert.ok(html.indexOf('Programmes for '+wednesday)<html.indexOf('Programmes for '+sunday));
 tree=choose(s,'Filter guide','Upcoming');
 assert.deepEqual(sections(tree).map(node=>node.props['aria-label']),['Programmes for '+sunday,'Programmes for '+saturday,'Programmes for '+wednesday],'actual-day priority uses only entries in the selected filter');
 const many=scene({channels:[future,channel('actual','Actual source',Array.from({length:42},(_,i)=>programme('Wednesday '+String(i).padStart(2,'0'),wednesday,1+i/10)).concat(programme('Monday actual',monday,10)))]});
 tree=many.draw();assert.equal(rows(tree).length,40);assert.equal(ids(tree)[0],'Wednesday 41');assert.ok(!ids(tree).includes('Sunday future only'));
 nodes(tree).find(node=>node.props?.children==='Load more programmes').props.onClick();tree=many.draw();
 assert.equal(rows(tree).length,44);assert.equal(ids(tree).at(-1),'Sunday future only');assert.equal(new Set(ids(tree)).size,44);
});

test('Ordered agenda pagination and day selection retain exact IDs/actions; timeline retains channel sorting',()=>{
 const many=channel('many','Many',Array.from({length:65},(_,i)=>programme('Sunday '+String(i).padStart(2,'0'),sunday,1+i/10)).concat(programme('Older Monday',monday,0)),{sortMetrics:{latest:100}});
 const s=scene({phone:true,channels:[many]});let tree=s.draw();
 assert.equal(rows(tree).length,40);assert.equal(ids(tree)[0],'Sunday 64');assert.ok(!ids(tree).includes('Older Monday'));
 control(tree,'Open Sunday 64').props.onClick();tree=s.draw();
 const sheet=nodes(tree).find(node=>node.props?.video?.title==='Sunday 64');assert.equal(sheet.props.video.id,'Sunday_64__');
 nodes(tree).find(node=>node.props?.children==='Load more programmes').props.onClick();tree=s.draw();
 assert.equal(rows(tree).length,66);assert.equal(new Set(ids(tree)).size,66);assert.equal(ids(tree).at(-1),'Older Monday');
 tree=choose(s,'Sort channels','upcoming');assert.equal(rows(tree).length,40,'changing sort resets the bounded page');assert.equal(ids(tree)[0],'Older Monday');
 tree=choose(s,'Sort channels','latest');assert.equal(rows(tree).length,40);assert.equal(ids(tree)[0],'Sunday 64');
 tree=choose(s,'Guide day',monday);assert.deepEqual(ids(tree),['Older Monday']);
 const timeline=scene({channels:fixtures});tree=choose(timeline,'Guide view','timeline');
 const rowHeaders=nodes(tree).filter(node=>node.props?.role==='rowheader');
 assert.deepEqual(rowHeaders.map(node=>nodes(node).find(child=>String(child.props?.['aria-label']||'').startsWith('Channel information:')||String(child.props?.['aria-label']||'').startsWith('Movie information:')).props['aria-label']),['Channel information: Zulu','Channel information: Alpha','Channel information: Favourite','Movie information: Date-only title']);
 assert.ok(nodes(tree).some(node=>node.props?.['aria-label']==='Seven-day channel guide'));
});
