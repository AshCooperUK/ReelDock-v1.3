const assert=require('node:assert/strict'),fs=require('node:fs'),vm=require('node:vm'),ts=require('typescript');
const React=require('react'),{renderToStaticMarkup}=require('react-dom/server'),mantine=require('@mantine/core');
const code=ts.transpileModule(fs.readFileSync(__dirname+'/src/components/SelectField.tsx','utf8'),{compilerOptions:{target:ts.ScriptTarget.ES2022,module:ts.ModuleKind.CommonJS,jsx:ts.JsxEmit.ReactJSX}}).outputText;
function load(react=React){const exports={};vm.runInNewContext(code,{exports,require:name=>name==='react'?react:require(name)});return exports;}
const actual=load(),h=React.createElement;
const normalized=actual.normalizeSelectData({data:[{value:'ignored',label:'Ignored'}],children:h(React.Fragment,null,
 h('option',{value:'',disabled:true,title:'Choose first'},'Choose a release'),
 [h('option',{key:'a',value:0},'Zero'),h(React.Fragment,{key:'f'},h('optgroup',{label:'Grouped',disabled:true},h('option',{value:'release-id','aria-label':'Complete release label'},'Full title ',h('span',null,'2160p'))))])});
assert.equal(JSON.stringify(normalized.map(item=>'group'in item?{group:item.group,values:item.items.map(row=>[row.value,row.label,row.disabled])}:[item.value,item.label,item.disabled])),JSON.stringify([['','Choose a release',true],['0','Zero',false],{group:'Grouped',values:[['release-id','Full title 2160p',true]]}]));
assert.equal(normalized[0].title,'Choose first');assert.equal(normalized[2].items[0].ariaLabel,'Complete release label');
assert.equal(actual.normalizeSelectData({data:['all',{group:'Formats',items:['720',{value:'1080',label:'Full HD',disabled:true}]}]})[1].items[1].disabled,true);
assert.equal(actual.normalizeSelectData({data:['same','same']}).length,1,'duplicate native values keep their first identity rather than crashing Mantine');
const themeExports={};vm.runInNewContext(ts.transpileModule(fs.readFileSync(__dirname+'/src/mantineTheme.ts','utf8'),{compilerOptions:{module:ts.ModuleKind.CommonJS}}).outputText,{exports:themeExports,require});
const render=node=>renderToStaticMarkup(h(mantine.MantineProvider,{forceColorScheme:'dark',theme:themeExports.mantineTheme},node));
for(const props of [{value:'b'},{defaultValue:'b'}]) {
 const html=render(h(actual.SelectField,{...props,id:'source',name:'source',title:'Choose source','aria-label':'Media source',required:true,data:[{value:'a',label:'Alpha'},{value:'b',label:'Beta'}]}));
 assert.doesNotMatch(html,/<select\b/,'the popup is never an OS native select');assert.match(html,/role="combobox"/);assert.match(html,/aria-label="Media source"/);assert.match(html,/title="Choose source"/);assert.match(html,/min-height:44px/);
 const hidden=html.match(/<input[^>]*type="hidden"[^>]*>/)[0];assert.match(hidden,/value="b"/);assert.match(hidden,/name="source"/,'the exact submitted ID is separate from the visible option label');
 assert.match(html,/<input[^>]*value="Beta"/);assert.match(html,/data-select-required-proxy=""/);
 const proxy=html.match(/<input[^>]*data-select-required-proxy=""[^>]*>/)[0];assert.match(proxy,/required=""/);assert.doesNotMatch(proxy,/name=/,'required validation must not add a second submitted field');
}
assert.match(render(h(actual.SelectField,{name:'n',defaultValue:0,data:[{value:'0',label:'Zero'}]})),/type="hidden" name="n" value="0"/);
assert.match(render(h(actual.SelectField,{name:'n',data:[{value:'no',label:'Unavailable',disabled:true},{value:'yes',label:'Available'}]})),/type="hidden" name="n" value="yes"/,'uncontrolled native selects initially choose the first enabled option');
assert.match(render(h(actual.SelectField,{name:'n',disabled:true,value:'a',data:['a']})),/type="hidden" disabled="" name="n" value="a"/);

// Inspect adapter callbacks while leaving the actual Mantine Select untouched.
let slots=[],index=0,imperative;
const fakeReact={...React,useState:initial=>{const i=index++;if(!(i in slots))slots[i]=typeof initial==='function'?initial():initial;return[slots[i],v=>slots[i]=typeof v==='function'?v(slots[i]):v];},useRef:initial=>{const i=index++;if(!(i in slots))slots[i]={current:initial};return slots[i];},useEffect(){},useMemo:f=>f(),useImperativeHandle:(_ref,f)=>imperative=f};
const fake=load(fakeReact),changed=[];
const draw=props=>{index=0;const tree=fake.SelectField.render(props,null);return{select:tree.props.children[0],proxy:tree.props.children[1]};};
let view=draw({name:'quality',required:true,value:'720',onChange:event=>changed.push(event),data:['720','1080']});
assert.equal(view.select.type,mantine.Select);assert.equal(view.select.props.allowDeselect,false);assert.equal(view.select.props.clearable,false);
view.select.props.onChange('720',{value:'720',label:'720'});assert.equal(changed.length,0,'reselecting the displayed value must not repeat a field write');
view.select.props.onChange('1080',{value:'1080',label:'1080'});
assert.equal(changed[0].target.value,'1080');assert.equal(changed[0].currentTarget.value,'1080');assert.equal(changed[0].target.name,'quality');assert.equal(view.select.props.value,'720','controlled selection stays caller-owned');
view.select.props.onChange('blocked',{value:'blocked',label:'Blocked',disabled:true});view.select.props.onChange(null,null);assert.equal(changed.length,1,'one changed selection emits one write');
view=draw({name:'quality',required:true,value:'720',onChange:event=>changed.push(event),data:['720','1080']});view.select.props.onChange('1080',{value:'1080',label:'1080'});assert.equal(changed.length,2,'a failed controlled change can be retried while the displayed value remains old');
let prevented=0,stopped=0;const escape=()=>({key:'Escape',currentTarget:{getAttribute:()=> 'false'},preventDefault:()=>prevented++,stopPropagation:()=>stopped++});
view.select.props.onDropdownOpen();view.select.props.onKeyDown(escape());assert.equal(prevented,1);assert.equal(stopped,1,'first Escape cannot bubble to the dialog');
view.select.props.onDropdownClose();view.select.props.onKeyDown(escape());assert.equal(prevented,1);assert.equal(stopped,1,'a second Escape is available to close the dialog');
let focused=0;view.select.props.ref.current={focus:()=>focused++};view.proxy.props.onInvalid({preventDefault(){}});assert.equal(focused,1);assert.equal(imperative(),view.select.props.ref.current,'forwarded ref resolves to the visible input');
slots=[];view=draw({defaultValue:'720',data:['720','1080']});view.select.props.onChange('1080',{value:'1080',label:'1080'});view=draw({defaultValue:'720',data:['720','1080']});assert.equal(view.select.props.value,'1080','uncontrolled selection persists');
slots=[];const missingProps={name:'source',value:'',required:true,data:[{value:'',label:'Choose source',disabled:true},{value:'valid',label:'Valid'}]};
let missing=draw(missingProps);missing.proxy.props.onInvalid({preventDefault(){}});missing=draw(missingProps);assert.equal(missing.select.props.error,'Choose an option.','required validation gives a visible standard Mantine error');
missing=draw({...missingProps,error:'Connection unavailable'});assert.equal(missing.select.props.error,'Connection unavailable','caller errors have priority');
missing.select.props.onChange('valid',{value:'valid',label:'Valid'});missing=draw({...missingProps,value:'valid'});assert.equal(missing.select.props.error,undefined,'valid selection clears the local required error');
const popup=view.select.props.comboboxProps;assert.equal(popup.withinPortal,true);assert.equal(popup.floatingStrategy,'fixed');assert.ok(popup.zIndex>200);assert.ok(popup.middlewares.flip&&popup.middlewares.shift&&popup.middlewares.size);assert.equal(popup.preventPositionChangeWhenVisible,false);
const sizing={setProperty(name,value){this[name]=value;}};popup.middlewares.size.apply({availableWidth:290,availableHeight:150,elements:{floating:{style:sizing}}});assert.equal(sizing.maxWidth,'290px');assert.equal(sizing.maxHeight,'150px');assert.equal(sizing['--rd-select-available-height'],'142px');
assert.match(view.select.props.maxDropdownHeight,/--rd-select-available-height/);assert.equal(view.select.props.scrollAreaProps.overscrollBehavior,'contain');
const openHtml=render(h(mantine.Select,{...view.select.props,dropdownOpened:true,comboboxProps:{...popup,withinPortal:false,transitionProps:{duration:0}},data:[{value:'720',label:'720'},{value:'1080',label:'Full release title',disabled:true}]}));
assert.match(openHtml,/role="listbox"/);assert.match(openHtml,/data-dialog-selector=""/);assert.match(openHtml,/role="option"[^>]*data-combobox-disabled="true"|data-combobox-disabled="true"[^>]*role="option"/);assert.match(openHtml,/Full release title/);
console.log('SelectField: real styled combobox/hidden values, options/groups/disabled, value callbacks, defaults, required gate/focus, Escape layering and viewport-aware portal contract passed.');
