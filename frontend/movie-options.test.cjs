const sharedSelect=require('./select-field-test-helper.cjs')();
const Mantine=require('@mantine/core');
const assert=require('node:assert/strict'),fs=require('node:fs'),vm=require('node:vm'),ts=require('typescript');
const React=require('react'),{renderToStaticMarkup}=require('react-dom/server');
function load(path,resolve){const out={};vm.runInNewContext(ts.transpileModule(fs.readFileSync(__dirname+'/src/'+path,'utf8'),{compilerOptions:{module:ts.ModuleKind.CommonJS,jsx:ts.JsxEmit.ReactJSX}}).outputText,{exports:out,require:name=>name.endsWith('/mediaDuration')?require('./media-duration-test-helper.cjs')():name==='react'?React:name==='@mantine/core'?Mantine:name==='../components/SelectField'?sharedSelect:name==='react/jsx-runtime'?require(name):resolve?.(name)||{}});return out;}
let toggles=[],selects=[],inputs=[],calls=[];
const fields=load('discovery/MovieOptions.tsx',name=>name==='../SettingsUI'?{
 Group:({title,children})=>React.createElement('section',{},React.createElement('h3',{},title),children),Row:({children})=>{if(children.type===sharedSelect.SelectField)selects.push(children.props);return children;},Toggle:props=>{toggles.push(props);return React.createElement('button',{'aria-label':props.title});}
}:name==='../TextField'?{TextInput:props=>{inputs.push(props);return React.createElement('input',{'aria-label':props['aria-label']});}}:{}).MovieSettingsFields;
const value={shown:false,dates:true,favourite:false,automatic:false,quality:'default',minimum:null,globalMinimum:0};
function render(){toggles=[];selects=[];inputs=[];return renderToStaticMarkup(React.createElement(Mantine.MantineProvider,{forceColorScheme:'dark'},React.createElement(fields,{value,save:(...args)=>calls.push(args)})));}
let html=render();assert.equal(toggles.map(t=>t.title).join('|'),'Show in guide|Release, premiere & source dates|Favourite|Automatic download');assert.equal(inputs.length,0);
for(const [n,field] of ['shown','dates','favourite','automatic'].entries()){toggles[n].onChange(true);assert.equal(calls.at(-1)[0],field);}
selects.find(s=>s['aria-label']==='Movie quality').onChange({target:{value:'720'}});assert.equal(calls.at(-1).join('|'),'quality|720');
selects.find(s=>s['aria-label']==='Movie minimum length').onChange({target:{value:'custom'}});assert.equal(calls.at(-1).join('|'),'minimum|0','zero is a real custom minimum');
value.minimum=90;render();assert.equal(inputs.length,1);let valid=true,reported=false;
const blur=minutes=>inputs[0].onBlur({currentTarget:{value:minutes,checkValidity:()=>valid,reportValidity:()=>reported=true}});
blur('91');assert.equal(calls.at(-1).join('|'),'minimum|91');const count=calls.length;valid=false;blur('-1');assert.equal(calls.length,count);assert.equal(reported,true);
selects.find(s=>s['aria-label']==='Movie minimum length').onChange({target:{value:'default'}});assert.equal(calls.at(-1)[0],'minimum');assert.equal(calls.at(-1)[1],null);
const guide=load('guideData.ts');const movie={id:'movie:1234567',kind:'movie',title:'A movie',year:'2026'};
const rows=guide.mapGuide({channels:[],uploads:[],forecasts:[],truncated:false,movies:[{key:movie.id,item:movie,favourite:true,events:[{id:'release',date:'2026-10-17',label:'Release date',note:'Date only; time not announced'},{id:'found',date:'2026-10-16',label:'Found in sources',note:'NZB'}]}]},'2026-10-12','2026-10-18');
assert.equal(rows.length,1);assert.equal(rows[0].movie.id,movie.id);assert.equal(rows[0].favourite,true);assert.equal(rows[0].programmes.length,2);assert.ok(rows[0].programmes.every(p=>p.allDay&&!p.videoId),'movie dates never masquerade as timed YouTube uploads');
console.log('Movie options: shared switches, inherited quality/minimum, blur validation and date-only movie Guide mapping passed.');
