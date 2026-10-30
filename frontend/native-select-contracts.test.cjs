const sharedSelect=require('./select-field-test-helper.cjs')();
const assert=require('node:assert/strict'),fs=require('node:fs'),vm=require('node:vm'),ts=require('typescript');
const React=require('react'),jsxRuntime=require('react/jsx-runtime'),Mantine=require('@mantine/core');
const {renderToStaticMarkup}=require('react-dom/server');
const h=React.createElement;
let seeds=[],cursor=0,updates=[];
const wrapper=({children})=>h('div',null,children);
const hooks={...React,useEffect:()=>{},useLayoutEffect:()=>{},useRef:value=>({current:value}),useCallback:fn=>fn,
 useState:value=>{const index=cursor++;return [index in seeds?seeds[index]:typeof value==='function'?value():value,next=>updates.push([index,next])];}};
const components={Button:({children,variant,...props})=>h('button',props,children),PopupShell:wrapper,DialogTitle:wrapper,DialogDescription:wrapper};
const settings={Group:wrapper,Row:wrapper,Field:({label,children})=>h('label',null,label,children),Toggle:()=>null,Notice:()=>null,useAction:()=>({busy:false,run:async()=>null})};
function compile(name,extra={}){
 const out={};
 const modules={'../components/SelectField':sharedSelect,'./components/SelectField':sharedSelect,react:hooks,'react/jsx-runtime':jsxRuntime,'@mantine/core':Mantine,'lucide-react':require('lucide-react'),
  '../components/ui':components,'./components/ui':components,'../SettingsUI':settings,'./SettingsUI':settings,
  '../TextField':{TextInput:props=>h('input',props)},'./TextField':{TextInput:props=>h('input',props),TextArea:props=>h('textarea',props)},
  '../notifications':{NotificationMessage:()=>null},'./notifications':{NotificationMessage:()=>null},
  '../discoverPayload':{acquisitionItem:item=>item},'./api':{api:async()=>({})},'../api':{api:async()=>({})},...extra};
 vm.runInNewContext(ts.transpileModule(fs.readFileSync(__dirname+'/src/'+name,'utf8'),{compilerOptions:{module:ts.ModuleKind.CommonJS,jsx:ts.JsxEmit.ReactJSX}}).outputText,{exports:out,require:key=>modules[key]||{},AbortController,URLSearchParams,Error});
 return out;
}
const nodes=value=>Array.isArray(value)?Array.from(value).flatMap(nodes):value&&typeof value==='object'?[value,...nodes(value.props?.children)]:[];
const select=(tree,label)=>nodes(tree).find(node=>node.type===sharedSelect.SelectField&&node.props['aria-label']===label);
const render=tree=>renderToStaticMarkup(h(Mantine.MantineProvider,{forceColorScheme:'dark'},tree));
const values=tree=>Array.from(sharedSelect.normalizeSelectData(tree.props)).flatMap(row=>row.items||[row]).map(row=>row.value);
let choice;
const panel=compile('downloadUI/DownloadPanel.tsx').default({quality:'1080',busy:true,onQuality:value=>choice=value});
const quality=select(panel,'Preferred quality');assert.ok(quality);assert.deepEqual(values(quality),['720','1080','2160']);
quality.props.onChange({target:{value:'2160'}});assert.equal(choice,'2160','download policy receives a native select event value');
const panelHTML=render(panel);assert.match(panelHTML.match(/<input[^>]*aria-label="Preferred quality"[^>]*>/)[0],/disabled=""/);assert.match(panelHTML,/role="combobox"/);assert.match(panelHTML,/value="1080p"/);assert.ok(!panelHTML.includes('<select'));

const changed=[];
const movie=compile('discovery/MovieOptions.tsx').MovieSettingsFields({value:{shown:true,dates:true,favourite:false,automatic:false,quality:'default',minimum:null,globalMinimum:20},save:(...args)=>changed.push(args)});
assert.deepEqual(values(select(movie,'Movie quality')),['default','720','1080','2160']);
select(movie,'Movie quality').props.onChange({target:{value:'720'}});
select(movie,'Movie minimum length').props.onChange({target:{value:'custom'}});
select(movie,'Movie minimum length').props.onChange({target:{value:'default'}});
assert.deepEqual(changed,[['quality','720'],['minimum',20],['minimum',null]],'inheritance/custom minimum keep their original domain values');
assert.match(render(movie),/<input[^>]*aria-label="Movie quality"[^>]*role="combobox"/);
const show=compile('discovery/ShowOptions.tsx').ShowSettingsFields({value:{shown:true,favourite:true,downloadAll:false,downloadFuture:true,automaticSupported:true,quality:'1080',minimum:15,globalMinimum:30},save:(...args)=>changed.push(args)});
select(show,'TV show quality').props.onChange({target:{value:'2160'}});assert.deepEqual(changed.at(-1),['quality','2160']);
select(show,'TV show minimum length').props.onChange({target:{value:'default'}});assert.deepEqual(changed.at(-1),['minimum',null]);
assert.match(render(show),/value="Custom minimum"/);

seeds=[{locked:false,mode:'',data:{},media_type:'movie',name:'Film',year:2026,tmdb_saved:false},'','search','','','movie',null];cursor=0;
const fix=compile('FixMatch.tsx').default({path:'film.mkv',csrf:'fixture',onChanged(){},embedded:true});
assert.deepEqual(values(select(fix,'Match method')),['search','manual']);assert.deepEqual(values(select(fix,'Match media type')),['movie','tv']);
select(fix,'Match method').props.onChange({target:{value:'manual'}});assert.deepEqual(updates.at(-1),[2,'manual']);
select(fix,'Match media type').props.onChange({target:{value:'tv'}});assert.deepEqual(updates.at(-1),[5,'tv']);
assert.match(render(fix),/<fieldset[^>]*disabled=""/,'TMDB-unavailable fieldset still disables native form controls');

const files=['M3uLibrary.tsx','Guide.tsx','FixMatch.tsx','ChannelViews.tsx','downloadUI/DownloadPanel.tsx','discovery/MovieOptions.tsx','discovery/ShowOptions.tsx'];
for(const file of files){
 const source=fs.readFileSync(__dirname+'/src/'+file,'utf8'),ast=ts.createSourceFile(file,source,ts.ScriptTarget.Latest,true,ts.ScriptKind.TSX);
 assert.match(source,/import \{ SelectField as NativeSelect \} from ['"][^'"]*SelectField['"]/,file+' imports the shared styled Mantine Select adapter');
 let native=0;function walk(node){if(ts.isJsxOpeningElement(node)||ts.isJsxSelfClosingElement(node)){
  assert.notEqual(node.tagName.getText(ast),'select',file+' has no unmanaged native select');
  if(node.tagName.getText(ast)==='NativeSelect'){native++;assert.ok(node.attributes.properties.some(prop=>prop.name?.getText(ast)==='aria-label'),file+' retains a control accessible name');}
 }ts.forEachChild(node,walk);}walk(ast);assert.ok(native,file+' has a framework select');
}
const episode=fs.readFileSync(__dirname+'/src/discovery/EpisodeDownload.tsx','utf8');
// Episode source selection now uses the shared readable in-sheet picker.
assert.match(episode,/import \{[^}]*DownloadSourcePicker[^}]*\} from ['\"]\.\/ReleaseChoicePicker['\"]/);
assert.match(episode,/<DownloadSourcePicker /,'episode source choice stays on the shared framework control');
assert.doesNotMatch(episode,/<select(?:\s|>)/,'episode source has no unmanaged native select');
assert.ok(!/<span>(Source|Release)<\/span>/.test(episode),'source/release controls have accessible names without redundant adjacent text');
const m3u=fs.readFileSync(__dirname+'/src/M3uLibrary.tsx','utf8');assert.ok(!/>Subscription<NativeSelect/.test(m3u));
const discover=fs.readFileSync(__dirname+'/src/Discover.tsx','utf8');assert.match(discover,/<Button type="submit" disabled=\{query\.trim\(\)\.length\s*<\s*2\}>Search<\/Button>/,'provider Search submits its form with mouse or keyboard');
seeds=[];cursor=0;const searches=[];
const provider=compile('Discover.tsx',{'./api':{api:async(url,signal)=>{searches.push({url,signal});return {items:[],nextOffset:null};}}}).ProviderSearch({section:'Films',layout:'grid',initialQuery:'  Night & Day  ',onSelect(){}});
const form=nodes(provider).find(node=>node.type==='form'||node.props?.component==='form');let prevented=false;
form.props.onSubmit({preventDefault(){prevented=true;}});
assert.ok(prevented);assert.equal(searches.length,1,'the actual search form makes one provider request');
const searchURL=new URL(searches[0].url,'https://fixture.invalid');
assert.equal(searchURL.searchParams.get('provider'),'movies');assert.equal(searchURL.searchParams.get('q'),'Night & Day');assert.equal(searchURL.searchParams.get('offset'),'0');
assert.ok(searches[0].signal instanceof AbortSignal,'form search remains cancellable');
assert.match(render(provider),/<button[^>]*type="submit"[^>]*>Search<\/button>/,'real SSR produces a native submit control');
console.log('Styled selects: real Mantine combobox SSR/native callback values, disabled fieldsets, inherited policies, accessible names, label cleanup and provider submit semantics passed.');
