const assert=require('node:assert/strict'),fs=require('node:fs'),vm=require('node:vm'),ts=require('typescript');
const dates={};vm.runInNewContext(ts.transpileModule(fs.readFileSync(__dirname+'/src/mediaDate.ts','utf8'),{compilerOptions:{module:ts.ModuleKind.CommonJS}}).outputText,{exports:dates});
const jsx=(type,props)=>({type,props:props||{}}),out={};let opened,favourites=0;
const code=ts.transpileModule(fs.readFileSync(__dirname+'/src/ChannelViews.tsx','utf8'),{compilerOptions:{module:ts.ModuleKind.CommonJS,jsx:ts.JsxEmit.ReactJSX}}).outputText;
vm.runInNewContext(code,{setTimeout,clearTimeout,exports:out,require:n=>n==='./mediaDate'?dates:n==='react'?{useRef:value=>({current:value}),useEffect:()=>{}}:n==='react/jsx-runtime'?{jsx,jsxs:jsx}:n==='./channelNavigation'?{openChannel:v=>opened=v}:n==='./ChannelIdentity'?{default:'ChannelIdentity'}:n==='./ChannelOptions'?{diskSize:()=> '1 GB'}:new Proxy({},{get:(_,k)=>k})});
const nodes=t=>!t||typeof t!=='object'?[]:Array.isArray(t)?t.flatMap(nodes):[t,...nodes(t.props?.children),...nodes(t.props?.actions),...nodes(t.props?.art),...nodes(t.props?.title)];
const item={id:'channel',name:'Channel',favourite:true,subscribers:100,video_count:50,latest_upload:'2026-10-03T10:00:00Z',downloads:{quality:'1080',range_days:7,videos:true},shown:true,forecasts:false};
for(const view of ['tile','list','compact','detailed','max']){
 const tree=out.default({item,view,saving:false,onFavourite:()=>favourites++}),all=nodes(tree);
 const star=all.find(n=>n.props['aria-label']==='Favourite Channel');assert.equal(star.props['aria-pressed'],true);star.props.onClick();
 const settings=all.find(n=>n.props['aria-label']==='Channel options: Channel');settings.props.onClick();assert.equal(opened.mode,'options');assert.equal(opened.id,'channel');
 assert.ok(JSON.stringify(tree).includes('ChannelIdentity'),'every view retains the shared channel navigation control');
 if(view==='detailed'||view==='max')assert.ok(JSON.stringify(tree).includes('Last video uploaded'));
 if(view==='list'||view==='compact')assert.ok(!JSON.stringify(tree).includes('Total videos'));
}
assert.equal(favourites,5);console.log('Channel views: five layouts, shared options, favourites, compact identity and detailed metadata passed.');

const ordered=out.favouriteChannels([{id:'a',name:'Alpha',favourite:false},{id:'z',name:'Zulu',favourite:true},{id:'b',name:'Beta',favourite:true}]);assert.equal(ordered.map(x=>x.id).join(','),'b,z,a');ordered[2].favourite=true;assert.equal(out.favouriteChannels(ordered)[0].id,'a');

let selected=0;
const selectionTree=out.default({item,view:'list',saving:false,onFavourite:()=>{},onSelect:()=>selected++});
const event=(identity,shift,control)=>({target:{closest:q=>q.includes('channel-identity')?identity:control},shiftKey:shift,preventDefault(){},stopPropagation(){}});
selectionTree.props.onClickCapture(event(false,false,false));assert.equal(selected,1);
selectionTree.props.onClickCapture(event(true,true,true));assert.equal(selected,2);
selectionTree.props.onClickCapture(event(true,false,true));assert.equal(selected,2);
selectionTree.props.onClickCapture(event(false,false,true));assert.equal(selected,2);
const bulk={};vm.runInNewContext(ts.transpileModule(fs.readFileSync(__dirname+'/src/BulkChannelOptions.tsx','utf8'),{compilerOptions:{module:ts.ModuleKind.CommonJS,jsx:ts.JsxEmit.ReactJSX}}).outputText,{exports:bulk,require:()=>({})});
const mixed=bulk.mixedChannelFields([item,{...item,favourite:false,downloads:{...item.downloads,videos:1,quality:'720',quality_override:1}}]);
assert.ok(mixed.has('favourite'));assert.ok(mixed.has('quality'));assert.ok(!mixed.has('videos'));assert.ok(!mixed.has('shown'));
console.log('Bulk channels: background/Shift selection, normal controls preserved, mixed values and boolean normalisation passed.');

for(const [values,pressed,next] of [[[],false,true],[[false,false],false,true],[[true,false,true],'mixed',true],[[true,true],true,false]]){
 const result=bulk.selectedFavouriteState(values.map(favourite=>({favourite})));
 assert.equal(result.pressed,pressed);assert.equal(result.next,next);
}
console.log('Bulk favourite toggle: empty, none, mixed and all selected states passed.');

(async()=>{
 let touches=0,blocked=false;
 const card=out.default({item,view:'compact',saving:false,onFavourite:()=>{},onSelect:()=>touches++});
 const pointer={pointerType:'touch',clientX:0,clientY:0,target:{closest:()=>true}};
 card.props.onPointerDownCapture(pointer);
 await new Promise(resolve=>setTimeout(resolve,600));
 assert.equal(touches,1);
 card.props.onPointerUpCapture();
 card.props.onClickCapture({...event(true,false,true),preventDefault(){blocked=true;}});
 assert.equal(blocked,true);assert.equal(touches,1);
 card.props.onPointerDownCapture(pointer);card.props.onPointerMoveCapture({...pointer,clientY:30});
 await new Promise(resolve=>setTimeout(resolve,600));assert.equal(touches,1);
 card.props.onPointerDownCapture(pointer);card.props.onPointerUpCapture();
 await new Promise(resolve=>setTimeout(resolve,600));assert.equal(touches,1);
 console.log('Touch selection: long press selects once, suppresses opening, scrolling and short taps cancel selection.');
})().catch(error=>{console.error(error);process.exitCode=1;});

assert.equal(out.uploadStamp('2026-10-03T22:29:00Z'),'Date: 03.10.2026 · Time: 23:29');
assert.equal(out.uploadStamp('2026-12-03T22:29:00Z'),'Date: 03.12.2026 · Time: 22:29');
assert.equal(out.uploadStamp('2026-10-03'),'Date: 03.10.2026 · Time: Not available');
assert.ok(out.uploadStamp(null).includes('Not available'));
assert.ok(!JSON.stringify(out.default({item,view:'compact',saving:false,onFavourite:()=>{}})).includes('Last video uploaded'));

const inherited={...item,downloads:{...item.downloads,range_override:0}};
assert.ok(bulk.mixedChannelFields([inherited,{...item,downloads:{...item.downloads,range_override:1}}]).has('range_days'));
assert.ok(!bulk.mixedChannelFields([inherited,{...inherited,downloads:{...inherited.downloads,range_days:30}}]).has('range_days'));
const fields={};
vm.runInNewContext(ts.transpileModule(fs.readFileSync(__dirname+'/src/ChannelOptions.tsx','utf8'),{compilerOptions:{module:ts.ModuleKind.CommonJS,jsx:ts.JsxEmit.ReactJSX}}).outputText,{exports:fields,require:n=>n==='react/jsx-runtime'?{jsx,jsxs:jsx}:new Proxy({},{get:(_,k)=>k})});
let savedRange;
const range=nodes(fields.ChannelSettingsFields({channel:inherited,rule:(key,value)=>savedRange=[key,value],preference:()=>{}})).find(n=>n.props['aria-label']==='Download range');
assert.equal(range.props.value,'default');
range.props.onChange({target:{value:'default'}});assert.deepEqual(savedRange,['range_days','default']);
range.props.onChange({target:{value:'0'}});assert.deepEqual(savedRange,['range_days',0]);
console.log('Channel range: inherited selection, explicit all uploads and bulk mixed modes passed.');

let preferenceChange;
const subscriptionFields=nodes(fields.ChannelSettingsFields({channel:{...inherited,downloads:{...inherited.downloads,minimum_video_minutes:5,minimum_override:0,subscription_paused:0}},rule:(key,value)=>preferenceChange=[key,value],preference:()=>{}}));
const minimumSelect=subscriptionFields.find(n=>n.props['aria-label']==='Channel minimum length');
assert.equal(minimumSelect.props.value,'default');
minimumSelect.props.onChange({target:{value:'custom'}});assert.deepEqual(preferenceChange,['minimum_video_minutes',5]);
minimumSelect.props.onChange({target:{value:'default'}});assert.deepEqual(preferenceChange,['minimum_video_minutes','default']);
const pauseToggle=subscriptionFields.find(n=>n.props.title==='Pause automatic downloads');
pauseToggle.props.onChange(true);assert.deepEqual(preferenceChange,['subscription_paused',true]);
const explicitMinimum={...inherited,downloads:{...inherited.downloads,minimum_override:1,minimum_video_minutes:0,subscription_paused:true}};
const explicitFields=nodes(fields.ChannelSettingsFields({channel:explicitMinimum,rule:(key,value)=>preferenceChange=[key,value],preference:()=>{}}));
const minimumInput=explicitFields.find(n=>n.props['aria-label']==='Channel minimum length (minutes)');
assert.equal(minimumInput.props.defaultValue,0);
minimumInput.props.onBlur({currentTarget:{value:'2',checkValidity:()=>true}});assert.deepEqual(preferenceChange,['minimum_video_minutes',2]);
assert.ok(bulk.mixedChannelFields([inherited,explicitMinimum]).has('minimum_video_minutes'));
assert.ok(bulk.mixedChannelFields([inherited,explicitMinimum]).has('subscription_paused'));
console.log('Subscription preferences: global/custom minimum, zero, autosave, pause and bulk modes passed.');
