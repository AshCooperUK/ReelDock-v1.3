const assert=require('node:assert/strict'),fs=require('node:fs'),vm=require('node:vm'),ts=require('typescript');
const React=require('react'),{renderToStaticMarkup:renderMarkup}=require('react-dom/server');
const {MantineProvider}=require('@mantine/core');
const renderToStaticMarkup=tree=>renderMarkup(React.createElement(MantineProvider,{forceColorScheme:'dark'},tree));
function compiled(file){const exports={};vm.runInNewContext(ts.transpileModule(fs.readFileSync(__dirname+'/src/'+file,'utf8'),{compilerOptions:{module:ts.ModuleKind.CommonJS,jsx:ts.JsxEmit.ReactJSX,esModuleInterop:true}}).outputText,{exports,location:{search:''},require:name=>{
 if(name.endsWith('/mediaDuration'))return require('./media-duration-test-helper.cjs')();
 if(name.endsWith('/TileActions'))return Object.assign(()=>null,{mediaTileTarget:()=>({})});
 if(name.endsWith('/HoverCard'))return {__esModule:true,default:()=>null,useCardHover:()=>({anchor:{current:null},open:false,show:()=>{},hide:()=>{},failed:[],failImage:()=>{}}),titleHover:()=>({})};
 if(name.endsWith('/components/browse'))return require('./media-browse-test-helper.cjs')();
 if(name==='@mantine/core')return require(name);
 if(name.endsWith('/components/SelectField'))return require('./select-field-test-helper.cjs')();
 if(name==='react'||name==='react/jsx-runtime')return require(name);
 if(name.endsWith('/api')||name==='./api')return {api:()=>{throw Error('First paint must use the supplied preview');}};
 if(name==='lucide-react')return new Proxy({},{get:()=>()=>null});
 if(name.endsWith('/components/ui'))return {Button:({children})=>React.createElement('button',{},children)};
 if(name.endsWith('/TextField'))return {TextInput:props=>React.createElement('input',{value:props.value,readOnly:true,'aria-label':props['aria-label']})};
 if(name==='./movieOptions'){const choices={};vm.runInNewContext(ts.transpileModule(fs.readFileSync(__dirname+'/src/viewMedia/movieOptions.ts','utf8'),{compilerOptions:{module:ts.ModuleKind.CommonJS}}).outputText,{exports:choices});return choices;}
 if(['./refresh','./pagePreloader'].includes(name)){const refresh={};vm.runInNewContext(ts.transpileModule(fs.readFileSync(__dirname+'/src/viewMedia/'+name.slice(2)+'.ts','utf8'),{compilerOptions:{module:ts.ModuleKind.CommonJS}}).outputText,{exports:refresh});return refresh;}
 if(name==='./localParent')return {createParentOpener:()=>({open:()=>{},cancel:()=>{}})};
 if(name==='./ordering')return {orderCards:items=>items,sortGroups:[]};
 if(name==='./MediaWarnings')return compiled('viewMedia/MediaWarnings.tsx');
 if(name==='../appNavigation')return {mediaSearchQuery:()=>''};
 return {};
}});return exports.default;}
const ViewMedia=compiled('viewMedia/ViewMedia.tsx');
const preview={source:'discover',media:'movies',label:'Discover',items:[{id:'movie',kind:'movie',title:'Preloaded View Media film',year:'2026',action:{type:'discover',item:{id:'movie',kind:'movie',title:'Preloaded View Media film'}}}],nextOffset:null};
const view=renderToStaticMarkup(React.createElement(ViewMedia,{csrf:'test',initial:preview,sourceLocked:true}));
assert.match(view,/Open Preloaded View Media film/);assert.doesNotMatch(view,/Loading media/);
const all=renderToStaticMarkup(React.createElement(ViewMedia,{csrf:'test',initial:preview}));
assert.doesNotMatch(all,/Preloaded View Media film/,'All sources must not display an NZB-only preview as a combined catalogue');
console.log('Source browsing: cached titles render before provider requests; All sources never mislabels an NZB preview.');
