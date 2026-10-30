const assert=require('node:assert/strict'),fs=require('node:fs'),vm=require('node:vm'),ts=require('typescript');
const React=require('react'),runtime=require('react/jsx-runtime'),Mantine=require('@mantine/core'),{renderToStaticMarkup}=require('react-dom/server');
const h=React.createElement;
const readable=value=>typeof value==='string'||typeof value==='number'?String(value):Array.isArray(value)?Array.from(value).map(readable).join(' '):value&&typeof value==='object'?readable(value.props?.children):'';
function compile(file,imports={}){const out={};vm.runInNewContext(ts.transpileModule(fs.readFileSync(__dirname+'/src/'+file,'utf8'),{compilerOptions:{module:ts.ModuleKind.CommonJS,jsx:ts.JsxEmit.ReactJSX}}).outputText,{exports:out,require:name=>imports[name]||(name.endsWith('.module.css')?{default:{media:'fixture-media',title:'fixture-title',progress:'fixture-progress',details:'fixture-details'}}:require(name))});return out;}
const status=compile('components/ActivityStatus.tsx'),colour=status.activityStatusColour;
for(const [tone,states] of [['green',['complete','completed','skipped','removed']],['red',['failed','error','cancelled','expired','stopped']],['yellow',['queued','paused','waiting','choose','pending']],['violet',['verifying','encoding','merging','sponsors','metadata','processing']],['blue',['downloading','running','searching','starting','seeding','cancelling','preparing']]])for(const state of states)assert.equal(colour(state),tone,state);
assert.equal(colour(undefined),'yellow');assert.equal(colour('unknown'),'gray');
for(const phase of ['Encoding with FFmpeg','Merging audio and video','Removing sponsor sections','Saving metadata','Processing','Awaiting placement']){
 assert.equal(colour('downloading',phase),'violet',phase);
 assert.equal(colour('complete',phase),'green','completed result outranks a stale processing phase');
 assert.equal(colour('failed',phase),'red','failed result outranks a stale processing phase');
 assert.equal(colour('queued',phase),'yellow','queued work does not masquerade as processing');
 assert.equal(colour('cancelling',phase),'blue','cancellation remains active even with a stale processing phase');
}
assert.equal(colour('downloading','Paused'),'yellow');assert.equal(colour('downloading','Choose type'),'yellow');assert.equal(colour('downloading','Preparing'),'blue');
const Button=({variant,...props})=>h(Mantine.Button,{...props,variant:variant==='secondary'?'default':undefined});
function rowModule(phone){return compile('components/ListRow.tsx',{'./ActivityStatus':status,'./ui':{Button,cn:(...parts)=>parts.filter(Boolean).join(' ')},'@mantine/hooks':{useMediaQuery:()=>phone}});}
const cases=[['removed',undefined,'Removed','green'],['queued',undefined,'Queued','yellow'],['failed',undefined,'Failed','red'],['downloading',undefined,'Downloading','blue'],['complete','Encoding with FFmpeg','Encoding with FFmpeg','green'],['downloading','Encoding with FFmpeg','Encoding with FFmpeg','violet'],['downloading','Choose type','Choose type','yellow']];
for(const phone of [false,true])for(const [state,phaseLabel,label,tone] of cases){
 const row=rowModule(phone),value=colour(state,phaseLabel);
 const tree=h(row.CompactActivityRows,null,h(row.default,{title:'Fixture title',media:h('img',{src:'/fixture.png',alt:''}),badge:h(status.ActivityStatus,{state,phaseLabel}),statusColour:value,meta:'Fixture source'}));
 const html=renderToStaticMarkup(h(Mantine.MantineProvider,{forceColorScheme:'dark'},tree));
 assert.ok(html.includes(label));
 assert.ok(html.indexOf('data-activity-status')<html.indexOf('data-status-colour'),'status appears at the tile top-left before icon/content at every width');
 const frame=html.match(/<div\b[^>]*data-status-colour="[^"]+"[^>]*>/)?.[0];assert.ok(frame);
 const badge=html.match(/<div\b[^>]*data-activity-status-badge[^>]*>/)?.[0];assert.ok(badge);
 const token='var(--mantine-color-'+tone+'-light-color)';
 assert.ok(frame.includes('border:2px solid '+token));assert.ok(badge.includes('color:'+token),'badge and icon frame use the exact same resolved colour');
 assert.ok(frame.includes('padding:6px'),'media frame keeps the requested internal padding');
 assert.ok(html.includes('<img src="/fixture.png"'),'padding preserves the original media content');
 if(phone){assert.match(frame,/width:calc\(2\.75rem/);assert.match(frame,/height:calc\(2\.75rem/);}
}
// Exercise the real engine card factories: colours must match their displayed
// badge, and their existing completion precedence and action callbacks remain.
function engineModule(file){
 const hooks={...React,useState:value=>[typeof value==='function'?value():value,()=>{}],useEffect:()=>{},useCallback:fn=>fn,useRef:value=>({current:value})};
 const ui=new Proxy({Button:'Button'},{get:(target,key)=>target[key]||String(key)});
 const out={};vm.runInNewContext(ts.transpileModule(fs.readFileSync(__dirname+'/src/'+file,'utf8'),{compilerOptions:{module:ts.ModuleKind.CommonJS,jsx:ts.JsxEmit.ReactJSX}}).outputText,{exports:out,require:name=>name==='react'?hooks:name==='react/jsx-runtime'?runtime:name==='./components/ActivityStatus'?status:name==='./components/ui'?ui:name==='./components/ListRow'?{__esModule:true,default:'ListRow'}:new Proxy({__esModule:true,default:'Placeholder'},{get:(target,key)=>target[key]||(()=>null)})});return out;
}
const base={id:'fixture',name:'Fixture engine item',progress:.5,state:'downloading',files:[],trackers:[],published:[],placements:[],error:'',stop_reason:'',down_rate:0,up_rate:0,peers:0,connections:0,servers:[],limits:{}};
for(const [file,hook] of [['TorrentDownloads.tsx','useTorrentActivity'],['NzbDownloads.tsx','useNzbActivity']]){
 const engine=engineModule(file)[hook]('fixture-csrf');
 for(const state of ['queued','paused','choose','downloading','failed']){
  const row=engine.card({...base,state});assert.equal(row.props.statusColour,colour(row.props.badge.props.state,row.props.badge.props.phaseLabel));
  assert.equal(row.props.statusColour,state==='failed'?'red':['queued','paused','choose'].includes(state)?'yellow':'blue',file+':'+state);
 }
 const done=engine.card({...base,progress:1,state:'stopped'});assert.equal(done.props.badge.props.state,'complete');assert.equal(done.props.statusColour,'green');
 const note='A unique full metadata outcome for '+file;
 const detailed=engine.card({...base,state:'failed',metadata_note:note});
 assert.ok(!readable(detailed.props.meta).includes(note),'verbose engine outcomes stay out of collapsed metadata');
 assert.ok(readable(detailed.props.details).includes(note),'the complete engine outcome remains in expanded Details');
 if(file==='TorrentDownloads.tsx'){
  const error=engine.card({...base,state:'error'});assert.equal(error.props.badge.props.state,'failed');assert.equal(error.props.statusColour,'red');
  const finishedError=engine.card({...base,state:'error',progress:1});assert.equal(finishedError.props.badge.props.state,'complete','existing finished-progress precedence is retained');
 }
}
const library=fs.readFileSync(__dirname+'/src/LibraryPages.tsx','utf8');
assert.equal((library.match(/statusColour=\{activityStatusColour\(/g)||[]).length,3,'sync/event, Discover, and YouTube/VOD history all opt into the shared colour helper');
assert.match(library,/export \{ ActivityStatus as DownloadState \}/,'existing public status export remains compatible');
// Render every history family through the real DownloadList factories while
// capturing ListRow props, so note placement cannot silently lose an outcome.
const captured=[],fixtures=[
 {kind:'m3u_sync',item:{id:'sync',title:'Fixture sync',subscription:'Provider',state:'complete',message:'Unique sync outcome',finished:1,stats:{changes:{movies:{added:3,removed:1,updated:2}}}}},
 {kind:'discover',item:{id:'discover',title:'Fixture Discover',state:'queued',mediaKind:'movie',message:'Unique Discover outcome',created:2}},
 {kind:'youtube',item:{id:'video',title:'Fixture YouTube',state:'failed',channel_name:'Creator',message:'Unique YouTube outcome',created:3}},
 {kind:'m3u',item:{id:'vod',title:'Fixture VOD',subscription:'Provider',source:'fixture',path:'movie.strm',state:'complete',message:'Unique VOD outcome',created:4}},
];
const generic=new Proxy({},{get:(_,key)=>key==='__esModule'?false:({children})=>h('div',null,children)});
const modules={
 './activityClear':require('./activity-clear-test-helper.cjs')(),'./warmSnapshots':compile('warmSnapshots.ts'),'./components/ActivityStatus':status,'@mantine/hooks':{useMediaQuery:()=>false},
 './components/SelectField':{SelectField:require('./select-field-test-helper.cjs')().SelectField},
 './DownloadHistory':{useDownloadHistory:()=>({items:fixtures,active:[],more:false,busy:false,error:'',total:fixtures.length,end:{current:null},refresh(){}})},
 './TorrentDownloads':{useTorrentActivity:()=>({jobs:[],card(){},stateOf(){}})},'./NzbDownloads':{useNzbActivity:()=>({jobs:[],card(){},stateOf(){}})},
 './components/ListRow':{__esModule:true,default:props=>{captured.push(props);return h('article',null,props.title);},CompactActivityRows:({children})=>children,ActivityQueueRows:({children})=>children},
 './components/ui':{Button:({variant,...props})=>h('button',props),Dialog:()=>null,DialogContent:generic.X,DialogDescription:generic.X,DialogTitle:generic.X,Switch:generic.X},
 './downloadFeed':{downloadLabel:()=> 'Downloading',refreshDownloads(){}},'./mediaDate':{mediaDate:value=>value},
};
const history={};vm.runInNewContext(ts.transpileModule(library,{compilerOptions:{module:ts.ModuleKind.CommonJS,jsx:ts.JsxEmit.ReactJSX}}).outputText,{exports:history,require:name=>modules[name]||(['react','react/jsx-runtime','@mantine/core','lucide-react'].includes(name)?require(name):generic)});
renderToStaticMarkup(h(Mantine.MantineProvider,{forceColorScheme:'dark'},h(history.DownloadList,{csrf:'fixture'})));
assert.equal(captured.length,4);
for(const fixture of fixtures){const row=captured.find(row=>readable(row.title)===fixture.item.title);assert.ok(row,fixture.kind);assert.ok(!readable(row.meta).includes(fixture.item.message),fixture.kind+' collapsed summary excludes the full note');assert.ok(readable(row.details).includes(fixture.item.message),fixture.kind+' Details retain the complete note');assert.equal(row.statusColour,colour(fixture.item.state));}
const sync=captured.find(row=>readable(row.title)==='Fixture sync');assert.match(readable(sync.details).replace(/\s+/g,' '),/3 added · 1 removed · 2 updated/,'sync counts remain in Details');
console.log('Activity status: terminal/queued/processing precedence, engine cards, five caller sites, top-left labels, exact frame/badge colour and padded 44px media retention passed.');

assert.equal(status.ActivityGlyph,undefined,'the removed queue-square renderer is no longer exported');assert.equal(status.activityGlyphKind,undefined,'the unused queue-square icon mapping is deleted');
for(const[phase,short]of [['Encoding with FFmpeg','Encoding'],['Merging audio and video','Merging'],['Saving metadata','Metadata'],['Removing sponsor sections','Processing'],['Awaiting placement','Processing']]){const compact=renderToStaticMarkup(h(Mantine.MantineProvider,{forceColorScheme:'dark'},h(status.ActivityStatus,{state:'downloading',phaseLabel:phase,concise:true})));assert.ok(compact.includes('title="'+phase+'"'));assert.ok(compact.includes('>'+short+'</span>'));const full=renderToStaticMarkup(h(Mantine.MantineProvider,{forceColorScheme:'dark'},h(status.ActivityStatus,{state:'downloading',phaseLabel:phase})));assert.ok(full.includes('>'+phase+'</span>'),'other surfaces keep the full phase label');}
