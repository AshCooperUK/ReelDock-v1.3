const assert=require('node:assert/strict'),fs=require('node:fs'),vm=require('node:vm'),ts=require('typescript');
const jsx=(type,props)=>({type,props:props||{}});
function nodes(tree){return !tree||typeof tree!=='object'?[]:Array.isArray(tree)?tree.flatMap(nodes):[tree,...nodes(tree.props?.children)];}
function compile(file,imports){const out={};vm.runInNewContext(ts.transpileModule(fs.readFileSync(__dirname+'/src/'+file,'utf8'),{compilerOptions:{module:ts.ModuleKind.CommonJS,jsx:ts.JsxEmit.ReactJSX}}).outputText,{exports:out,require:n=>n==='react/jsx-runtime'?{jsx,jsxs:jsx,Fragment:'Fragment'}:imports[n]||new Proxy({},{get:(_,key)=>key})});return out;}
(async()=>{
 const patches=[],actions=[],stateChanges=[];
 const settings={max_active_downloads:3,download_limit_bps:0,servers:[{id:'fixture',name:'Test server',host:'news.example.test',port:563,ssl:true,connections:8,priority:0,password_saved:true,enabled:true}],indexers:[]};
 const form=compile('settings/nzb/NzbSettings.tsx',{
  react:{useRef:()=>({current:false}),useState:v=>[v,next=>stateChanges.push({initial:v,next})]},
  '../../SettingsUI':{Field:'Field',Group:'Group',Loading:'Loading',Notice:'Notice',Row:'Row',Toggle:'Toggle',useSettings:()=>({data:settings,setData(){}}),useAction:()=>({busy:false,run:async(url,body)=>{actions.push({url,body});return null;}})},
  '../useEngineSettings':{useEngineSettings:(engine)=>{assert.equal(engine,'nzb');return{values:settings,save:async v=>patches.push(v)};}}
 }).default({csrf:'test'});
 const all=nodes(form);
 all.find(n=>n.props['aria-label']==='NZB download limit (KB/s)').props.onBlur({currentTarget:{value:'128',checkValidity:()=>true}});
 assert.equal(patches[0].download_limit_bps,131072);
 all.find(n=>n.props['aria-label']==='NZBs at once').props.onBlur({currentTarget:{value:'1000',checkValidity:()=>false}});
 assert.equal(patches.length,1);
 assert.equal(all.find(n=>n.props['aria-label']==='Usenet password').props.type,'password');
 assert.equal(all.find(n=>n.props['aria-label']==='Indexer API key').props.type,'password');
 all.find(n=>n.type==='form').props.onSubmit({preventDefault(){}});
 await new Promise(resolve=>setImmediate(resolve));
 assert.equal(stateChanges.filter(change=>change.initial!==null).length,0,'failed connection saves retain the entered form; feedback scope may change');
 assert.equal(actions[0].body.servers[0].password,'','existing server credentials are retained by ID');
 const source=fs.readFileSync(__dirname+'/src/LiveSettings.tsx','utf8');
 assert.match(source,/section\s*===\s*'NZB Settings'\s*&&\s*<NzbSettings/);
 assert.ok(!source.includes('</fieldset><NzbSettings'));
 console.log('NZB settings: isolated speed patch, numeric validation, masked secrets and failed-save retention passed.');
})().catch(e=>{console.error(e);process.exitCode=1;});
