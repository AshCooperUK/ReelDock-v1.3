const assert=require('node:assert/strict'),fs=require('node:fs'),path=require('node:path'),vm=require('node:vm'),ts=require('typescript');
const React=require('react'),runtime=require('react/jsx-runtime'),Mantine=require('@mantine/core'),{renderToStaticMarkup}=require('react-dom/server');
function load(file,imports={},globals={}){const output={};vm.runInNewContext(ts.transpileModule(fs.readFileSync(path.join(__dirname,'src',file),'utf8'),{compilerOptions:{module:ts.ModuleKind.CommonJS,jsx:ts.JsxEmit.ReactJSX}}).outputText,{exports:output,...globals,require:name=>name==='react/jsx-runtime'?runtime:imports[name]||(name==='react'?React:require(name))});return output;}
function hooks(){let index=0,values=[];return{start(){index=0;},react:{...React,useState(initial){const at=index++;if(!(at in values))values[at]=typeof initial==='function'?initial():initial;return[values[at],next=>{values[at]=typeof next==='function'?next(values[at]):next;}];},useRef(initial){const at=index++;return values[at]??={current:initial};},useEffect(){}}};}
const nodes=node=>!node||typeof node!=='object'?[]:Array.isArray(node)?node.flatMap(nodes):[node,...nodes(node.props?.children)];
const find=(node,predicate)=>nodes(node).find(predicate),byText=(node,text)=>find(node,item=>item.props.children===text);
const tick=()=>new Promise(resolve=>setImmediate(resolve));
const primitive={Field:'Field',Group:'SettingsGroup',Row:'Row',Toggle:'Toggle',Notice:'Notice',Loading:'Loading',Disclosure:'Disclosure'};
const connectionForm=load('settings/media-players/ConnectionPanel.tsx',{'@mantine/core':Mantine,'../../components/layout':{Panel:'Panel'},'../../components/ui':{Button:'Button'},'../../SettingsUI':primitive,'../../TextField':{TextInput:'TextInput'}});
function expandConnection(node){if(Array.isArray(node))return node.map(expandConnection);if(!React.isValidElement(node))return node;if(node.type===connectionForm.ConnectionPanel)return expandConnection(node.type(node.props));return React.cloneElement(node,{},expandConnection(node.props.children));}
async function main(){
 // Saved Google credentials expose one stage at a time. Cancelling editing
 // makes no request; native named fields keep blank-secret preservation.
 const authHooks=hooks(),calls=[];let saveSucceeds=true;
 const saved={admin:true,origin:'https://fixture.example',csrf:'fixture-csrf',storage:{ready:true},use_google_api:true,import_method:'google',source_ready:false,youtube_connected:false,client_id_present:true,client_secret_present:true,id_from_yaml:false,secret_from_yaml:false,checks:[]};
 const Auth=load('AuthApp.tsx',{'react':authHooks.react,'@mantine/core':Mantine,'@mantine/hooks':{useMediaQuery:()=>false},'./components/SelectField':{SelectField:'SelectField'},'./notifications':{notify(){}}},{
  FormData:class{constructor(form){this.values=form.values;}entries(){return Object.entries(this.values)[Symbol.iterator]();}},
  fetch:async(url,options)=>{calls.push({url,options});if(url==='/api/setup/state')return{ok:true,json:async()=>saved};return{ok:saveSucceeds,json:async()=>saveSucceeds?{}:{error:'Fixture rejected credentials'}};},
 }).default;
 const renderAuth=()=>{authHooks.start();return Auth({bootstrap:{page:'setup',version:'fixture',initial:saved}});};
 let tree=renderAuth();assert.ok(byText(tree,'Connect YouTube'));let edit=byText(tree,'Edit saved connection details');assert.equal(edit.props['aria-expanded'],false);
 edit.props.onClick();tree=renderAuth();assert.ok(byText(tree,'Save connection details'));assert.ok(!byText(tree,'Connect YouTube'),'connection cannot compete with unsaved editing');
 const cancel=byText(tree,'Cancel editing');assert.equal(cancel.props.type,'button');assert.equal(cancel.props['aria-expanded'],true);cancel.props.onClick();tree=renderAuth();assert.ok(byText(tree,'Connect YouTube'));assert.equal(calls.length,0,'cancel does not touch saved credentials');
 byText(tree,'Edit saved connection details').props.onClick();tree=renderAuth();
 let form=find(tree,node=>node.props.id==='google-credentials');assert.ok(form);
 const inputs=nodes(form).filter(node=>node.props.name);assert.deepEqual(inputs.map(node=>node.props.name),['client_id','client_secret']);assert.ok(inputs.every(node=>node.props.value===undefined),'saved private credentials are never populated');assert.equal(inputs[0].props.autoFocus,true,'editing focuses the first editable credential');
 saveSucceeds=false;form.props.onSubmit({preventDefault(){},currentTarget:{values:{client_id:'replacement.apps.googleusercontent.com',client_secret:''}}});await tick();tree=renderAuth();assert.ok(byText(tree,'Cancel editing'));assert.ok(!byText(tree,'Connect YouTube'),'failed edits stay in the editing stage');
 saveSucceeds=true;find(tree,node=>node.props.id==='google-credentials').props.onSubmit({preventDefault(){},currentTarget:{values:{client_id:'replacement.apps.googleusercontent.com',client_secret:''}}});await tick();tree=renderAuth();
 assert.ok(byText(tree,'Connect YouTube'));assert.ok(!byText(tree,'Save connection details'));
 const submitted=calls.filter(call=>call.url==='/api/setup/google');assert.equal(submitted.length,2);assert.deepEqual(JSON.parse(submitted[1].options.body),{client_id:'replacement.apps.googleusercontent.com',client_secret:''});assert.equal(submitted[1].options.headers['X-CSRF-Token'],'fixture-csrf');
 assert.equal(calls.at(-1).url,'/api/setup/state','Connect returns only after the server confirms saved state');

 // Emby credentials submit separately; automatic preferences still send the
 // same scoped payload and cannot overwrite an unsaved connection address.
 const embyHooks=hooks(),embyCalls=[];let busy=false,response=null;
 let data={url:'http://saved.fixture',user:'one',root:'/fixture',key_saved:true,auto_refresh:false,watched:true,favourites:false,status:'Connected',refresh_status:'Idle',favourites_status:'Ready',users:[{id:'one',name:'One'},{id:'two',name:'Two'}]};
 const resource={get data(){return data;},setData(next){data=typeof next==='function'?next(data):next;},reload(){}};
 const Emby=load('settings/media-players/EmbySettings.tsx',{'react':embyHooks.react,'@mantine/core':Mantine,'./ConnectionPanel':connectionForm,'../../components/SelectField':{SelectField:'SelectField'},'../../TextField':{TextInput:'TextInput'},'../../components/ui':{Button:'Button'},'../../SettingsUI':{...primitive,useSettings:()=>resource,useAction:token=>{assert.equal(token,'fixture-csrf');return{busy,run:async(url,body)=>{embyCalls.push({url,body});return response;}};}}}).default;
 const renderEmby=()=>{embyHooks.start();return expandConnection(Emby({csrf:'fixture-csrf'}));};
 tree=renderEmby();form=find(tree,node=>node.type==='form');assert.ok(!nodes(form).some(node=>node.type==='Toggle'||node.type==='SelectField'),'automatic preferences are outside the Connect form');assert.ok(nodes(tree).some(node=>String(node.props.children).startsWith('Changes below save automatically.')));
 find(form,node=>node.props.type==='url').props.onChange({target:{value:'http://draft.fixture'}});find(form,node=>node.props.type==='password').props.onChange({target:{value:'replacement-key'}});assert.equal(embyCalls.length,0,'editing credentials never autosaves');
 tree=renderEmby();response={...data,url:'http://saved.fixture',user:'two'};
 find(tree,node=>node.type==='SelectField').props.onChange({target:{value:'two'}});await tick();assert.equal(data.url,'http://draft.fixture');assert.equal(data.user,'two');assert.deepEqual(JSON.parse(JSON.stringify(embyCalls[0])),{url:'/api/emby',body:{action:'preferences',preferences:{user:'two'}}});
 tree=renderEmby();response={...data,favourites:true};find(tree,node=>node.props.title==='Sync favourites').props.onChange(true);await tick();assert.equal(data.favourites,true);
 assert.deepEqual(JSON.parse(JSON.stringify(embyCalls[1].body)),{action:'preferences',preferences:{favourites:true}});
 tree=renderEmby();form=find(tree,node=>node.type==='form');assert.equal(byText(form,'Connect').props.type,'submit');response=null;form.props.onSubmit({preventDefault(){}});await tick();tree=renderEmby();assert.equal(find(tree,node=>node.props.type==='password').props.value,'replacement-key','failed saves retain the new key for retry');
 assert.deepEqual(JSON.parse(JSON.stringify(embyCalls.at(-1).body)),{action:'save',url:'http://draft.fixture',user:'two',root:'/fixture',auto_refresh:false,watched:true,favourites:true,key:'replacement-key'});
 response={...data};find(tree,node=>node.type==='form').props.onSubmit({preventDefault(){}});await tick();tree=renderEmby();assert.equal(find(tree,node=>node.props.type==='password').props.value,'','confirmed save clears the new key');
 busy=true;tree=renderEmby();assert.equal(byText(tree,'Connect').props.disabled,true);assert.ok(nodes(tree).filter(node=>node.type==='Toggle').every(node=>node.props.disabled));
 busy=false;data={...data,key_saved:false,users:[]};tree=renderEmby();assert.ok(!find(tree,node=>node.type==='SelectField'),'no singleton empty user selector');assert.ok(nodes(tree).filter(node=>node.type==='Toggle').every(node=>node.props.disabled));

 // Expanded advanced networking has exactly one inline feedback location.
 const Torrent=load('settings/torrent/TorrentSettings.tsx',{'@mantine/core':Mantine,'../../components/SelectField':{SelectField:'SelectField'},'../../TextField':{TextInput:'TextInput'},'../../SettingsUI':primitive,'../useEngineSettings':{useEngineSettings:()=>({values:{download_limit_bps:0,max_active_downloads:3,listen_port:6881,encryption:'prefer'},busy:false,notice:'Unique save confirmation',save(){}})}}).default;
 tree=Torrent({csrf:'fixture'});assert.equal(nodes(tree).filter(node=>node.type==='Notice'&&node.props.text==='Unique save confirmation').length,1);

 // DropZone forwards the actual FileInput label/id/name/description and leaves
 // file validation/content reads to the caller. Drag, select, clear and disabled
 // guards share one callback contract.
 const dropHooks=hooks(),files=[];
 const Drop=load('components/DropZone.tsx',{'react':dropHooks.react,'@mantine/core':{Box:'Box',FileInput:'FileInput'}}).default;
 const file={name:'fixture.torrent',size:1200},dropProps={label:'Torrent file',id:'fixture-file',name:'torrent',description:'At most 2 MB.','aria-label':'Choose torrent',accept:'.torrent',file:null,onFile:value=>files.push(value)};
 const drawDrop=extra=>{dropHooks.start();return Drop({...dropProps,...extra});};
 tree=drawDrop();let picker=find(tree,node=>node.type==='FileInput');assert.equal(picker.props.id,'fixture-file');assert.equal(picker.props.name,'torrent');assert.equal(picker.props.label,'Torrent file');assert.equal(picker.props.description,'At most 2 MB.');assert.equal(picker.props['aria-label'],'Choose torrent');
 let resets=0;picker.props.resetRef.current=()=>resets++;picker.props.onChange(file);picker.props.onChange(null);assert.deepEqual(files,[file,null]);assert.equal(resets,2);
 tree.props.onDrop({preventDefault(){},stopPropagation(){},dataTransfer:{files:[file,{name:'ignored'}]}});assert.equal(files.at(-1),file);
 for(const restriction of [{disabled:true},{readOnly:true}]){const before=files.length;tree=drawDrop(restriction);find(tree,node=>node.type==='FileInput').props.onChange(file);tree.props.onDrop({preventDefault(){},stopPropagation(){},dataTransfer:{files:[file]}});assert.equal(files.length,before,'disabled/read-only selection and drop never mutate caller state');}
 const RealDrop=load('components/DropZone.tsx',{'@mantine/core':Mantine}).default;
 const html=renderToStaticMarkup(React.createElement(Mantine.MantineProvider,{forceColorScheme:'dark'},React.createElement(RealDrop,{...dropProps,form:'fixture-form'})));
 assert.match(html,/<label[^>]*for="fixture-file"[^>]*>Torrent file<\/label>/);assert.match(html,/<input[^>]*type="file"[^>]*name="torrent"/);assert.match(html,/form="fixture-form"/);
 assert.equal((html.match(/>At most 2 MB\.</g)||[]).length,1);assert.ok(html.indexOf('At most 2 MB.')>html.indexOf('id="fixture-file"'),'one helper follows the labelled picker');
 const selectedHtml=renderToStaticMarkup(React.createElement(Mantine.MantineProvider,{forceColorScheme:'dark'},React.createElement(RealDrop,{...dropProps,file})));
 assert.match(selectedHtml,/At most 2 MB\./);assert.match(selectedHtml,/1 KB/);assert.match(selectedHtml,/choose or drop another file to replace it/,'custom size-limit guidance does not hide the selected-file size/replacement hint');
 console.log('Form UX: staged Google edits/native fields, separate Emby credentials and automatic preferences, single Torrent feedback, labelled FileInput and selection guards passed.');
}
main().catch(error=>{console.error(error);process.exitCode=1;});
