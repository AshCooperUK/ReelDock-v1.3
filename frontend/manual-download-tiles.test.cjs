const assert=require('node:assert/strict'),fs=require('node:fs'),vm=require('node:vm'),ts=require('typescript');
const jsx=(type,props)=>({type,props:props||{}});
function nodes(tree){return !tree||typeof tree!=='object'?[]:Array.isArray(tree)?tree.flatMap(nodes):[tree,...nodes(tree.props?.children),...nodes(tree.props?.title),...nodes(tree.props?.actions),...nodes(tree.props?.progress)];}
const calls=[],stateSlots={};let scene='youtube',stateIndex=0;const state=v=>{const values=stateSlots[scene]||=([]),i=stateIndex++;if(!(i in values))values[i]=v;return[values[i],next=>values[i]=typeof next==='function'?next(values[i]):next];};
const yt={id:'youtube-id',title:'YouTube fixture',state:'downloading',automatic:0,description:'YouTube details'};
const vod={id:'vod-id',source:'source-id',path:'Films/Fixture.strm',title:'VOD fixture',state:'running',progress:25,description:'TMDB details',artwork:'/artwork/'+'a'.repeat(64)};
const imports={react:{useState:state,lazy:()=> 'LazyM3uDetails',Suspense:'Suspense'},'@mantine/core':{UnstyledButton:'ControlTrigger',Paper:'Paper',Stack:'Stack',Group:'Group',Title:'Title',Text:'Text',Progress:'Progress'},'./components/DetailSheet':{DetailSheet:'DetailSheet'},'./api':{api:async(...args)=>calls.push(args)},'./components/ui':{Button:'Button',Dialog:'Dialog',DialogContent:'DialogContent'},'./DownloadActivityRow':{YouTubeActivityRow:'YouTubeActivityRow',MediaHover:'MediaHover'},'./activityView':{manualYoutubeEntries:items=>items.map(item=>({key:item.id,kind:'download',state:'running',item}))},'./downloadFeed':{useDownloadFeed:()=>({items:[yt]}),refreshDownloads:async()=>{}},'./M3uDownloads':{useJobs:()=>({data:[vod]})},'./M3uLibrary':{M3uDetails:'M3uDetails'},'lucide-react':{Film:'Film',Youtube:'Youtube'}};
imports['./components/ListRow']={__esModule:true,default:'ListRow',CompactActivityRows:'CompactActivityRows'};imports['./components/ActivityStatus']={ActivityStatus:'ActivityStatus',activityStatusColour:state=>state==='queued'?'yellow':'blue'};
const out={};vm.runInNewContext(ts.transpileModule(fs.readFileSync(__dirname+'/src/ManualDownloadTiles.tsx','utf8'),{compilerOptions:{module:ts.ModuleKind.CommonJS,jsx:ts.JsxEmit.ReactJSX}}).outputText,{exports:out,require:n=>n==='react/jsx-runtime'?{jsx,jsxs:jsx,Fragment:'Fragment'}:imports[n]});
(async()=>{
 const youtube=nodes(out.YoutubeDownloadProgress({csrf:'fixture-csrf'}));
 assert.equal(youtube.find(n=>n.type==='Paper'&&n.props.component==='section').props['aria-label'],'YouTube downloads');
 assert.equal(youtube.find(n=>n.type==='YouTubeActivityRow').props.entry.item.description,'YouTube details');
 youtube.find(n=>n.type==='Button').props.onClick();
 scene='vod';const drawVod=()=>{stateIndex=0;return out.VodDownloadProgress({csrf:'fixture-csrf'});};
 const rows=nodes(drawVod());
 assert.equal(rows.find(n=>n.type==='Paper'&&n.props.component==='section').props['aria-label'],'VOD downloads');
 assert.equal(rows.find(n=>n.type==='MediaHover').props.card.overview,'TMDB details');
 assert.equal(rows.find(n=>n.type==='Progress').props.value,25);
 rows.find(n=>n.type==='Button').props.onClick();
 rows.find(n=>n.type==='ControlTrigger'&&n.props.children==='VOD fixture').props.onClick();
 const opened=nodes(drawVod()),suspense=opened.find(n=>n.type==='Suspense');assert.ok(suspense,'opening details loads the media chunk behind Suspense');assert.equal(suspense.props.children.type,'LazyM3uDetails');assert.equal(suspense.props.children.props.item.id,'vod-id');assert.equal(suspense.props.children.props.source,'source-id');assert.equal(suspense.props.fallback.type,'DetailSheet');assert.match(JSON.stringify(suspense.props.fallback),/Opening media details/);suspense.props.fallback.props.onOpenChange(false);assert.ok(!nodes(drawVod()).some(n=>n.type==='Suspense'),'the loading sheet can close without cancelling the download');
 await new Promise(resolve=>setImmediate(resolve));
 assert.equal(calls[0][0],'/api/downloads');assert.equal(calls[0][2].id,'youtube-id');assert.equal(calls[0][2].action,'cancel');assert.equal(calls[0][3],'fixture-csrf');
 assert.equal(calls[1][0],'/api/m3u-server-downloads');assert.equal(calls[1][2].id,'vod-id');assert.equal(calls[1][3],'fixture-csrf');
 console.log('Manual download tiles: progress, metadata hover data and per-engine cancellation passed.');
})().catch(e=>{console.error(e);process.exitCode=1;});
