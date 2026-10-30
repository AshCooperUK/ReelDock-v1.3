const assert=require('node:assert/strict'),fs=require('node:fs'),vm=require('node:vm'),ts=require('typescript');
const jsx=(type,props)=>({type,props:props||{}});
function compile(file,imports){const out={};const code=ts.transpileModule(fs.readFileSync(__dirname+'/src/'+file,'utf8'),{compilerOptions:{module:ts.ModuleKind.CommonJS,jsx:ts.JsxEmit.ReactJSX}}).outputText;vm.runInNewContext(code,{exports:out,encodeURIComponent,require:n=>n==='react/jsx-runtime'?{jsx,jsxs:jsx,Fragment:'Fragment'}:imports[n]||new Proxy({},{get:(_,key)=>key})});return out;}
function nodes(tree){return !tree||typeof tree!=='object'?[]:Array.isArray(tree)?tree.flatMap(nodes):[tree,...nodes(tree.props?.children)];}
(async()=>{
 let release,calls=[],saved=[];
 const hook=compile('settings/useEngineSettings.ts',{
  react:{useRef:v=>({current:v})},
  '../SettingsUI':{useSettings:url=>{assert.equal(url,'/api/settings/youtube');return{data:{values:{quality:'1080'}},setData:v=>saved.push(v),error:'',reload(){}};},useAction:csrf=>{assert.equal(csrf,'token');return{run:(url,body)=>{calls.push({url,body});return new Promise(resolve=>release=resolve);}};}}
 }).useEngineSettings('youtube','token');
 const pending=hook.save({quality:'720'});
 assert.equal(await hook.save({quality:'480'}),false); // no overlapping save that could race
 assert.equal(calls.length,1);assert.equal(calls[0].body.values.quality,'720');
 release({engine:'youtube',values:{quality:'720'}});assert.equal(await pending,true);
 assert.equal(saved[0].values.quality,'720');
 const failed=hook.save({quality:'480'});release(null);assert.equal(await failed,false);
 assert.equal(saved.length,1); // failed requests do not replace confirmed values
 const updates=[];
 const form=compile('settings/youtube/YouTubePreferences.tsx',{
  '../../SettingsUI':{Group:'Group',Loading:'Loading',Notice:'Notice',Row:'Row',Toggle:'Toggle'},
  '../useEngineSettings':{useEngineSettings:()=>({values:{quality:'1080',minimum_video_minutes:5,default_range:180,sleep_interval:0,sponsorblock:'off'},save:async v=>updates.push(v)})}
 }).default({csrf:'token'});
 const all=nodes(form),quality=all.find(n=>n.props['aria-label']==='Maximum quality');
 await quality.props.onChange({target:{value:'720'}});assert.equal(updates[0].quality,'720');
 const delay=all.find(n=>n.props['aria-label']==='Wait between downloads (seconds)');
 delay.props.onBlur({currentTarget:{value:'25',checkValidity:()=>true}});assert.equal(updates[1].sleep_interval,25);
 delay.props.onBlur({currentTarget:{value:'-2',checkValidity:()=>false,reportValidity(){}}});assert.equal(updates.length,2);
 for(const title of ['Auto Downloads','Metadata format','Pause downloads']) assert.ok(!all.some(n=>n.props.title===title));
 const m3uChanges=[];
 const metadata=compile('settings/MetadataSettings.tsx',{
  react:{useState:v=>[v,()=>{}]},
  '../SettingsUI':{Group:'Group',Loading:'Loading',Notice:'Notice',Row:'Row',Toggle:'Toggle',Field:'Field',useSettings:()=>({data:{tmdb_saved:true},reload(){}}),useAction:()=>({})},
  './useEngineSettings':{useEngineSettings:engine=>({values:{share_metadata:true},save:async patch=>m3uChanges.push({engine,...patch})})}
 }).default({csrf:'token'});
 await nodes(metadata).find(n=>n.props.title==='Save local metadata & artwork').props.onChange(false);
 assert.equal(m3uChanges[0].share_metadata,false);
 assert.equal(m3uChanges[0].engine,'m3u');
 const metadataGroups=nodes(metadata).filter(n=>n.type==='Group'&&['NZB metadata','Torrent metadata','YouTube metadata'].includes(n.props.title));
 for(const group of metadataGroups) await nodes(group).find(n=>n.type==='Toggle').props.onChange(false);
 assert.deepEqual(m3uChanges.map(x=>x.engine),['m3u','nzb','torrent','youtube']);
 assert.equal(nodes(metadata).find(n=>n.props.type==='password').props.value,'');
 console.log('Engine settings: isolated values, confirmed saves, failure preservation, overlapping-save guard and YouTube form boundaries passed.');
})().catch(e=>{console.error(e);process.exitCode=1;});
