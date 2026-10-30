const assert=require('node:assert/strict'),fs=require('node:fs'),vm=require('node:vm'),ts=require('typescript');
const jsx=(type,props)=>({type,props:props||{}});
function compile(file,imports,extra={}){const out={};let code=ts.transpileModule(fs.readFileSync(__dirname+'/src/'+file,'utf8'),{compilerOptions:{module:ts.ModuleKind.CommonJS,jsx:ts.JsxEmit.ReactJSX}}).outputText;vm.runInNewContext(code,{exports:out,document:{},window:{dispatchEvent(){}},Event:class{},...extra,require:n=>n==='react/jsx-runtime'?{jsx,jsxs:jsx,Fragment:'Fragment'}:imports[n]||new Proxy({},{get:(_,key)=>key})});return out;}
function nodes(tree){return !tree||typeof tree!=='object'?[]:Array.isArray(tree)?tree.flatMap(nodes):[tree,...nodes(tree.props?.children)];}
const calls=[];let enabled=true;
const react={useState:v=>[v,()=>{}],useRef:v=>({current:v}),useEffect(){}};
const settings=compile('settings/youtube/YouTubeSettings.tsx',{'react':react,'../../SettingsUI':{Group:'Group',Row:'Row',Toggle:'Toggle',Field:'Field',Notice:'Notice',useSettings:url=>({data:url.includes('cookies')?{saved:true}:{use_google_api:enabled,youtube_connected:true,source_message:'Source status'},reload(){}}),useAction:()=>({run:async(url,body)=>{calls.push({url,body});return{ok:true};}})}});
(async()=>{
 let tree=settings.default({csrf:'fixture'}),all=nodes(tree);
 assert.ok(all.some(n=>n.props.title==='Connection'));assert.ok(all.some(n=>n.props.title==='Google connection settings'));
 await all.find(n=>n.props.title==='Use Google API').props.onChange(false);
 assert.equal(calls[0].url,'/api/settings/youtube');assert.equal(calls[0].body.values.use_google_api,false);
 enabled=false;all=nodes(settings.default({csrf:'fixture'}));
 assert.ok(!all.some(n=>n.props.title==='Connection'));assert.ok(!all.some(n=>n.props.title==='Google connection settings'));
 for(const title of ['Use Google API','Pause upload checks','Refresh your guide','YouTube cookies'])assert.ok(all.some(n=>n.props.title===title),title);
 const auth=compile('AuthApp.tsx',{'react':react,'@mantine/hooks':{useMediaQuery:()=>false}});
 const initial={storage:{ready:true,base:'/fixture',roots:['/fixture'],writable:true,free_gb:10,names:{}},admin:true,origin:'https://fixture.test',use_google_api:false,source_ready:true,import_method:'manual',youtube_connected:false,csrf:'fixture',checks:[{name:'Storage',ok:true}]};
 const ready=nodes(auth.default({bootstrap:{page:'setup',version:'fixture',initial}}));
 assert.ok(ready.some(n=>n.props.children==='STEP '||JSON.stringify(n.props.children||'').includes('Open ReelDock')));
 assert.ok(!ready.some(n=>n.props.title==='Connect with Google Cloud'));
 const google=nodes(auth.default({bootstrap:{page:'setup',version:'fixture',initial:{...initial,use_google_api:true}}}));
 assert.ok(google.some(n=>n.props['aria-label']==='Import your channels'));
 const choices=google.filter(n=>n.type==='option').map(n=>n.props.value);assert.deepEqual(choices,['google','cookies','manual']);
 const cookies=nodes(auth.default({bootstrap:{page:'setup',version:'fixture',initial:{...initial,source_ready:false,import_method:'cookies'}}}));assert.ok(cookies.some(n=>n.props.label==='YouTube cookies.txt'));
 const manual=nodes(auth.default({bootstrap:{page:'setup',version:'fixture',initial:{...initial,source_ready:false}}}));assert.ok(manual.some(n=>n.props.label==='Channel links or @nicknames'));assert.ok(google.some(n=>n.props.title==='Connect with Google Cloud'));
 console.log('Optional Google UI: mode toggle, conditional account controls, shared cookies/checks and Google-free setup passed.');
})().catch(e=>{console.error(e);process.exitCode=1;});
