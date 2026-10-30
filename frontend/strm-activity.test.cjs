const assert=require('node:assert/strict'),fs=require('node:fs'),vm=require('node:vm'),ts=require('typescript');
const React=require('react'),Mantine=require('@mantine/core'),{renderToStaticMarkup}=require('react-dom/server'),h=React.createElement;
const text=value=>typeof value==='string'||typeof value==='number'?String(value):Array.isArray(value)?value.map(text).join(' '):value&&typeof value==='object'?text(value.props?.children):'';
const nodes=value=>Array.isArray(value)?value.flatMap(nodes):value&&typeof value==='object'?[value,...nodes(value.props?.children)]:[];
function compile(file,imports={}){const out={};vm.runInNewContext(ts.transpileModule(fs.readFileSync(__dirname+'/src/'+file,'utf8'),{compilerOptions:{module:ts.ModuleKind.CommonJS,jsx:ts.JsxEmit.ReactJSX}}).outputText,{exports:out,require:name=>imports[name]||(name.endsWith('.module.css')?{default:new Proxy({},{get:(_,key)=>String(key)})}:require(name))});return out;}
const status=compile('components/ActivityStatus.tsx'),warm=compile('warmSnapshots.ts');
const captured=[],calls=[],refreshed=[];
const fixtures=['queued','running','cancelling','failed','cancelled','complete'].map((state,i)=>({kind:'m3u',item:{id:'strm-'+i,source:'strm:source-'+i,path:'media.strm',sourceType:'strm',sourceName:'Fixture.Movie.2026.strm',title:'Imported '+state,kind:'movies',subscription:'STRM',artwork:'',description:'',state,message:'Full '+state+' outcome',progress:43,created:1_800_000_000,updated:1_800_000_100,destination:'/fixture/Movies/Imported.mp4',url:'https://example.invalid/?token=private-do-not-render'}}));
fixtures.push({kind:'m3u',item:{id:'catalogue',title:'Catalogue film',source:'provider-id',path:'Film.strm',subscription:'Provider',kind:'movies',state:'complete',message:'Original catalogue outcome'}});
const generic=new Proxy({},{get:(_,key)=>key==='__esModule'?false:({children})=>h('div',null,children)});
const modules={
 './activityClear':require('./activity-clear-test-helper.cjs')(),'./warmSnapshots':warm,'./components/ActivityStatus':status,'@mantine/hooks':{useMediaQuery:()=>false},
 './components/SelectField':require('./select-field-test-helper.cjs')(),
 './DownloadHistory':{useDownloadHistory:()=>({items:fixtures,active:[],more:false,busy:false,error:'',total:fixtures.length,end:{current:null},refresh(){refreshed.push('history');}})},
 './TorrentDownloads':{useTorrentActivity:()=>({jobs:[],card(){},stateOf(){}})},'./NzbDownloads':{useNzbActivity:()=>({jobs:[],card(){},stateOf(){}})},
 './components/ListRow':{__esModule:true,default:props=>{captured.push(props);return h('article',null,props.title);},ActivityQueueRows:({children})=>children},
 './components/ui':{Button:({variant,...props})=>h(Mantine.Button,props),Dialog:()=>null,DialogContent:generic.X,DialogDescription:generic.X,DialogTitle:generic.X,Switch:generic.X},
 './downloadFeed':{downloadLabel:()=> 'Downloading',refreshDownloads(){refreshed.push('downloads');}},'./mediaDate':{mediaDate:value=>value},
 './api':{api:async(url,signal,body,csrf)=>{calls.push({url,body,csrf});return{};}},
};
const out={};vm.runInNewContext(ts.transpileModule(fs.readFileSync(__dirname+'/src/LibraryPages.tsx','utf8'),{compilerOptions:{module:ts.ModuleKind.CommonJS,jsx:ts.JsxEmit.ReactJSX}}).outputText,{exports:out,require:name=>modules[name]||(['react','react/jsx-runtime','@mantine/core','lucide-react'].includes(name)?require(name):generic)});
renderToStaticMarkup(h(Mantine.MantineProvider,{forceColorScheme:'dark'},h(out.DownloadList,{csrf:'fixture-csrf'})));
(async()=>{
 assert.equal(captured.length,fixtures.length);
 for(const fixture of fixtures.filter(row=>row.item.sourceType==='strm')){
  const item=fixture.item,row=captured.find(row=>text(row.title)===item.title);assert.ok(row);assert.equal(typeof row.title.type,'string','manual imports have plain titles, without a pretend provider route');assert.equal(row.title.props.onClick,undefined);assert.match(text(row.meta),/Movie\s+STRM/);assert.ok(!text(row.meta).includes(item.message),'full notes remain collapsed');for(const fact of [item.sourceName,'Type: Movie',item.message,'Added:','Path: /fixture/Movies/Imported.mp4'])assert.ok(text(row.details).replace(/\s+/g,' ').includes(fact),fact+' is retained in the one Details expander');
  const actions=nodes(row.actions);assert.ok(!actions.some(node=>/^(Open|Info)/.test(node.props?.['aria-label']||text(node))),'manual imports expose no duplicate or broken Info route');
  if(['queued','running','cancelling'].includes(item.state)){const cancel=actions.find(node=>text(node)==='Cancel');assert.ok(cancel);assert.equal(cancel.props.disabled,item.state==='cancelling');if(item.state!=='cancelling'){await cancel.props.onClick();assert.deepEqual(JSON.parse(JSON.stringify(calls.at(-1))),{url:'/api/m3u-server-downloads',body:{id:item.id,action:'cancel'},csrf:'fixture-csrf'});}}
  if(['failed','cancelled'].includes(item.state)){const retry=actions.find(node=>text(node)==='Retry');assert.ok(retry);await retry.props.onClick();assert.deepEqual(JSON.parse(JSON.stringify(calls.at(-1))),{url:'/api/m3u-server-downloads',body:{action:'download',source:item.source,path:item.path},csrf:'fixture-csrf'},'Retry reuses the authoritative persisted source identity without reuploading a link');}
  if(item.state==='running'){assert.equal(row.progress.props.value,43);assert.equal(row.progress.props['aria-label'],'Download progress for '+item.title);}
  for(const phone of [false,true]){const real=compile('components/ListRow.tsx',{'@mantine/hooks':{useMediaQuery:()=>phone}}),html=renderToStaticMarkup(h(Mantine.MantineProvider,{forceColorScheme:phone?'light':'dark'},h(real.ActivityQueueRows,null,h(real.default,row))));assert.equal((html.match(/aria-label="Show details"/g)||[]).length,1);assert.ok(!html.includes('Open '+item.title));assert.ok(!html.includes('Full '+item.state+' outcome'),'the original outcome only appears after Details');assert.ok(!html.includes('private-do-not-render'),'even unexpected private URL fields cannot leak through Activity rendering');}
 }
 const catalogue=captured.find(row=>text(row.title)==='Catalogue film');assert.equal(typeof catalogue.title.props.onClick,'function','registered catalogue titles preserve their original detail navigation');assert.ok(nodes(catalogue.actions).some(node=>node.props?.['aria-label']==='Open Catalogue film'),'existing catalogue actions remain supplied to the shared deduplication control');
 await new Promise(resolve=>setImmediate(resolve));assert.equal(calls.length,4);assert.equal(refreshed.filter(value=>value==='history').length,4);assert.equal(refreshed.filter(value=>value==='downloads').length,4);
 console.log('STRM Activity: plain source-labelled titles, one Details control at all widths, safe filename/type/outcome/dates/path, exact cancel/retry/progress, no provider modal/URL leak and unchanged catalogue navigation passed.');
})().catch(error=>{console.error(error);process.exitCode=1;});
