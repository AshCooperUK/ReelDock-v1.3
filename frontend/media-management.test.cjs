const assert=require('node:assert/strict'),fs=require('node:fs'),vm=require('node:vm'),ts=require('typescript');
const React=require('react');
const states=[],effects=[],requests=[],events=[],timers=[];let cursor=0;
const exportsFor={};
const ui={Button:()=>null,Dialog:()=>null,DialogContent:()=>null,DialogTitle:()=>null,DialogDescription:()=>null,PopupShell:()=>null};
const sheet={ConfirmInline:()=>null,SheetBack:()=>null,DetailSheet:()=>null};
const settings={Field:()=>null,Toggle:()=>null};
const fields={TextInput:()=>null};
const base={series:'',season:null,number:null,date:'2026-10-17',dateLabel:'Added',status:'Saved',monitored:true,automatic:true,hold:false,removed:false,quality:'1080',fallback:false,bytes:2*1024**3,active:false,message:'',channelId:'',channel:'',image:''};
const movie={...base,key:'m1',kind:'movie',title:'A Movie',file:true,filename:'A Movie (2026).mkv',fileQuality:'720',metadataReady:true};
const episode={...base,key:'e1',kind:'episode',series:'A Show',season:1,number:2,title:'Episode Two',file:true,filename:'A Show s01e02.mkv',fileQuality:'Not verified',metadataReady:false};
const video={...base,key:'v1',kind:'video',title:'A Video',file:true,filename:'clip.mp4',fileQuality:'480',metadataReady:false};
const pending={...base,key:'p1',kind:'movie',title:'Pending Film',file:false,filename:'',fileQuality:'Not verified',metadataReady:false,status:'Waiting'};
const busy={...base,key:'a1',kind:'movie',title:'Busy Movie',file:true,filename:'Busy Movie (2026).mkv',fileQuality:'1080',metadataReady:false,active:true,status:'Downloading'};
const releases=[{id:'r480',title:'Release.480p',quality:'480p',size:1,source:'Indexer A',reason:''},{id:'r720',title:'Release.720p',quality:'720p',size:2,source:'Indexer A',reason:''},{id:'r1080',title:'Release.1080p',quality:'1080p',size:3,source:'Indexer B',reason:''},{id:'r1080i',title:'Release.1080i',quality:'1080i',size:3,source:'Indexer B',reason:''},{id:'r2160',title:'Release.2160p',quality:'2160p',size:4,source:'Indexer B',reason:'Already grabbed before'},{id:'runk',title:'Release.Unknown',quality:'Unspecified',size:5,source:'Indexer B',reason:'Outside the selected quality policy'}];
let responseItems=[movie,episode,video,pending,busy];
const respond=(url,body)=>{
 if(body?.action==='enrol-local')return{key:'local:42',entry:movie};
 if(!body)return{items:responseItems};
 if(body.action==='releases')return{items:releases,message:''};
 if(body.action==='rename')return{item:{localChanges:[{from:'a'.repeat(32),to:'b'.repeat(32)}]}};
 if(body.action==='repair-metadata')return{item:{},message:'Artwork unavailable; metadata repaired.'};
 return{item:{}};
};
vm.runInNewContext(ts.transpileModule(fs.readFileSync(__dirname+'/src/MediaManagement.tsx','utf8'),{compilerOptions:{module:ts.ModuleKind.CommonJS,jsx:ts.JsxEmit.ReactJSX,target:ts.ScriptTarget.ES2022}}).outputText,{exports:exportsFor,AbortController,Event,CustomEvent:class extends Event{constructor(name,options){super(name);this.detail=options.detail;}},setTimeout:fn=>timers.push(fn),clearTimeout:()=>{},window:{dispatchEvent:event=>events.push(event)},require:name=>{
 if(name==='react')return{useState:value=>{const i=cursor++;if(!(i in states))states[i]=value;return[states[i],next=>states[i]=typeof next==='function'?next(states[i]):next];},useEffect:fn=>effects.push(fn)};
 if(name==='react/jsx-runtime')return require(name);
 if(name==='@mantine/core')return new Proxy({},{get:(_,key)=>key});
 if(name==='./notifications')return {NotificationMessage:'NotificationMessage'};
 if(name==='./api')return{api:async(url,signal,body,csrf)=>{requests.push({url,signal,body,csrf});return respond(url,body);}};
 if(name==='./components/ui')return ui;
 if(name==='./components/DetailSheet')return sheet;
 if(name==='./SettingsUI')return settings;
 if(name==='./TextField')return fields;
 if(name==='./ChannelIdentity')return{default:()=>null};
 if(name==='./popupActions'){const ex={};vm.runInNewContext(ts.transpileModule(fs.readFileSync(__dirname+'/src/popupActions.ts','utf8'),{compilerOptions:{module:ts.ModuleKind.CommonJS}}).outputText,{exports:ex});return ex;}
 return new Proxy({},{get:()=>()=>null});
}});
const manage=exportsFor.default;
const render=(props={})=>{cursor=0;return manage({csrf:'csrf-fixture',...props});};
// PopupShell carries header/footer content in props (title, subtitle, actions,
// media, footer) rather than children — walk those too, wrapping raw strings
// so textOf() can read header text.
const nodes=value=>value==null||typeof value==='boolean'?[]:Array.isArray(value)?value.flatMap(nodes):typeof value==='object'?[value,...nodes(value.props?.children),...nodes(value.props?.footer),...nodes(value.props?.actions),...nodes(value.props?.title),...nodes(value.props?.subtitle),...nodes(value.props?.media)]:[{type:'span',props:{children:value}}];
const textOf=v=>v==null||typeof v==='boolean'?'':typeof v==='string'||typeof v==='number'?String(v):Array.isArray(v)?v.map(textOf).join(''):typeof v==='object'?textOf(v.props?.children):'';
const buttons=list=>list.filter(n=>n.type===ui.Button);
const labels=list=>buttons(list).map(n=>n.props.children);
const flush=()=>new Promise(setImmediate);
let tree=[];
const row=key=>nodes(tree.find(n=>n.props?.component==='article'&&n.key===key));
// Sub-flows are in-sheet panes (a <Pane back={…}> element), never a stacked
// Dialog. The destructive confirm is a ConfirmInline element whose onConfirm
// prop stands in for the old "Delete video file" dialog button.
const pane=()=>tree.find(n=>typeof n.props?.back==='function'&&typeof n.props?.title==='string');
const confirmBox=()=>tree.find(n=>n.type===sheet.ConfirmInline);
(async()=>{
 tree=nodes(render());
 for(const fn of effects.splice(0))fn();
 await flush();await flush();
 effects.length=0;
 tree=nodes(render());
 assert.equal(requests[0].body,undefined,'listing loads without an action');
 assert.deepEqual(labels(row('m1')).filter(l=>['Correct title','Repair metadata','Find better quality'].includes(l)),['Correct title','Find better quality'],'saved movies get rename and replace but not repair when metadata is ready');
 assert.ok(labels(row('e1')).includes('Correct title')&&labels(row('e1')).includes('Repair metadata')&&labels(row('e1')).includes('Find better quality'),'saved episodes get all three file actions');
 assert.ok(!labels(row('v1')).includes('Correct title')&&!labels(row('v1')).includes('Find better quality')&&labels(row('v1')).includes('Repair metadata'),'video rows keep repair but never rename or replace');
 assert.ok(labels(row('p1')).includes('Compare releases')&&!labels(row('p1')).includes('Find better quality')&&!labels(row('p1')).includes('Correct title'),'unsaved entries keep the unfiltered compare');
 assert.ok(textOf(row('m1')).includes('File quality: 720'),'verified file quality is shown');
 assert.ok(textOf(row('e1')).includes('File quality: Not verified'),'unverified files keep the honest label');
 assert.deepEqual(labels(row('a1')).filter(l=>['Correct title','Repair metadata','Find better quality','Compare releases'].includes(l)),[],'an active acquisition hides the file-management actions');
 assert.ok(labels(row('a1')).includes('Cancel acquisition'),'an active acquisition keeps its cancel control');
 assert.ok(buttons(row('a1')).find(n=>n.props.children==='Delete file').props.disabled,'delete stays visible but disabled while active');

 buttons(row('m1')).find(n=>n.props.children==='Correct title').props.onClick();
 tree=nodes(render());let body=nodes(pane());
 assert.ok(pane(),'rename pane opens inline');
 let input=body.find(n=>n.type===fields.TextInput);
 assert.equal(input.props.value,'A Movie (2026)','movie rename seeds the filename stem without the extension');
 input.props.onChange({target:{value:'   '}});
 tree=nodes(render());body=nodes(pane());
 assert.ok(body.find(n=>n.type===ui.Button&&n.props.children==='Save title').props.disabled,'blank titles cannot be saved');
 body.find(n=>n.type===fields.TextInput).props.onChange({target:{value:'Better Movie'}});
 tree=nodes(render());body=nodes(pane());
 body.find(n=>n.type===ui.Button&&n.props.children==='Save title').props.onClick();
 await flush();await flush();
 let call=requests.find(r=>r.body?.action==='rename');
 assert.equal(call.body.key,'m1');assert.equal(call.body.value,'Better Movie');assert.equal(call.body.confirm,false);
 assert.equal(events.at(-1).type,'reeldock-library-changed');assert.equal(events.at(-1).detail.localChanges[0].from,'a'.repeat(32));assert.equal(events.at(-1).detail.localChanges[0].to,'b'.repeat(32),'rename event carries exact server IDs to open sheets');
 tree=nodes(render());assert.ok(!pane(),'rename pane closes after saving');

 buttons(row('e1')).find(n=>n.props.children==='Correct title').props.onClick();
 tree=nodes(render());body=nodes(pane());
 assert.equal(body.find(n=>n.type===fields.TextInput).props.value,'Episode Two','episode rename seeds the episode title');
 assert.ok(textOf(body).includes('shared show folder'),'episode renames explain the shared folder keeps its name');
 body.find(n=>n.type===fields.TextInput).props.onChange({target:{value:'Renamed Episode Two'}});
 tree=nodes(render());body=nodes(pane());
 body.find(n=>n.type===ui.Button&&n.props.children==='Save title').props.onClick();
 await flush();await flush();
 call=requests.filter(r=>r.body?.action==='rename').at(-1);
 assert.equal(call.body.key,'e1');assert.equal(call.body.value,'Renamed Episode Two','episode rename submits the new title');
 tree=nodes(render());assert.ok(!pane());

 buttons(row('e1')).find(n=>n.props.children==='Repair metadata').props.onClick();
 await flush();await flush();
 call=requests.find(r=>r.body?.action==='repair-metadata');
 assert.equal(call.body.key,'e1');
 tree=nodes(render());
 assert.ok(tree.some(n=>n.type==='NotificationMessage'&&n.props.kind==='success'&&n.props.message==='Artwork unavailable; metadata repaired.'),'a partial repair response is preserved exactly in the shared outcome notification');

 buttons(row('m1')).find(n=>n.props.children==='Delete file').props.onClick();
 tree=nodes(render());body=nodes(pane());
 const toggle=body.find(n=>n.type===settings.Toggle);
 assert.equal(toggle.props.title,'Also remove the folder if it only has metadata and artwork left','movie deletes offer folder removal');
 assert.ok(textOf(body).includes('Artwork and metadata stay.'),'default delete copy keeps sidecars');
 toggle.props.onChange(true);
 tree=nodes(render());body=nodes(pane());
 assert.ok(textOf(body).includes('artwork and metadata are removed too'),'enabled folder removal updates the warning');
 confirmBox().props.onConfirm();
 await flush();await flush();
 call=requests.find(r=>r.body?.action==='delete');
 assert.equal(call.body.key,'m1');assert.deepEqual(Object.entries(call.body.value),[['folder',true]]);assert.equal(call.body.confirm,true);

 tree=nodes(render());buttons(row('m1')).find(n=>n.props.children==='Delete file').props.onClick();
 tree=nodes(render());body=nodes(pane());
 confirmBox().props.onConfirm();
 await flush();await flush();
 assert.deepEqual(Object.entries(requests.filter(r=>r.body?.action==='delete').at(-1).body.value),[['folder',false]],'an untoggled movie delete sends folder:false');

 tree=nodes(render());buttons(row('e1')).find(n=>n.props.children==='Delete file').props.onClick();
 tree=nodes(render());body=nodes(pane());
 assert.ok(!body.some(n=>n.type===settings.Toggle),'episode deletes never offer folder removal');
 confirmBox().props.onConfirm();
 await flush();await flush();
 assert.deepEqual(Object.entries(requests.filter(r=>r.body?.action==='delete').at(-1).body.value),[['folder',false]],'non-movie deletes send folder:false');

 tree=nodes(render());buttons(row('m1')).find(n=>n.props.children==='Find better quality').props.onClick();
 await flush();await flush();
 assert.equal(requests.at(-1).body.action,'releases');
 tree=nodes(render());body=nodes(pane());
 assert.ok(textOf(body).includes('Find better quality')&&textOf(body).includes('File quality: 720'));
 assert.deepEqual(body.filter(n=>n.type==='Title'&&n.props.order===3).map(n=>n.props.children),['Release.1080p','Release.1080i','Release.2160p','Release.Unknown'],'replace offers only above-720 or unparseable releases: same and lower quality stay out, 1080i parses as 1080');
 const swaps=buttons(body).filter(n=>n.props.children==='Replace with this release');
 assert.equal(swaps.length,4);
 assert.deepEqual(swaps.map(n=>!!n.props.disabled),[false,false,true,true],'reason-disabled and unparseable releases stay unselectable');
 swaps[0].props.onClick();
 await flush();await flush();
 call=requests.find(r=>r.body?.action==='replace');
 assert.equal(call.body.key,'m1');assert.equal(call.body.value,'r1080');assert.equal(call.body.confirm,false);
 tree=nodes(render());assert.ok(!pane(),'replace pane closes after queueing');

 buttons(row('e1')).find(n=>n.props.children==='Find better quality').props.onClick();
 await flush();await flush();
 tree=nodes(render());body=nodes(pane());
 assert.deepEqual(body.filter(n=>n.type==='Title'&&n.props.order===3).map(n=>n.props.children),releases.map(r=>r.title),'unverified files see every rankable release');
 pane().props.back();
 tree=nodes(render());assert.ok(!pane());

 buttons(row('p1')).find(n=>n.props.children==='Compare releases').props.onClick();
 await flush();await flush();
 tree=nodes(render());body=nodes(pane());
 assert.ok(textOf(body).includes('Compare releases'),'unsaved entries keep the compare title');
 assert.deepEqual(body.filter(n=>n.type==='Title'&&n.props.order===3).map(n=>n.props.children),releases.map(r=>r.title),'compare stays unfiltered');
 assert.equal(buttons(body).filter(n=>n.props.children==='Download this release').length,6,'compare keeps the download action');

 // Local Library files enrol through their scan identity, then poll the returned key.
 requests.length=0;states.length=0;effects.length=0;cursor=0;
 tree=nodes(manage({csrf:'csrf-fixture',localId:'scan:42'}));
 for(const fn of effects.splice(0))fn();
 await flush();await flush();
 assert.equal(requests[0].body?.action,'enrol-local','local files enrol by scan identity before listing');
 assert.equal(requests[0].body?.value?.id,'scan:42','the scan identity is the enrolment value');
 assert.equal(requests[1].url,'/api/media-management?key='+encodeURIComponent('local:42'),'the enrolled key scopes subsequent reads');
 cursor=0;tree=nodes(manage({csrf:'csrf-fixture',localId:'scan:42'}));
 assert.ok(row('m1').length,'the enrolled local entry renders its row');

 // A show library list includes only server-confirmed files, even when the
 // same parent has catalogue, active, missing or removed request records.
 const showItem={id:'show:42',kind:'show',title:'A Show',externalId:'42'};
 const catalogue={...episode,key:'catalogue',title:'Catalogue only',season:3,file:false,filename:'Looks like a file.mkv'};
 const activeOnly={...catalogue,key:'active-only',title:'Active request',season:4,active:true,status:'Downloading'};
 const truthyFile={...catalogue,key:'truthy-file',title:'Unverified flag',season:5,file:'true'};
 const special={...episode,key:'special',title:'Downloaded special',season:0,number:1};
 const removedFile={...episode,key:'removed-file',title:'Removed saved episode',season:2,removed:true};
 const removedMissing={...catalogue,key:'removed-missing',title:'Removed request',season:8,removed:true};
 responseItems=[catalogue,activeOnly,truthyFile,episode,special,removedFile,removedMissing];
 async function loadView(props){
  requests.length=0;states.length=0;effects.length=0;timers.length=0;
  tree=nodes(render(props));for(const fn of effects.splice(0))fn();
  await flush();await flush();effects.length=0;tree=nodes(render(props));effects.length=0;
 }
 const library={item:showItem,filesOnly:true};
 await loadView(library);
 assert.equal(requests.length,1);assert.equal(requests[0].url,'/api/media-management?parent='+encodeURIComponent(showItem.id));
 assert.equal(requests[0].body,undefined,'files-only opening reads existing library without catalogue enrolment');
 assert.deepEqual(nodes(tree.find(n=>n.props?.['aria-label']==='Managed media filter')).filter(n=>n.type==='option').map(n=>n.props.children),['Downloaded files','Removed files'],'library filters describe files rather than catalogue requests');
 assert.deepEqual(tree.filter(n=>n.props?.component==='article').map(n=>n.key).sort(),['e1','special'],'only actual file=true episodes are shown, including specials');
 let seasonControl=tree.find(n=>n.props?.['aria-label']==='Season');
 assert.deepEqual(nodes(seasonControl).filter(n=>n.type==='option').map(n=>n.props.value),['all','2','1','0'],'only downloaded-file seasons are offered, including removed files');
 assert.ok(labels(row('e1')).includes('Correct title')&&labels(row('e1')).includes('Delete file'),'saved file actions remain available in library view');
 seasonControl.props.onChange({target:{value:'1'}});tree=nodes(render(library));
 assert.deepEqual(tree.filter(n=>n.props?.component==='article').map(n=>n.key),['e1'],'season selection filters downloaded rows');
 tree.find(n=>n.props?.['aria-label']==='Season').props.onChange({target:{value:'all'}});
 tree=nodes(render(library));tree.find(n=>n.props?.['aria-label']==='Managed media filter').props.onChange({target:{value:'removed'}});
 tree=nodes(render(library));assert.ok(row('removed-file').length&&!row('removed-missing').length,'removed view still exposes retained files without missing catalogue rows');
 assert.ok(labels(row('removed-file')).includes('Restore entry'),'retained removed files keep recovery controls');
 responseItems=[catalogue,activeOnly];timers.at(-1)();await flush();await flush();tree=nodes(render(library));
 assert.ok(textOf(tree).includes('No downloaded episodes in your library.'),'an empty library explains that no episodes have downloaded');
 assert.equal(tree.filter(n=>n.props?.component==='article').length,0);assert.ok(!tree.some(n=>n.props?.['aria-label']==='Season'),'polling removes stale file rows and seasons');
 assert.equal(requests.length,2);assert.ok(requests.every(r=>r.body===undefined),'files-only initial load and polling remain read-only');

 responseItems=[catalogue,episode,removedFile,removedMissing];
 await loadView({item:showItem,filesOnly:false});
 assert.equal(requests[0].body?.action,'enrol','generic management keeps its original show enrolment');
 assert.deepEqual(nodes(tree.find(n=>n.props?.['aria-label']==='Managed media filter')).filter(n=>n.type==='option').map(n=>n.props.children),['Managed media','Removed entries'],'generic filter copy remains unchanged');
 assert.ok(row('catalogue').length,'generic management still includes unsaved catalogue episodes');
 buttons(row('catalogue')).find(n=>n.props.children==='Episode settings').props.onClick();
 tree=nodes(render({item:showItem,filesOnly:false}));
 assert.equal(pane().props.title,'Episode settings');
 const quality=nodes(pane()).find(n=>n.props?.['aria-label']==='Preferred quality for Catalogue only');
 assert.equal(quality.props.value,'1080','selected unsaved episode settings still resolve from the full record list');
 quality.props.onChange({target:{value:'720'}});await flush();await flush();
 call=requests.find(r=>r.body?.action==='quality');assert.equal(call.body.key,'catalogue');assert.equal(call.body.value,'720');assert.equal(call.csrf,'csrf-fixture');
 tree=nodes(render({item:showItem,filesOnly:false}));pane().props.back();tree=nodes(render({item:showItem,filesOnly:false}));
 tree.find(n=>n.props?.['aria-label']==='Managed media filter').props.onChange({target:{value:'removed'}});tree=nodes(render({item:showItem,filesOnly:false}));
 assert.ok(row('removed-missing').length,'generic Removed entries retains missing-file recovery');
 buttons(row('removed-missing')).find(n=>n.props.children==='Restore entry').props.onClick();await flush();await flush();
 call=requests.find(r=>r.body?.action==='restore');assert.equal(call.body.key,'removed-missing');assert.equal(call.csrf,'csrf-fixture');

 console.log('Media management: file actions, local/default enrolment, strict downloaded-library rows/seasons, read-only polling, empty state, episode settings and removed recovery passed.');
})().catch(e=>{console.error(e);process.exitCode=1;});

// ReelDock never plays video: the shared action schema has no Play or external-watch action.
{
 const schema={};vm.runInNewContext(ts.transpileModule(fs.readFileSync(__dirname+'/src/popupActions.ts','utf8'),{compilerOptions:{module:ts.ModuleKind.CommonJS}}).outputText,{exports:schema});
 const ids=schema.ACTIONS.map(spec=>spec.id);
 assert.ok(!ids.includes('play')&&!ids.includes('viewExternal'),'no play or external-watch action');
 for(const kind of ['movie','show','episode','video','vod'])for(const placement of ['header','entry']){
  const labels=schema.actionsFor({kind,file:true,automatic:true},placement).map(spec=>schema.actionLabel(spec,{kind}));
  assert.ok(!labels.some(label=>/\bplay\b|\bwatch\b|trailer|youtube/i.test(label)),kind+' '+placement+' has no playback labels: '+labels.join(','));
 }
 assert.ok(!/Managed ?FilePlayer|setPlaying|case 'play'/.test(fs.readFileSync(__dirname+'/src/MediaManagement.tsx','utf8')));
 console.log('Popup actions: playback and external-watch actions are gone; downloads, settings and management actions remain.');
}
