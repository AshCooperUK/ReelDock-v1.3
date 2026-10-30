const assert=require('node:assert/strict'),fs=require('node:fs'),vm=require('node:vm'),ts=require('typescript');
const React=require('react'),{renderToStaticMarkup:markup}=require('react-dom/server');
const Mantine=require('@mantine/core');const renderToStaticMarkup=tree=>markup(React.createElement(Mantine.MantineProvider,{forceColorScheme:'dark'},tree));
function load(file,resolve){const out={};vm.runInNewContext(ts.transpileModule(fs.readFileSync(__dirname+'/src/'+file,'utf8'),{compilerOptions:{module:ts.ModuleKind.CommonJS,jsx:ts.JsxEmit.ReactJSX,esModuleInterop:true}}).outputText,{exports:out,Date,require:name=>name==='@mantine/core'?Mantine:name==='react/jsx-runtime'?require(name):resolve(name)});return out;}
const calls=[],jsx=(type,props)=>({type,props:props||{}});
const settings=load('settings/CatalogueSettings.tsx',name=>name==='react'?{useEffect:()=>{},useState:v=>[v,()=>{}]}:name==='./useEngineSettings'?{useEngineSettings:()=>({values:{enabled:true,minutes:60},save:v=>calls.push(v)})}:name==='../SettingsUI'?Object.fromEntries(['Group','Row','Toggle','Notice'].map(name=>[name,name])):{});
// Inspect shared control props before rendering the complete settings panel.
function nodes(node){if(!node||typeof node!=='object')return[];return Array.isArray(node)?node.flatMap(nodes):[node,...nodes(node.props?.children)];}
const tree=settings.default({csrf:'fixture'}),all=nodes(tree);
all.find(n=>n.type==='Toggle').props.onChange(false);assert.equal(calls.at(-1).enabled,false);
const interval=all.find(n=>n.props?.['aria-label']==='Catalogue refresh interval');assert.equal(interval.props.value,60);interval.props.onChange({target:{value:'180'}});assert.equal(calls.at(-1).minutes,180);
let stateIndex=0,transferPanels=0;
const movie={id:'movie:1234567',kind:'movie',title:'Future movie',year:'2099'},row={key:movie.id,title:movie.title,date:'2099-01-01',status:'Waiting for release',file:false,requested:false,requestState:''};
const popup=load('discovery/DiscoveryTitle.tsx',name=>{
 if(name==='react')return{useState:v=>[++stateIndex===1?{item:movie,items:[row],loading:false,nextOffset:null}:v,()=>{}],useEffect:()=>{},useRef:v=>({current:v})};
 if(name==='../components/ui')return{Button:({variant,...p})=>React.createElement('button',p),Dialog:({children})=>children,DialogContent:({children})=>children,DialogTitle:({children})=>React.createElement('h2',{},children),DialogDescription:({children})=>React.createElement('p',{},children),PanelButton:({title,children})=>React.createElement('button',{},title,children),PopupShell:({title,subtitle,actions,media,children,footer})=>React.createElement('div',{},media,title,subtitle,actions,children,footer)};
 // DetailSheet renders one overlay: header props plus every tab label and —
 // for the test — every tab's content, so the in-sheet panels can be checked.
 if(name==='../components/DetailSheet')return{DetailSheet:({title,subtitle,actions,media,tabs,back,children})=>React.createElement('div',{},media,title,subtitle,actions,back&&'Back to '+back.label,tabs?.map(t=>React.createElement('button',{key:t.id},t.label)),tabs?.map(t=>React.createElement('section',{key:t.id},t.content)),children)};
 if(name==='../MediaManagement')return{__esModule:true,default:()=>React.createElement('div',{},'Managed media'),MediaTitleHistory:()=>React.createElement('div',{},'Title download history')};
 if(name==='../discoverPayload')return{acquisitionItem:v=>v};
 if(name==='./MovieInformation')return{useMovieInformation:()=>({info:null}),useShowInformation:()=>({info:null}),MovieFacts:()=>null,MovieCast:()=>null,MovieTrailer:()=>null};
 if(name==='./MovieDownloadStatus')return()=>{transferPanels++;return React.createElement('div',{},'Existing request');};
 return new Proxy({},{get:()=>()=>null});
});
const html=renderToStaticMarkup(React.createElement(popup.default,{item:movie,csrf:'fixture',onClose:()=>{}}));
// Options and management are sheet tabs, not extra header dialogs.
assert.match(html,/Downloads/);assert.match(html,/Manage/);assert.match(html,/History/);
assert.match(html,/Managed media/,'media management renders inside the sheet');
assert.match(html,/>Manage</,'management is a shared sheet section');
assert.doesNotMatch(html,/Movie Options/,'no separate options dialog button — settings are a sheet tab');
assert.doesNotMatch(html,/Download when released|Cancel request|Finding available/);assert.equal(transferPanels,0);
// TV shows expose the same tabbed sheet plus the Episodes section.
stateIndex=0;
const showHtml=renderToStaticMarkup(React.createElement(popup.default,{item:{id:'show:99',kind:'show',title:'Fixture show',year:'2024'},csrf:'fixture',onClose:()=>{}}));
assert.match(showHtml,/Choose an episode|Choose what to download/);assert.match(showHtml,/Downloads/);assert.match(showHtml,/Managed media/,'the TV show sheet exposes in-sheet media management');
assert.doesNotMatch(showHtml,/TV Show Options/,'no separate TV options dialog button');
// Movie Options hosts the same panel inside its own dialog body.
const movieOptions=load('discovery/MovieOptions.tsx',name=>{
 if(name==='react')return{useState:v=>[v,()=>{}],useEffect:()=>{}};
 if(name==='../components/ui')return{Button:({variant,...p})=>React.createElement('button',p),DialogTitle:({children})=>React.createElement('h2',{},children),DialogDescription:({children})=>React.createElement('p',{},children),PopupShell:({title,subtitle,actions,media,children,footer})=>React.createElement('div',{},media,title,subtitle,actions,children,footer)};
 if(name==='../SettingsUI')return{Group:({children})=>children,Row:({children})=>children,Toggle:()=>null,Notice:()=>null,useAction:()=>({busy:false,notice:'',error:'',run:async()=>null})};
 if(name==='../TextField')return{TextInput:p=>React.createElement('input',p)};
 return new Proxy({},{get:()=>()=>null});
});
const optionsHtml=renderToStaticMarkup(React.createElement(movieOptions.default,{item:movie,csrf:'fixture',files:React.createElement('div',{},'Files panel slot')}));
assert.match(optionsHtml,/Files panel slot/,'Movie Options renders the management slot inside its dialog');
row.requested=true;row.requestState='waiting';stateIndex=0;
const waiting=renderToStaticMarkup(React.createElement(popup.default,{item:movie,csrf:'fixture',onClose:()=>{}}));
assert.equal(transferPanels,1,'existing future requests retain status/cancellation without recreating the removed action');assert.doesNotMatch(waiting,/Download when released/);
console.log('Saved catalogue: shared metadata switch/hourly intervals and future movie downloads only in Movie Options passed.');
