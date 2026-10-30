const assert=require('node:assert/strict'),fs=require('node:fs'),vm=require('node:vm'),ts=require('typescript');
const React=require('react'),M=require('@mantine/core'),{renderToStaticMarkup}=require('react-dom/server');
const jsx=(type,props)=>({type,props:props||{}});
const nodes=node=>node==null?[]:Array.isArray(node)?node.flatMap(nodes):typeof node==='object'?[node,...nodes(node.props?.children),...nodes(node.props?.actions),...nodes(node.props?.tabs?.map(tab=>tab.content))]:[];
function load(file,{seed=(_i,value)=>value,modules={},real=false}={}){
 let state=0;const out={},react={...React,useEffect(){},useState:value=>[seed(state++,typeof value==='function'?value():value),()=>{}],useRef:value=>({current:value})};
 vm.runInNewContext(ts.transpileModule(fs.readFileSync(__dirname+'/src/'+file,'utf8'),{compilerOptions:{target:ts.ScriptTarget.ES2022,module:ts.ModuleKind.CommonJS,jsx:ts.JsxEmit.ReactJSX}}).outputText,{exports:out,Date,Intl,Set,URLSearchParams,AbortController,require:name=>{if(name.endsWith('/mediaDuration'))return require('./media-duration-test-helper.cjs')();
  if(name.endsWith('/useChannelBanner'))return {useChannelBanner:()=>undefined};
  if(name==='react')return react;if(name==='react/jsx-runtime')return real?require(name):{jsx,jsxs:jsx,Fragment:'Fragment'};
  if(real&&name==='@mantine/core')return M;
  if(modules[name])return modules[name];
  if(name.endsWith('discoverPayload'))return {acquisitionItem:item=>item};
  if(name==='./MovieInformation')return {useMovieInformation:()=>({info:null}),useShowInformation:()=>({info:null})};
  return new Proxy({},{get:(_,key)=>key==='__esModule'?false:key==='default'?name.split('/').pop():key});
 }});return out;
}
const channel={id:'UC'+'a'.repeat(22),name:'Channel',shown:true,forecasts:true,favourite:true,downloads:{videos:true,shorts:false,live:false,quality:'1080',quality_override:1,range_days:7,keep_files:true,sponsorblock:'default',force_transcode:'default'}};
const movie={id:'movie:77',kind:'movie',title:'Movie'},show={id:'show:88',kind:'show',title:'Show'};
const movieValue={item:movie,shown:true,dates:true,favourite:true,automatic:true,quality:'default',minimum:null,globalMinimum:10};
const showValue={shown:true,favourite:true,downloadFuture:true,downloadAll:false,automaticSupported:true,quality:'default',minimum:null,globalMinimum:10};

// Render the real field helpers with Mantine switches. The optional flag removes
// exactly the duplicate Favourite; every other policy and its callback survives.
for(const [file,field,props] of [
 ['discovery/MovieOptions.tsx','MovieSettingsFields',{value:movieValue}],
 ['discovery/ShowOptions.tsx','ShowSettingsFields',{value:showValue}],
 ['ChannelOptions.tsx','ChannelSettingsFields',{channel}],
]){
 let controls=[],writes=[];
 const settings={Group:({children})=>React.createElement(M.Stack,null,children),Row:({children})=>React.createElement(M.Box,null,children),Toggle:props=>{controls.push(props);return React.createElement(M.Switch,{label:props.title,checked:props.value,disabled:props.disabled,onChange:event=>props.onChange(event.currentTarget.checked)});}};
 const fields=load(file,{real:true,modules:{'../SettingsUI':settings,'./SettingsUI':settings,'../components/SelectField':{SelectField:()=>null},'./components/SelectField':{SelectField:()=>null}}})[field];
 const render=extra=>{controls=[];return renderToStaticMarkup(React.createElement(M.MantineProvider,{forceColorScheme:'dark'},React.createElement(fields,{...props,...extra,save:(...args)=>writes.push(args),preference:(...args)=>writes.push(args),rule:(...args)=>writes.push(args)})));};
 const standalone=render({}),allControls=controls.map(control=>control.title);
 assert.equal(controls.filter(control=>control.title==='Favourite').length,1,file+' keeps standalone/bulk Favourite');
 controls.find(control=>control.title==='Favourite').onChange(false);assert.equal(writes.at(-1).join('|'),'favourite|false');
 assert.match(standalone,/role="switch"/);
 render({showFavourite:false});assert.deepEqual(controls.map(control=>control.title),allControls.filter(title=>title!=='Favourite'),file+' only removes the duplicate flag');
 for(const control of controls){control.onChange(!control.value);assert.notEqual(writes.at(-1)[0],'favourite',file+' retains the independent policy callback');}
}

// Wrapper defaults preserve callers without a header heart, even when embedded.
for(const [file,field,props,value] of [
 ['discovery/MovieOptions.tsx','MovieSettingsFields',{item:movie,csrf:'fixture'},movieValue],
 ['discovery/ShowOptions.tsx','ShowSettingsFields',{item:show,csrf:'fixture',onPolicyChange(){}},showValue],
 ['ChannelOptions.tsx','ChannelSettingsFields',{channel,csrf:'fixture',onChange(){},onRemoved(){},onClose(){}},null],
]){
 for(const visible of [undefined,false]){
  const out=load(file,{seed:(index,initial)=>index===0&&value?value:initial,modules:{'../SettingsUI':{useAction:()=>({busy:false})},'./SettingsUI':{useAction:()=>({busy:false})}}});
  const tree=out.default({...props,embedded:true,...(visible===false?{showFavourite:false}:{})});
  assert.equal(nodes(tree).find(node=>node.type===out[field]).props.showFavourite,visible!==false,file+' forwards the explicit flag with a safe default');
 }
}
for(const item of [movie,show])for(const visible of [undefined,false]){
 const vod=load('collections/VodOptions.tsx',{seed:(index,initial)=>index===0?{item,csrf:'fixture'}:initial});
 const child=vod.default({source:'exact-source',path:'exact.strm',embedded:true,...(visible===false?{showFavourite:false}:{})});
 assert.equal(child.props.item.id,item.id);assert.equal(child.props.showFavourite,visible!==false,'VOD forwards the same flag through both catalogue kinds');
}

// Every opt-out is paired with the visible heart for precisely that identity.
for(const item of [movie,show]){
 const out=load('discovery/DiscoveryTitle.tsx',{seed:(index,initial)=>index===0?{item,items:[],loading:false,nextOffset:null}:initial});
 const sheet=out.default({item,csrf:'fixture',onClose(){}}),header=nodes(sheet.props.actions).filter(node=>node.type==='TileActions');
 assert.equal(header.length,1);assert.equal(header[0].props.item.id,item.id);
 const options=nodes(sheet.props.tabs[2].content).find(node=>node.type===(item.kind==='movie'?'MovieOptions':'ShowOptions'));
 assert.equal(options.props.item.id,item.id);assert.equal(options.props.showFavourite,false);
}
const channelSheet=load('ChannelDetail.tsx',{modules:{'./SettingsUI':{useSettings:url=>url.startsWith('/api/subscriptions')?{data:{items:[channel],csrf:'fixture'},reload(){},setData(){}}:{data:{channel,csrf:'fixture'},reload(){}}}}}).default({target:{id:channel.id},readOnly:false,mode(){},close(){}});
assert.equal(nodes(channelSheet.props.actions).filter(node=>node.type==='TileActions').length,1);assert.equal(nodes(channelSheet.props.actions).find(node=>node.type==='TileActions').props.item.channelId,channel.id);
assert.equal(nodes(channelSheet.props.tabs[2].content).find(node=>node.type==='ChannelOptions').props.showFavourite,false);
const vodSheet=load('M3uLibrary.tsx').M3uDetails({source:'exact-source',item:{path:'exact.strm',kind:'movies',title:'VOD'}});
assert.equal(nodes(vodSheet.props.actions).filter(node=>node.type==='TileActions').length,1);
const headerTarget=nodes(vodSheet.props.actions).find(node=>node.type==='TileActions').props.resolve,options=nodes(vodSheet.props.tabs[2].content).find(node=>node.type==='VodOptions');
assert.equal(options.props.source,headerTarget.source);assert.equal(options.props.path,headerTarget.path);assert.equal(options.props.showFavourite,false);
console.log('Favourite controls: one header heart per embedded identity; standalone/bulk defaults, VOD forwarding and all independent policy callbacks retained.');
