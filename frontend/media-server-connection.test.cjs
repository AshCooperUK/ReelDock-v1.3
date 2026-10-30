const assert=require('node:assert/strict'),fs=require('node:fs'),path=require('node:path'),vm=require('node:vm'),ts=require('typescript');
const React=require('react'),runtime=require('react/jsx-runtime'),Mantine=require('@mantine/core'),{renderToStaticMarkup}=require('react-dom/server');
function load(file,imports={}){const result={};vm.runInNewContext(ts.transpileModule(fs.readFileSync(path.join(__dirname,'src',file),'utf8'),{compilerOptions:{module:ts.ModuleKind.CommonJS,jsx:ts.JsxEmit.ReactJSX}}).outputText,{exports:result,require:name=>name==='react/jsx-runtime'?runtime:imports[name]||(name==='react'?React:require(name))});return result;}
const nodes=node=>!node||typeof node!=='object'?[]:Array.isArray(node)?node.flatMap(nodes):[node,...nodes(node.props?.children)],find=(node,predicate)=>nodes(node).find(predicate);
const layout=load('components/layout.tsx'),fields=load('TextField.tsx'),theme=load('mantineTheme.ts').mantineTheme;
const ui=load('components/ui.tsx',{'../ScrollToTop':{default:()=>null},'../notifications-state':{registerNotificationHost:()=>()=>{}}});
const settings=load('SettingsUI.tsx',{'./api':{},'./components/ui':ui,'./components/layout':layout,'./notifications':{}});
const shared=load('settings/media-players/ConnectionPanel.tsx',{'../../components/layout':layout,'../../components/ui':ui,'../../SettingsUI':settings,'../../TextField':fields});
const select=require('./select-field-test-helper.cjs')();
const base={url:'http://server.fixture:8096',username:'',user:'',library:'',connected:false,secret_saved:false,key_saved:false,name:'Fixture',users:[],libraries:[],status:'Not connected.',last_sync:'',items:0,watched:false,favourites:false,auto_refresh:false,root:'/fixture',refresh_status:'Idle',favourites_status:'Ready'};
const values={jellyfin_sync:false,jellyfin_auto_scan:false,jellyfin_minutes:15,plex_sync:false,plex_auto_scan:false,plex_minutes:15,kodi_sync:false,kodi_auto_scan:false,kodi_minutes:15};
let viewportWidth=1440;
const render=node=>renderToStaticMarkup(React.createElement(Mantine.MantineProvider,{theme,forceColorScheme:'dark'},React.createElement('div',{style:{width:viewportWidth,maxWidth:'100%'}},node)));
function serverModule(player,data=base){return load(player==='emby'?'settings/media-players/EmbySettings.tsx':'settings/media-players/ServerSettings.tsx',{
 './ConnectionPanel':shared,'../../components/SelectField':select,'../../components/ui':ui,
 '../../SettingsUI':{...settings,useSettings:()=>({data,setData(){},reload(){}}),useAction:()=>({busy:false,error:'',notice:'',run(){throw Error('SSR cannot issue requests');}})},
 '../useEngineSettings':{useEngineSettings:()=>({values,busy:false,reload(){},save(){throw Error('SSR cannot save preferences');}})},
}).default;}
const labels={emby:'Emby',jellyfin:'Jellyfin',plex:'Plex',kodi:'Kodi'};
function normalizeForm(html){return html.replace(/\b(id|for|aria-describedby|aria-labelledby)="[^"]*"/g,(_,key)=>key+'="FIELD-ID"').replace(/Plex token|API key/g,'Credential').replace(/Emby|Jellyfin|Plex/g,'Server').replace(/name="key"/g,'name="secret"');}
for(const width of [320,768,1440]){
 viewportWidth=width;
 let reference;
 for(const player of Object.keys(labels)){
  const Component=serverModule(player),html=render(React.createElement(Component,{player,csrf:'fixture'}));
  const section=html.match(/<section\b[^>]*data-server-connection="[^"]+"[^>]*>[\s\S]*?<\/section>/)?.[0];assert.ok(section,player+' uses the shared connection section at '+width);
  assert.equal((section.match(/data-connection-status=""/g)||[]).length,1);assert.equal((section.match(/Not connected/g)||[]).length,1,'connection state is written once for '+player);
  assert.equal((section.match(/<form\b/g)||[]).length,1);assert.equal((section.match(/<fieldset\b/g)||[]).length,1);assert.equal((section.match(/type="submit"/g)||[]).length,1);
  assert.match(section,/mantine-Paper-root/);assert.match(section,/mantine-Badge-root/);
  assert.match(section,/>Connection<\/h2>/,'the common heading stays the same width for every server');
  const inputTags=section.match(/<input\b[^>]*>/g)||[];assert.deepEqual(inputTags.map(tag=>/name="([^"]+)"/.exec(tag)?.[1]),player==='emby'?['url','key']:player==='kodi'?['url','username','secret']:['url','secret']);
  assert.match(inputTags[0],/type="url"/);assert.match(inputTags.at(-1),/type="password"/);assert.match(inputTags.at(-1),/value=""/);
  assert.ok(section.indexOf('Enter the address reachable')>section.indexOf(inputTags[0]));assert.ok(section.indexOf('Stored privately on this server')>section.indexOf(inputTags.at(-1)),'credential help always follows its input');
  assert.ok(inputTags.every(tag=>tag.includes('min-height:44px')),'all connection controls share the actual 44px theme');
  const form=section.match(/<form\b[\s\S]*?<\/form>/)[0];
  if(player==='emby')reference=normalizeForm(form);else if(player!=='kodi')assert.equal(normalizeForm(form),reference,player+' has the same actual field/action markup and spacing as Emby at '+width);
  if(player==='kodi')assert.ok(section.indexOf('name="username"')>section.indexOf('name="url"')&&section.indexOf('name="username"')<section.indexOf('name="secret"'),'Kodi adds only its required username field within the same form');
 }
}
for(const player of Object.keys(labels)){
 const Component=serverModule(player,{...base,connected:true,secret_saved:true,key_saved:true,status:'Could not reach this server.'});
 const html=render(React.createElement(Component,{player,csrf:'fixture'})),section=html.match(/<section\b[^>]*data-server-connection="[^"]+"[^>]*>[\s\S]*?<\/section>/)[0];
 assert.equal((section.match(/Could not reach this server\./g)||[]).length,1,'a meaningful connection result is retained once');assert.match(section,/Saved credential/);assert.doesNotMatch(section,/value="(?:replacement|saved-secret)"/);
}
// The shared native form delegates once, keeps Connect/Disconnect distinct,
// and disables mutation while busy without changing the caller's payload.
let connects=0,disconnects=0;const changes=[];
let panel=shared.ConnectionPanel({server:'Fixture',configured:true,status:'Configured.',url:'http://fixture',onUrl:value=>changes.push(['url',value]),credential:'',onCredential:value=>changes.push(['secret',value]),credentialSaved:true,onConnect:()=>connects++,onDisconnect:()=>disconnects++});
let form=find(panel,node=>node.type==='form');form.props.onSubmit({preventDefault(){}});assert.equal(connects,1);
find(panel,node=>node.props.name==='url').props.onChange({target:{value:'http://changed'}});find(panel,node=>node.props.name==='secret').props.onChange({target:{value:'fixture-secret'}});assert.deepEqual(changes,[['url','http://changed'],['secret','fixture-secret']]);
find(panel,node=>node.props.children==='Disconnect').props.onClick();assert.equal(disconnects,1);
panel=shared.ConnectionPanel({server:'Fixture',configured:false,url:'http://fixture',onUrl(){},credential:'',onCredential(){},credentialSaved:false,busy:true,onConnect:()=>connects++});find(panel,node=>node.type==='form').props.onSubmit({preventDefault(){}});assert.equal(connects,1);assert.equal(find(panel,node=>node.props.children==='Connect').props.disabled,true);
function hooks(){let cursor=0,slots=[];return{start(){cursor=0;},react:{...React,useState(initial){const at=cursor++;if(!(at in slots))slots[at]=initial;return[slots[at],next=>{slots[at]=typeof next==='function'?next(slots[at]):next;}];},useEffect(){}}};}
(async()=>{
 for(const player of ['jellyfin','plex','kodi']){
  const state=hooks(),calls=[];let data={...base,connected:true,secret_saved:true,username:player==='kodi'?'initial-user':''},answer=null,reloads=0;
  const resource={get data(){return data;},setData(next){data=next;},reload(){}};
  const Component=load('settings/media-players/ServerSettings.tsx',{'react':state.react,'./ConnectionPanel':{ConnectionPanel:'ConnectionPanel'},'../../components/ui':{Button:'Button'},'../../SettingsUI':{Group:'Group',Row:'Row',Toggle:'Toggle',Notice:'Notice',Loading:'Loading',useSettings:()=>resource,useAction:token=>{assert.equal(token,'fixture-csrf');return{busy:false,run:async(url,body)=>{calls.push({url,body});return answer;}};}},'../../components/SelectField':{SelectField:'SelectField'},'../useEngineSettings':{useEngineSettings:()=>({values,busy:false,reload(){reloads++;},save(){}})}}).default;
  const draw=()=>{state.start();return Component({player,csrf:'fixture-csrf'});};
  let connection=find(draw(),node=>node.type==='ConnectionPanel');assert.equal(connection.props.credential,'');connection.props.onUrl('http://draft.fixture');connection.props.onCredential('replacement');
  if(player==='kodi'){connection=find(draw(),node=>node.type==='ConnectionPanel');connection.props.username.onChange('new-user');}
  connection=find(draw(),node=>node.type==='ConnectionPanel');await connection.props.onConnect();assert.equal(find(draw(),node=>node.type==='ConnectionPanel').props.credential,'replacement','failure keeps the new credential for retry');
  const expected={action:'connect',url:'http://draft.fixture',username:player==='kodi'?'new-user':'',secret:'replacement'};assert.deepEqual(JSON.parse(JSON.stringify(calls[0])),{url:'/api/media-players/'+player,body:expected});
  data={...data,url:'http://polled.fixture'};assert.equal(find(draw(),node=>node.type==='ConnectionPanel').props.url,'http://draft.fixture','status refresh cannot replace an unsaved URL');
  answer={...data,url:'http://draft.fixture'};await find(draw(),node=>node.type==='ConnectionPanel').props.onConnect();connection=find(draw(),node=>node.type==='ConnectionPanel');assert.equal(connection.props.credential,'');assert.equal(reloads,1);
  await connection.props.onDisconnect();assert.equal(calls.at(-1).body.action,'disconnect');assert.equal(reloads,2);
 }
 console.log('Media server connections: shared actual Mantine form/status/44px field grammar for all four servers, one native submit, exact API callbacks, masked credentials, failure retention, draft isolation and disconnect behavior passed.');
})().catch(error=>{console.error(error);process.exitCode=1;});
