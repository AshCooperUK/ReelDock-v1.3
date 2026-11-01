const assert=require('node:assert/strict'),fs=require('node:fs'),path=require('node:path'),vm=require('node:vm'),ts=require('typescript');
const React=require('react'),Mantine=require('@mantine/core'),{renderToStaticMarkup}=require('react-dom/server');
function load(file,imports={}){const output={};vm.runInNewContext(ts.transpileModule(fs.readFileSync(path.join(__dirname,'src',file),'utf8'),{compilerOptions:{module:ts.ModuleKind.CommonJS,jsx:ts.JsxEmit.ReactJSX}}).outputText,{exports:output,require:name=>imports[name]||(name.endsWith('/SelectField')?require('./select-field-test-helper.cjs')():require(name))});return output;}
const ui=load('components/ui.tsx',{'../ScrollToTop':{default:()=>null},'../notifications-state':{registerNotificationHost:()=>()=>{}},'@mantine/hooks':{useMediaQuery:()=>false}});
const layout=load('components/layout.tsx'),fields=load('TextField.tsx');
const settings=load('SettingsUI.tsx',{'./api':{},'./components/ui':ui,'./components/layout':layout});
const listing={kind:'movies',folder:'',files:[{path:'Alpha.mkv',bytes:1500},{path:'Charlie.mkv',bytes:10}],folders:['Zulu','Bravo','Partial','Unknown','Empty'],folderSizes:{Zulu:{bytes:20,complete:true,reason:''},Bravo:{bytes:5000,complete:true,reason:''},Partial:{bytes:999999999,complete:false,reason:'limit'},Unknown:{bytes:null,complete:false,reason:'unreadable'},Empty:{bytes:0,complete:true,reason:''}},truncated:false};
const data={enabled:false,tools_available:true,output_root:'/fixture-output',output_error:'',libraries:[{kind:'movies',available:true,path:'/fixture-movies',message:'',output_available:true,output_path:'/fixture-output-movies',output_message:''},{kind:'tv',available:false,path:'',message:'Not configured.'}],jobs:[]};
let cursor=0,states=[],requests=[],resourceError='';
const fakeReact={...React,useState(initial){const index=cursor++;if(!(index in states))states[index]=typeof initial==='function'?initial():initial;return[states[index],value=>states[index]=typeof value==='function'?value(states[index]):value];},useEffect(effect){effect();}};
const pollLogic=load('libraryOptimiseResource.ts',{'./api':{},'./SettingsUI':settings,'./visibility':{}});
const activityGroup=load('components/ActivityGroup.tsx');
const jobList=load('LibraryOptimiseJobs.tsx',{'./components/ui':ui,'./components/ActivityGroup':activityGroup,'./SettingsUI':settings,'./libraryOptimiseResource':pollLogic});
const subject=load('LibraryOptimiseSettings.tsx',{'react':fakeReact,'./LibraryOptimiseJobs':jobList,'./libraryOptimiseResource':{useLibraryOptimise:()=>({data,setData(next){Object.assign(data,next);},error:resourceError,reload(){}})},'./api':{api(){throw new Error('Idle library settings must not poll or scan');}},'./components/ui':ui,'./TextField':fields,'./SettingsUI':{...settings,useSettings:()=>({data,setData(next){Object.assign(data,next);},error:'',reload(){}}),useAction:()=>({busy:false,error:'',notice:'',async run(url,body){requests.push({url,body});if(body.action==='scan')return listing;if(body.action==='enqueue')return{...data,jobs:[{id:'fixture-job',kind:body.kind,path:body.path,profile:body.profile,stereo:body.stereo,limit1080:body.limit1080,state:'failed',error:'Fixture failure',source_bytes:1500,output_bytes:0,output:''}]};return data;}})}});
const walk=node=>!node||typeof node!=='object'?[]:Array.isArray(node)?node.flatMap(walk):[node,...walk(node.props?.children),...((node.type===jobList.LibraryOptimiseJobs||node.type?.name==='JobCard')?walk(node.type(node.props)):[])];
const render=()=>{cursor=0;return subject.default({csrf:'fixture-csrf'});};
const names=entries=>Array.from(entries,entry=>entry.path);
assert.deepEqual(names(subject.optimiseListingEntries(listing,'name')),['Alpha.mkv','Bravo','Charlie.mkv','Empty','Partial','Unknown','Zulu'],'name sorting combines folders and files');
assert.deepEqual(names(subject.optimiseListingEntries(listing,'size')),['Bravo','Alpha.mkv','Zulu','Charlie.mkv','Empty','Partial','Unknown'],'exact sizes sort together; partial lower bounds never masquerade as complete totals');
assert.equal(subject.optimiseEntrySize({bytes:32,complete:true,reason:''}),'32 B');
assert.equal(subject.optimiseEntrySize({bytes:0,complete:true,reason:''}),'0 B','a confirmed empty folder has an exact zero');
assert.match(subject.optimiseEntrySize({bytes:2000,complete:false,reason:'limit'}),/^At least .*partial$/);
for(const bytes of [null,0])assert.match(subject.optimiseEntrySize({bytes,complete:false,reason:'unreadable'}),/^Size unavailable · unreadable$/,'unknown totals are never rendered as exact zero');
assert.deepEqual(names(subject.optimiseListingEntries({...listing,folderSizes:undefined},'size')),['Alpha.mkv','Charlie.mkv','Bravo','Empty','Partial','Unknown','Zulu'],'older API folder sizes remain explicitly unknown');
(async()=>{
 let tree=render();assert.equal(requests.length,0,'opening settings must not list or recursively stat libraries');
 const form=walk(tree).find(node=>node.type==='form');let prevented=false;
 form.props.onSubmit({preventDefault(){prevented=true;}});await new Promise(setImmediate);assert.ok(prevented);
 assert.equal(requests.length,1);assert.deepEqual(JSON.parse(JSON.stringify(requests[0].body)),{action:'scan',kind:'movies',folder:''});
 tree=render();const selector=walk(tree).find(node=>node.props?.['aria-label']==='Optimisation file and folder sort');
 assert.equal(selector.props.value,'name');assert.deepEqual(React.Children.toArray(selector.props.children).map(option=>option.props.children),['Name A-Z','Size (largest first)']);selector.props.onChange({target:{value:'size'}});tree=render();
 assert.equal(requests.length,1,'sorting is local and does not rescan or enqueue');
 for(const scheme of ['light','dark']){
  const html=renderToStaticMarkup(React.createElement(Mantine.MantineProvider,{forceColorScheme:scheme},tree));
  const expected=['Bravo','Alpha.mkv','Zulu','Charlie.mkv','Empty','Partial','Unknown'];let previous=-1;
  for(const label of expected){const current=html.indexOf('>'+label+'</p>');assert.ok(current>previous,'actual rendered combined order: '+label);previous=current;}
  assert.match(html,/At least .*partial/);assert.match(html,/Size unavailable · unreadable/);assert.match(html,/>0 B<\/p>/);
  assert.match(html,/Size \(largest first\)/);assert.match(html,/Incomplete totals appear last/);
  assert.doesNotMatch(html,/role="dialog"/,'folder browsing stays inline');
 }
 const queues=walk(tree).filter(node=>node.type===ui.Button&&node.props.children==='Queue copy');assert.equal(queues.length,2);assert.ok(queues.every(node=>node.props.disabled),'listing and sorting never enable the optional engine');
 const open=walk(tree).find(node=>node.props?.['aria-label']==='Open folder Bravo');open.props.onClick();await new Promise(setImmediate);
 assert.deepEqual(JSON.parse(JSON.stringify(requests[1].body)),{action:'scan',kind:'movies',folder:'Bravo'});assert.ok(requests.every(request=>request.body.action==='scan'));
 tree=render();let stereo=walk(tree).find(node=>node.type===settings.Toggle&&node.props.title==='Convert audio to stereo');let limit=walk(tree).find(node=>node.type===settings.Toggle&&node.props.title==='Limit video to 1080p');
 assert.equal(stereo.props.value,false);assert.equal(limit.props.value,false,'both new options default off independently');
 stereo.props.onChange(true);tree=render();limit=walk(tree).find(node=>node.type===settings.Toggle&&node.props.title==='Limit video to 1080p');assert.equal(limit.props.value,false);limit.props.onChange(true);tree=render();
 assert.equal(requests.length,2,'choosing options does not queue, scan or save unrelated settings');
 data.enabled=true;data.libraries[0].output_available=false;data.libraries[0].output_message='Synthetic beside-original output unavailable';tree=render();assert.ok(walk(tree).filter(node=>node.type===ui.Button&&node.props.children==='Queue copy').every(node=>node.props.disabled));assert.match(renderToStaticMarkup(React.createElement(Mantine.MantineProvider,null,tree)),/Synthetic beside-original output unavailable/);data.libraries[0].output_available=true;data.libraries[0].output_message='';tree=render();walk(tree).find(node=>node.type===ui.Button&&node.props.children==='Queue copy').props.onClick();await new Promise(setImmediate);
 assert.deepEqual(JSON.parse(JSON.stringify(requests[2].body)),{action:'enqueue',kind:'movies',path:'Alpha.mkv',profile:'hevc',stereo:true,limit1080:true});
 tree=render();for(const title of ['Convert audio to stereo','Limit video to 1080p'])walk(tree).find(node=>node.type===settings.Toggle&&node.props.title===title).props.onChange(false);tree=render();
 const saved=renderToStaticMarkup(React.createElement(Mantine.MantineProvider,null,tree));assert.match(saved,/AAC stereo audio/);assert.match(saved,/1080p limit/);assert.match(saved,/Mono also becomes stereo/);assert.match(saved,/Retry uses the job’s saved choices/);
 walk(tree).find(node=>node.type===ui.Button&&node.props.children==='Retry').props.onClick();await new Promise(setImmediate);assert.deepEqual(JSON.parse(JSON.stringify(requests[3].body)),{action:'retry',id:'fixture-job'},'Retry uses persisted options, not current form switches');
 resourceError='Synthetic status refresh failed';tree=render();const stale=renderToStaticMarkup(React.createElement(Mantine.MantineProvider,null,tree));assert.match(stale,/Synthetic status refresh failed/);assert.match(stale,/AAC stereo audio/,'a failed refresh preserves last known job data with a visible error');assert.match(stale,/Refresh jobs/);
 console.log('Optimiser listing/options: combined sort, truthful sizes, explicit scans/queue, independent default-off stereo/1080 choices, persisted job summary and unchanged Retry choices passed.');
})().catch(error=>{console.error(error);process.exitCode=1;});
