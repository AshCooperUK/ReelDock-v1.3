const sharedSelect=require('./select-field-test-helper.cjs')();
const Mantine=require('@mantine/core');
const assert=require('node:assert/strict'),fs=require('node:fs'),vm=require('node:vm'),ts=require('typescript'),React=require('react'),{renderToStaticMarkup}=require('react-dom/server');
const compile=path=>ts.transpileModule(fs.readFileSync(__dirname+'/src/'+path,'utf8'),{compilerOptions:{module:ts.ModuleKind.CommonJS,jsx:ts.JsxEmit.ReactJSX}}).outputText;
// PopupShell/DetailSheet carry header/footer/tab content in props rather than
// children — walk those too.
function nodes(value){return value==null?[]:Array.isArray(value)?value.flatMap(nodes):typeof value==='object'?[value,...nodes(value.props?.children),...nodes(value.props?.actions),...nodes(value.props?.footer),...nodes(value.props?.title),...nodes(value.props?.subtitle),...nodes(value.props?.media),...nodes(value.props?.tabs?.flatMap(t=>[t.label,t.content]))]:[value];}
let toggles=[],selects=[],inputs=[],calls=[];const out={};
vm.runInNewContext(compile('discovery/ShowOptions.tsx'),{exports:out,require:name=>name.endsWith('/mediaDuration')?require('./media-duration-test-helper.cjs')():name==='react'?React:name==='@mantine/core'?Mantine:name==='../components/SelectField'?sharedSelect:name==='react/jsx-runtime'?require(name):name==='../SettingsUI'?{
 Group:({title,children})=>React.createElement('section',{},React.createElement('h3',{},title),children),Row:({children})=>{if(children.type===sharedSelect.SelectField)selects.push(children.props);return children;},Toggle:props=>{toggles.push(props);return React.createElement('button',{'aria-label':props.title,disabled:props.disabled});}
}:name==='../TextField'?{TextInput:props=>{inputs.push(props);return React.createElement('input',{'aria-label':props['aria-label']});}}:{}});
const value={shown:false,favourite:false,downloadAll:false,downloadFuture:true,automaticSupported:true,quality:'default',minimum:null,globalMinimum:5};
function renderFields(){toggles=[];selects=[];inputs=[];return renderToStaticMarkup(React.createElement(Mantine.MantineProvider,{forceColorScheme:'dark'},React.createElement(out.ShowSettingsFields,{value,save:(...args)=>calls.push(args)})));}
renderFields();assert.equal(toggles.map(t=>t.title).join('|'),'Show in guide|Favourite|Automatic Future Downloads|Download All Seasons');assert.equal(inputs.length,0);
for(const [n,field] of ['shown','favourite','downloadFuture','downloadAll'].entries()){toggles[n].onChange(true);assert.equal(calls.at(-1)[0],field);}
selects.find(s=>s['aria-label']==='TV show quality').onChange({target:{value:'720'}});assert.equal(calls.at(-1).join('|'),'quality|720');
selects.find(s=>s['aria-label']==='TV show minimum length').onChange({target:{value:'custom'}});assert.equal(calls.at(-1).join('|'),'minimum|5');
value.minimum=0;renderFields();assert.equal(inputs.length,1);let valid=true,reported=false;
const blur=minutes=>inputs[0].onBlur({currentTarget:{value:minutes,checkValidity:()=>valid,reportValidity:()=>reported=true}});
blur('5');assert.equal(calls.at(-1).join('|'),'minimum|5');const count=calls.length;valid=false;blur('-1');assert.equal(calls.length,count);assert.equal(reported,true);
selects.find(s=>s['aria-label']==='TV show minimum length').onChange({target:{value:'default'}});assert.equal(calls.at(-1)[1],null);
value.automaticSupported=false;renderFields();assert.ok(!toggles[2].disabled&&toggles[3].disabled,'Existing enabled plans can always be switched off while metadata is unavailable');value.downloadFuture=false;value.downloadAll=false;renderFields();assert.ok(toggles[2].disabled&&toggles[3].disabled);assert.ok(!toggles[0].disabled);
const states=[],popup={};let cursor=0;const item={id:'show:77',kind:'show',title:'A Show',externalId:'77'},episode={key:'show:77:1:1',season:1,number:1,title:'First episode',description:'Episode details',date:'2020-01-01'};
vm.runInNewContext(compile('discovery/DiscoveryTitle.tsx'),{exports:popup,Date,require:name=>{if(name.endsWith('/mediaDuration'))return require('./media-duration-test-helper.cjs')();
  if(name.endsWith('/useChannelBanner'))return {useChannelBanner:()=>undefined};
 if(name==='react')return {useEffect:()=>{},useRef:value=>({current:value}),useState:value=>{const i=cursor++;if(!(i in states))states[i]=i===0?{item,items:[episode],loading:false,nextOffset:null}:value;return [states[i],next=>states[i]=typeof next==='function'?next(states[i]):next];}};
 if(name==='react/jsx-runtime')return require(name);
 if(name==='../discoverPayload')return {acquisitionItem:i=>i};
 if(name==='../mediaDate')return {mediaDate:day=>day};
 if(name==='./MovieInformation')return {useMovieInformation:()=>({info:null}),useShowInformation:()=>({info:null})};
 if(name==='./ShowOptions')return {default:'ShowOptions'};
 if(name==='./EpisodeDownload')return {default:'EpisodeDownload'};
 return new Proxy({},{get:(_o,key)=>key});
}});
function renderPopup(){cursor=0;return nodes(popup.default({item,csrf:'fixture',onClose:()=>{}}));}
let tree=renderPopup();
// Options live in the sheet's Download settings tab — no separate dialog
// button, no per-episode options toggles.
const sheet=tree.find(n=>n.type==='DetailSheet');
assert.equal(sheet.props.tabs.map(t=>t.label).join('|'),'Overview|Downloads|Manage|History','the show sheet exposes one tab per section');
assert.equal(tree.filter(n=>n?.type==='ShowOptions').length,1,'show options render in-sheet');
assert.ok(!tree.some(n=>n?.props?.children==='TV Show Options'),'no separate options dialog button');
assert.ok(!tree.some(n=>n?.props?.title==='Download all episodes'||n?.props?.title==='Download future episodes'));
tree.find(n=>n?.props?.['aria-label']==='Episode').props.onChange({currentTarget:{value:episode.key}});tree=renderPopup();
assert.equal(tree.filter(n=>n?.type==='ShowOptions').length,1,'series policies remain available in Manage while an episode is selected');
assert.ok(tree.some(n=>n?.type==='EpisodeDownload'&&n.props.episode.key===episode.key),'selected exact episode renders inside Downloads');
assert.ok(tree.some(n=>n?.type==='SheetBack'&&n.props.label==='episodes'),'Back returns to the episode picker without replacing the sheet');
assert.equal(tree.find(n=>n.type==='DetailSheet').props.tabs.map(tab=>tab.label).join('|'),'Overview|Downloads|Manage|History');
const guide={};vm.runInNewContext(compile('guideData.ts'),{exports:guide,require:name=>name==='./mediaDuration'?require('./media-duration-test-helper.cjs')():require(name)});
const rows=guide.mapGuide({channels:[],uploads:[],forecasts:[],truncated:false,shows:[{key:item.id,item,favourite:true,events:[{id:episode.key,date:'2026-10-16',label:'Air date',title:'S01E01 · First episode',note:'Air date; time not announced'}]}]},'2026-10-12','2026-10-18');
assert.equal(rows[0].show.id,item.id);assert.equal(rows[0].favourite,true);assert.equal(rows[0].programmes[0].title,'S01E01 · First episode');assert.equal(rows[0].programmes[0].allDay,true);assert.equal(rows[0].programmes[0].videoId,undefined);
console.log('TV Show Options: requested fields, validation, manual-only state, one main-popup action, no episode action and real Guide mapping passed.');

// A header action invalidates the already-mounted options using a read of the
// same show. It must never turn an invalidation into another settings save.
(async()=>{
 const reactive={},state=[],effectSlots=[],pending=[],reads=[],writes=[],target=new EventTarget();let stateCursor=0,effectCursor=0;
 const hooks={useState:initial=>{const index=stateCursor++;if(!(index in state))state[index]=initial;return[state[index],next=>state[index]=typeof next==='function'?next(state[index]):next];},useEffect:(run,deps)=>{const index=effectCursor++,old=effectSlots[index];if(!old||deps.some((value,index)=>!Object.is(value,old.deps[index])))pending.push({index,run,deps});}};
 let response={...value,key:item.id,downloadAll:false};
 vm.runInNewContext(compile('discovery/ShowOptions.tsx'),{exports:reactive,AbortController,CustomEvent,window:target,require:name=>name==='react'?hooks:name==='react/jsx-runtime'?require(name):name==='@mantine/core'?Mantine:name==='../api'?{api:async(url,signal,body,csrf)=>{reads.push({url,signal,body,csrf});return{...response};}}:name==='../discoverPayload'?{acquisitionItem:show=>({id:show.id,kind:show.kind,title:show.title})}:name==='../SettingsUI'?{useAction:()=>({busy:false,notice:'',error:'',run:async(...args)=>{writes.push(args);return null;}})}:{} });
 const flush=()=>new Promise(setImmediate);
 function renderOptions(show=item){stateCursor=0;effectCursor=0;const result=reactive.default({item:show,csrf:'read-csrf',onPolicyChange(){},embedded:true});for(const slot of pending.splice(0)){effectSlots[slot.index]?.cleanup?.();effectSlots[slot.index]={deps:slot.deps,cleanup:slot.run()};}return nodes(result);}
 renderOptions();await flush();let optionsTree=renderOptions();
 assert.equal(reads.length,1);assert.equal(optionsTree.find(n=>n?.type===reactive.ShowSettingsFields).props.value.downloadAll,false);
 target.dispatchEvent(new CustomEvent('reeldock-show-options-changed',{detail:{key:'show:other',field:'downloadAll'}}));renderOptions();await flush();
 assert.equal(reads.length,1,'another show does not invalidate this mounted options pane');
 response={...response,downloadAll:true};target.dispatchEvent(new CustomEvent('reeldock-show-options-changed',{detail:{key:item.id,field:'downloadAll'}}));renderOptions();await flush();optionsTree=renderOptions();
 assert.equal(reads.length,2,'a matching accepted header action rereads the current show options');
 assert.equal(reads[0].signal.aborted,true,'superseded options reads retain abort cleanup');
 assert.equal(optionsTree.find(n=>n?.type===reactive.ShowSettingsFields).props.value.downloadAll,true,'the mounted Download All Seasons switch receives current server state');
 target.dispatchEvent(new CustomEvent('reeldock-show-options-changed'));renderOptions();await flush();
 assert.equal(reads.length,3,'unscoped show-options invalidations safely reread');
 target.dispatchEvent(new CustomEvent('reeldock-watchlist-changed',{detail:{key:'show:other'}}));renderOptions();await flush();
 assert.equal(reads.length,4,'existing watchlist invalidations retain their behavior');
 const nextShow={...item,id:'show:88'};response={...response,key:nextShow.id};renderOptions(nextShow);await flush();
 assert.equal(reads.length,5);target.dispatchEvent(new CustomEvent('reeldock-show-options-changed',{detail:{key:item.id}}));renderOptions(nextShow);await flush();assert.equal(reads.length,5,'identity changes detach the previous show listener');
 target.dispatchEvent(new CustomEvent('reeldock-show-options-changed',{detail:{key:nextShow.id}}));renderOptions(nextShow);await flush();assert.equal(reads.length,6,'the replacement show has its own matching listener');
 for(const read of reads){assert.equal(read.url,'/api/discovery-engine/show-options');assert.equal(read.csrf,'read-csrf');assert.equal(read.body.settings,undefined,'event refresh uses the existing POST-style read without settings mutation');}
 assert.equal(reads.at(-1).body.item.id,nextShow.id);assert.equal(writes.length,0);
 for(const slot of effectSlots)slot.cleanup?.();const revision=state[2];
 target.dispatchEvent(new CustomEvent('reeldock-show-options-changed',{detail:{key:nextShow.id}}));target.dispatchEvent(new CustomEvent('reeldock-watchlist-changed'));
 assert.equal(state[2],revision,'unmount removes both invalidation listeners');assert.equal(reads.at(-1).signal.aborted,true,'unmount aborts the final options read');
 console.log('Mounted TV show options: scoped/header and watchlist refresh, server switch state, POST-style read without save, identity replacement and cleanup passed.');
})().catch(error=>{console.error(error);process.exitCode=1;});
