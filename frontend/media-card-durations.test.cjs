const assert=require('node:assert/strict'),fs=require('node:fs'),vm=require('node:vm'),ts=require('typescript'),React=require('react'),M=require('@mantine/core'),{renderToStaticMarkup}=require('react-dom/server');
const duration=require('./media-duration-test-helper.cjs')(),browse=require('./media-browse-test-helper.cjs')();
const nil=()=>null;
function load(file,expose=''){
 const exports={};vm.runInNewContext(ts.transpileModule(fs.readFileSync(__dirname+'/src/'+file,'utf8'),{compilerOptions:{target:ts.ScriptTarget.ES2022,module:ts.ModuleKind.CommonJS,jsx:ts.JsxEmit.ReactJSX,esModuleInterop:true}}).outputText+expose,{exports,URLSearchParams,require:name=>name==='react'||name==='react/jsx-runtime'||name==='@mantine/core'||name==='lucide-react'?require(name):name.endsWith('/mediaDuration')?duration:name.endsWith('/components/browse')?browse:name.endsWith('/mediaDate')?require('./media-date-test-helper.cjs')():name.endsWith('/HoverCard')?{__esModule:true,default:nil,useCardHover:()=>({anchor:{current:null}}),titleHover:()=>({})}:name.endsWith('/TileActions')?{__esModule:true,default:nil,mediaTileTarget:()=>({})}:name.endsWith('/ChannelIdentity')?{__esModule:true,default:({name})=>React.createElement('span',{},name)}:new Proxy({},{get:(_,key)=>key==='__esModule'?true:nil})});return exports;
}
const {MediaTile}=load('viewMedia/ViewMedia.tsx'),{PlaylistCard}=load('M3uLibrary.tsx','\nexports.PlaylistCard=PlaylistCard;');
const samples=[
 ['YouTube saved seconds',{kind:'youtube',duration_seconds:125,action:{type:'youtube',id:'clip'}},'2m 5s'],
 ['YouTube metadata pending',{kind:'youtube',metadata_pending:true,action:{type:'youtube',id:'clip'}},'Length loading'],
 ['NZB movie',{kind:'movie',runtimeMinutes:110,action:{type:'discover',item:{kind:'movie'}}},'1h 50m'],
 ['Local movie',{kind:'movies',runtimeMinutes:93,action:{type:'local',id:'film'}},'1h 33m'],
 ['Local episode',{kind:'shows',runtimeMinutes:42,action:{type:'local',id:'episode'}},'42m'],
 ['All media local episode',{kind:'show',runtimeMinutes:42,action:{type:'local',id:'episode'}},'42m'],
 ['Individual VOD episode',{kind:'show',durationSeconds:2500,action:{type:'vod',source:'vod',item:{kind:'series'}}},'41m 40s'],
 ['Individual VOD dated TV',{kind:'show',action:{type:'vod',source:'vod',item:{kind:'tv'}}},'Length unavailable'],
 ['Unknown movie',{kind:'movie',action:{type:'local',id:'unknown'}},'Length unavailable'],
 ['ISO provider duration',{kind:'video',duration:'PT1H2M3S',action:{type:'youtube',id:'clip'}},'1h 2m 3s'],
];
for(const scheme of ['light','dark'])for(const layout of ['grid','list'])for(const [title,data,label]of samples){
 const item={id:title,title,image:'/fixture.jpg',...data};
 const html=renderToStaticMarkup(React.createElement(M.MantineProvider,{forceColorScheme:scheme},React.createElement(MediaTile,{item,layout,searched:false,upcoming:false,label:'Media',onOpen(){}})));
 assert.equal((html.match(/data-media-duration=/g)||[]).length,1,title+' has one shared badge');
 assert.ok(html.includes(label),title+' exposes its known or honest unknown length');
 assert.ok(html.includes('aria-label="'+(label.startsWith('Length')?label:'Length '+label)+'"'),title+' has an accessible duration');
 assert.equal((html.match(/<img\b/g)||[]).length,1,'the badge creates no extra artwork');
}
for(const item of [
 {kind:'channel',runtimeMinutes:42,action:{type:'youtube',id:'channel'}},
 {kind:'show',runtimeMinutes:42,action:{type:'discover',item:{kind:'show'}}},
 {kind:'shows',runtimeMinutes:42,action:{type:'local-group',id:'group'}},
]){
 const html=renderToStaticMarkup(React.createElement(M.MantineProvider,null,React.createElement(MediaTile,{item:{id:'parent',title:'Parent',...item},layout:'grid',searched:false,onOpen(){}})));
 assert.doesNotMatch(html,/data-media-duration=/,'parents do not pretend to be one video');
}
for(const kind of ['show','shows'])for(const count of [1,3])for(const layout of ['grid','list']){
 const html=renderToStaticMarkup(React.createElement(M.MantineProvider,null,React.createElement(MediaTile,{item:{id:'parent',title:'Grouped show',kind,count,children:[],action:{type:'local-group',id:'group'}},layout,searched:false,onOpen(){}})));
 assert.ok(html.includes(count+' '+(count===1?'episode':'episodes')),'both normalized All media and Local Library show parents display episode counts');
 assert.doesNotMatch(html,/data-media-duration=/,'one show card never borrows an episode runtime');
}
for(const kind of ['movies','series','tv','unsorted']){
 const html=renderToStaticMarkup(React.createElement(M.MantineProvider,null,React.createElement(PlaylistCard,{source:'vod',item:{kind,path:'fixture.strm',title:'Individual entry',durationSeconds:1800},onOpen(){}})));
 assert.match(html,/data-media-duration=/);assert.match(html,/30m/);
}
assert.equal(duration.mediaCardLength({kind:'show',episodeKey:'episode:1',runtimeMinutes:99,episode:{runtimeMinutes:42}}),'42m');
assert.equal(duration.mediaCardLength({kind:'show',episodeKey:'episode:1',runtimeMinutes:99}),'Length unavailable','a series duration cannot be copied onto an unknown episode');
console.log('Actual shared card consumers show one known/loading/unavailable length across YouTube, NZB, VOD, local and episode media; parents remain excluded.');
