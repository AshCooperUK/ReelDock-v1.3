const assert=require('node:assert/strict'),fs=require('node:fs'),path=require('node:path'),vm=require('node:vm'),ts=require('typescript');
const React=require('react'),runtime=require('react/jsx-runtime'),Mantine=require('@mantine/core');
const {renderToStaticMarkup}=require('react-dom/server');
function load(file,imports={}){
 const output={};
 vm.runInNewContext(ts.transpileModule(fs.readFileSync(path.join(__dirname,'src',file),'utf8'),{compilerOptions:{module:ts.ModuleKind.CommonJS,jsx:ts.JsxEmit.ReactJSX}}).outputText,{
  exports:output,require:name=>name.endsWith('.module.css')?{__esModule:true,default:{surface:'surface',tile:'tile',preview:'preview',bannerInformation:'bannerInformation'}}:name.endsWith('/mediaDuration')?require('./media-duration-test-helper.cjs')():name==='react/jsx-runtime'?runtime:imports[name]||(name==='react'?React:require(name)),
 });return output;
}
function hooks(){
 let cursor=0,values=[],effects=[],changed=false;
 const react={...React,useState(initial){const at=cursor++;if(!(at in values))values[at]=typeof initial==='function'?initial():initial;return[values[at],next=>{const value=typeof next==='function'?next(values[at]):next;if(!Object.is(values[at],value)){values[at]=value;changed=true;}}];},useEffect(effect,deps){const at=cursor++,before=values[at];if(!before||deps.some((value,index)=>!Object.is(value,before[index])))effects.push(effect);values[at]=deps;}};
 return{react,render(fn){let result;do{changed=false;cursor=0;effects=[];result=fn();for(const effect of effects)effect();}while(changed);return result;}};
}
const element=(tag,props)=>React.createElement(tag,props,props.children);
const management={default:props=>React.createElement(Mantine.Text,{'data-manage':props.manageKey},'Managed item'),MediaTitleHistory:props=>React.createElement(Mantine.Text,{'data-history':props.item.id},'History')};
const baseImports={
 './QuickDownload':{default:'QuickDownload'},'./discoverPayload':{acquisitionItem:load('discoverPayload.ts').acquisitionItem},
 './useChannelBanner':{useChannelBanner:()=>undefined},
 '@mantine/core':Mantine,'./components/DetailSheet':{DetailSheet:'DetailSheet'},'./components/ui':{Button:Mantine.Button},
 './MediaManagement':management,'./components/browse':{MediaArtwork:()=>null,MediaLengthBadge:require('./media-browse-test-helper.cjs')().MediaLengthBadge},'./ChannelIdentity':{default:()=>null},
};
const state=hooks(),Video=load('VideoSheet.tsx',{...baseImports,react:state.react}).default;
const facts=React.createElement(Mantine.Text,{'data-information':'programme'},'Programme date and duration');
const statusProps={readOnly:true,canDownload:false,'data-download-status':'fixture'};
const downloads=React.createElement('section',statusProps,'Existing status only');
const closeCalls=[],backCalls=[];
let props={csrf:'fixture-token',open:true,onOpenChange:value=>closeCalls.push(value),video:{id:'abcdefghijk',title:'Fixture video',image:'/fixture.svg',channel:'Exact channel',channelId:'UC'+'a'.repeat(22),avatar:'/avatar.svg'},content:facts,description:'About the fixture video',downloads,back:{label:'channel',onBack:()=>backCalls.push(true)}};
const draw=()=>state.render(()=>Video(props));
let sheet=draw();
assert.equal(sheet.props.subtitle,'YouTube');assert.equal(sheet.props.channel.props.id,props.video.channelId);assert.equal(sheet.props.channel.props.name,props.video.channel);assert.equal(sheet.props.channel.props.avatar,props.video.avatar,'channel identity is supplied separately from type text');
assert.deepEqual(Array.from(sheet.props.tabs,tab=>[tab.id,tab.label]),[['overview','Overview'],['downloads','Downloads'],['manage','Manage'],['history','History']]);
const markup=node=>renderToStaticMarkup(React.createElement(Mantine.MantineProvider,{forceColorScheme:'dark'},node));
assert.match(markup(sheet.props.tabs[0].content),/Programme date and duration/);
assert.match(markup(sheet.props.tabs[0].content),/About the fixture video/);
assert.doesNotMatch(markup(sheet.props.tabs[0].content),/data-download-status/,'information remains separate from download controls');
assert.equal(sheet.props.tabs[1].content,downloads,'caller status/download nodes are not cloned or granted permissions');
assert.equal(sheet.props.tabs[1].content.props.canDownload,false);
assert.equal(sheet.props.tabs[2].content.props.manageKey,'video:abcdefghijk');
assert.equal(sheet.props.tabs[3].content.props.item.id,'video:abcdefghijk');
sheet.props.onOpenChange(false);sheet.props.back.onBack();assert.deepEqual(closeCalls,[false]);assert.deepEqual(backCalls,[true]);
sheet.props.tabs[2].content.props.onOpenTab('history',{key:'video:abcdefghijk'});sheet=draw();
assert.equal(sheet.props.tab,'history');assert.equal(sheet.props.tabs[3].content.props.focus,'video:abcdefghijk','history uses the exact managed item key');
sheet.props.onTab('downloads');sheet=draw();assert.equal(sheet.props.tab,'downloads');
props={...props,video:{...props.video,id:'BBBBBBBBBBB'}};sheet=draw();
assert.equal(sheet.props.tab,'overview','changing video resets the active pane');
assert.equal(sheet.props.tabs[3].content.props.focus,'','history selection cannot leak across identities');
assert.equal(sheet.props.tabs[2].content.props.manageKey,'video:BBBBBBBBBBB');
sheet.props.onTab('history');draw();props={...props,open:false};sheet=draw();props={...props,open:true};sheet=draw();assert.equal(sheet.props.tab,'overview','reopening starts with information');
for(const restriction of [{readOnly:true},{canManage:false},{csrf:''},{video:{id:'',title:'Not announced'}}]){
 const restricted=hooks(),Component=load('VideoSheet.tsx',{...baseImports,react:restricted.react}).default;
 const node=restricted.render(()=>Component({...props,...restriction}));
 assert.deepEqual(Array.from(node.props.tabs,tab=>tab.label),['Overview','Downloads'],'restricted/no-id sheets expose no management or history');
 assert.equal(node.props.tabs[1].content,downloads,'read-only and status-only controls stay unchanged');
}
const missing=hooks(),NoControls=load('VideoSheet.tsx',{...baseImports,react:missing.react}).default;
const noControls=missing.render(()=>NoControls({...props,downloads:undefined,readOnly:true}));
assert.equal(noControls.props.tabs,undefined,'missing download controls do not create an empty actionable pane');
assert.match(markup(noControls.props.children),/Programme date and duration/);
// Quick acquisition inherits the exact permission-bearing download node.
const quickNode=value=>value.props.actions?.type==='QuickDownload'?value.props.actions:undefined;
const authorised=React.createElement('section',{id:props.video.id,canDownload:true,csrf:props.csrf},'Download controls');
const quickHooks=hooks(),WithQuick=load('VideoSheet.tsx',{...baseImports,react:quickHooks.react}).default;
let quickProps={...props,downloads:authorised},quickSheet=quickHooks.render(()=>WithQuick(quickProps));
assert.equal(quickNode(quickSheet).props.item.videoId,props.video.id);
assert.equal(quickNode(quickSheet).props.csrf,props.csrf);
quickNode(quickSheet).props.onChoices();quickSheet=quickHooks.render(()=>WithQuick(quickProps));
assert.equal(quickSheet.props.tab,'downloads','a required choice remains inside this one sheet');
for(const downloadProps of [{id:props.video.id,canDownload:false,csrf:props.csrf},{id:'another-video',canDownload:true,csrf:props.csrf},{canDownload:true},{canDownload:true,csrf:'different'},{canDownload:true,csrf:props.csrf,readOnly:true},{canDownload:true,csrf:props.csrf,localOnly:true}]){
 quickProps={...props,downloads:React.createElement('section',{id:props.video.id,...downloadProps})};
 quickSheet=quickHooks.render(()=>WithQuick(quickProps));assert.equal(quickNode(quickSheet),undefined,'header cannot escalate its supplied download permissions');
}
quickProps={...props,readOnly:true,downloads:authorised};quickSheet=quickHooks.render(()=>WithQuick(quickProps));assert.equal(quickNode(quickSheet),undefined);

// Render the actual shared responsive DetailSheet with stock Mantine controls;
// the overlay wrapper is inert in SSR so no browser, service or request is used.
let width=1440;
const ui={Dialog:({children})=>children,DialogContent:({children})=>React.createElement('section',{role:'dialog'},children),DialogTitle:props=>React.createElement(Mantine.Title,{order:2},props.children),DialogDescription:props=>React.createElement(Mantine.Text,props,props.children),Button:Mantine.Button};
const Detail=load('components/DetailSheet.tsx',{'@mantine/core':Mantine,'@mantine/hooks':{useMediaQuery:()=>width<768},'./SelectField':require('./select-field-test-helper.cjs')(),'./ui':ui,'../channelNavigation':{channelEvent:'fixture-channel'}}).DetailSheet;
for(width of [320,768,1440]){
 const html=markup(React.createElement(Detail,{...sheet.props,tab:'downloads'}));
 assert.equal((html.match(/role="dialog"/g)||[]).length,1,'one detail surface at '+width);
 assert.match(html,/Existing status only/);assert.doesNotMatch(html,/Programme date and duration/,'Downloads does not duplicate Overview content');
 if(width<768)assert.match(html,/role="combobox"[^>]*aria-label="Details sections"|aria-label="Details sections"[^>]*role="combobox"/);
 else for(const name of ['Overview','Downloads','Manage','History'])assert.match(html,new RegExp('role="tab"[^>]*>[\\s\\S]*?'+name));
}
console.log('Video details: common responsive sections, information/download separation, exact identity history, restrictions, status-only controls, close/back and reset behavior passed.');
