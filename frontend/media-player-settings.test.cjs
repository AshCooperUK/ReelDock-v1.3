const assert=require('node:assert/strict'),fs=require('node:fs'),vm=require('node:vm'),ts=require('typescript');
const jsx=(type,props)=>({type,props:props||{}});
function compile(file,imports){const out={};const code=ts.transpileModule(fs.readFileSync(__dirname+'/src/'+file,'utf8'),{compilerOptions:{module:ts.ModuleKind.CommonJS,jsx:ts.JsxEmit.ReactJSX}}).outputText;vm.runInNewContext(code,{exports:out,encodeURIComponent,require:n=>n==='react/jsx-runtime'?{jsx,jsxs:jsx,Fragment:'Fragment'}:imports[n]||new Proxy({},{get:(_,key)=>key})});return out;}
function nodes(tree){return !tree||typeof tree!=='object'?[]:Array.isArray(tree)?tree.flatMap(nodes):[tree,...nodes(tree.props?.children)];}
let selected='Emby';
const menu=compile('settings/media-players/MediaPlayerSettings.tsx',{
 react:{useState:v=>[v,n=>selected=n]},
 '@mantine/hooks':{useMediaQuery:()=>false},
 '../../components/ui':{Button:'Button'},
 './EmbySettings':{default:'EmbySettings'},'./ServerSettings':{default:'ServerSettings'}
}).default({csrf:'token'});
const chooser=nodes(menu).find(n=>n.type==='SegmentedControl');
assert.deepEqual(Array.from(chooser.props.data),['Emby','Jellyfin','Plex','Kodi']);
chooser.props.onChange('Plex');assert.equal(selected,'Plex');
const patches=[],calls=[];
const panel=compile('settings/media-players/ServerSettings.tsx',{
 react:{useState:v=>[v,()=>{}],useEffect:()=>{}},
 '../../SettingsUI':{Group:'Group',Loading:'Loading',Notice:'Notice',Row:'Row',Toggle:'Toggle',Field:'Field',useSettings:()=>({data:{url:'http://fixture.test',username:'',connected:true,secret_saved:true,library:'1',libraries:[{id:'1',name:'Movies'}],users:[],status:'Connected.'},setData(){},reload(){}}),useAction:()=>({run:async(url,body)=>{calls.push({url,body});return null;}})},
 '../useEngineSettings':{useEngineSettings:engine=>{assert.equal(engine,'media-players');return {values:{plex_sync:false,plex_auto_scan:false,plex_minutes:15},save:patch=>patches.push(patch)};}}
}).default({player:'plex',csrf:'token'});
const all=nodes(panel);
assert.equal(all.find(n=>n.type==='ConnectionPanel').props.credential,'','saved credentials stay masked in the shared connection form');
all.find(n=>n.props.title==='Sync library status').props.onChange(true);
assert.equal(patches[0].plex_sync,true);
all.find(n=>n.props.children==='Scan library').props.onClick();
assert.equal(calls[0].url,'/api/media-players/plex');assert.equal(calls[0].body.action,'scan');
assert.ok(!all.some(n=>n.props.label==='Directory'));
console.log('Media Players: top navigation, masked credentials, scoped preferences and scan actions passed.');
const matchingCalls=[];
const matching=compile('settings/youtube/WatchedMatching.tsx',{
 '../../components/ui':{Button:'Button',PanelButton:'PanelButton'},
 '../../SettingsUI':{Group:'Group',Loading:'Loading',Notice:'Notice',Row:'Row',Toggle:'Toggle',useSettings:()=>({data:{source:'plex',enabled:true,status:'Ready',cookie_check:'',sources:['emby','jellyfin','plex','kodi'].map(id=>({id,ready:true})),attempts:[]},setData(){}}),useAction:()=>({run:async(url,body)=>{matchingCalls.push({url,body});return null;}})}
}).default({csrf:'token'});
const matchingNodes=nodes(matching);
const source=matchingNodes.find(n=>n.props['aria-label']==='Source player');
source.props.onChange({target:{value:'kodi'}});
matchingNodes.find(n=>n.props.title==='Match watched status').props.onChange(false);
assert.equal(matchingCalls[0].url,'/api/youtube/watched-matching');
assert.equal(matchingCalls[0].body.values.source,'kodi');
assert.equal(matchingCalls[1].body.values.enabled,false);
assert.ok(!fs.readFileSync(__dirname+'/src/settings/media-players/EmbySettings.tsx','utf8').includes('title="Watched matching"'));
console.log('Shared YouTube matching selector, enable toggle and Emby control removal passed.');
