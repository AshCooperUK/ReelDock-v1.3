const assert=require('node:assert/strict'),fs=require('node:fs'),vm=require('node:vm'),ts=require('typescript');
const React=require('react'),M=require('@mantine/core'),{renderToStaticMarkup}=require('react-dom/server');
const select=require('./select-field-test-helper.cjs')(),{FilterBar}=require('./media-browse-test-helper.cjs')();
function load(file,imports={}){const out={};vm.runInNewContext(ts.transpileModule(fs.readFileSync(__dirname+'/src/'+file,'utf8'),{compilerOptions:{module:ts.ModuleKind.CommonJS,jsx:ts.JsxEmit.ReactJSX}}).outputText,{exports:out,Intl,Date,require:name=>imports[name]||require(name)});return out;}
const ui=load('components/ui.tsx',{'../ScrollToTop':{default:()=>null},'../notifications-state':{registerNotificationHost:()=>()=>{}}}),theme=load('mantineTheme.ts').mantineTheme;
const generic=new Proxy({},{get:(_,key)=>key==='__esModule'?false:()=>null});
const nodes=node=>node==null?[]:Array.isArray(node)?node.flatMap(nodes):typeof node==='object'?[node,...nodes(node.props?.children)]:[];
function channelSelector(status='',error=''){
 let state=0;const out={};vm.runInNewContext(ts.transpileModule(fs.readFileSync(__dirname+'/src/ChannelViews.tsx','utf8'),{compilerOptions:{module:ts.ModuleKind.CommonJS,jsx:ts.JsxEmit.ReactJSX}}).outputText,{exports:out,Intl,Date,require:name=>name==='react'?{...React,useEffect(){},useRef:value=>({current:value}),useState:value=>[[true,false,status,error][state++],()=>{}]}:name==='react/jsx-runtime'||name==='@mantine/core'||name==='lucide-react'?require(name):name==='./components/SelectField'?select:name==='./components/ui'?ui:name==='./notifications'?{NotificationMessage:({message})=>React.createElement(M.Alert,{role:'alert'},message)}:generic});
 return out.ChannelViewSelector({csrf:'fixture',view:'compact',onChange(){}});
}
for(const status of ['', 'Saving...'])for(const scheme of ['light','dark']){
 const selector=channelSelector(status),statusNodes=nodes(selector).filter(node=>node.props?.role==='status');
 assert.equal(statusNodes.length,status?1:0,'an empty status must not create a second flex child/gap');assert.equal(selector.props.mih,44);
 const field=nodes(selector).find(node=>node.type===select.SelectField);assert.equal(field.props['aria-label'],'Channel view');assert.equal(field.props.label,undefined,'the view selector has no adjacent visible label');
 const toolbar=FilterBar({sorts:[{value:'favourites',label:'Favourites first'}],sort:'favourites',onSort(){},sortLabel:'Sort channels',extra:React.createElement(React.Fragment,null,React.createElement(ui.Switch,{label:'Favourites only',showLabel:true,checked:false,onCheckedChange(){}}),selector)});
 assert.equal(toolbar.props.align,'flex-start','feedback below a field cannot vertically offset another44px control');assert.equal(toolbar.props.mih,44);
 const html=renderToStaticMarkup(React.createElement(M.MantineProvider,{theme,forceColorScheme:scheme},toolbar));
 assert.equal((html.match(/role="combobox"/g)||[]).length,2);assert.match(html,/role="switch"[^>]*aria-label="Favourites only"|aria-label="Favourites only"[^>]*role="switch"/);assert.match(html,/--group-align:flex-start/);assert.match(html,/min-height:44px/);assert.match(html,/data-channel-view-control/);assert.match(html,/data-filter-sort/);
 assert.equal((html.match(/role="status"/g)||[]).length,status?1:0,'SSR creates no empty live status box');assert.doesNotMatch(html,/<label[^>]*>Channel view|<label[^>]*>Sort channels/);
}
const failed=channelSelector('', 'Could not save the selected view.');assert.ok(nodes(failed).some(node=>node.props?.message==='Could not save the selected view.'),'real save failure feedback remains available below its own control');
console.log('Filter controls: real44px switch/selects share a top-aligned row, empty feedback adds no gap, saving/errors retain one live message and accessible unlabelled selects.');
