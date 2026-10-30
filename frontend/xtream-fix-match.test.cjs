const assert=require('node:assert/strict'),fs=require('node:fs'),vm=require('node:vm'),ts=require('typescript');
const jsx=(type,props)=>({type,props:props||{}});
function compile(file,imports){const out={};const code=ts.transpileModule(fs.readFileSync(__dirname+'/src/'+file,'utf8'),{compilerOptions:{module:ts.ModuleKind.CommonJS,jsx:ts.JsxEmit.ReactJSX}}).outputText;vm.runInNewContext(code,{exports:out,URLSearchParams,require:n=>n==='react/jsx-runtime'?{jsx,jsxs:jsx,Fragment:'Fragment'}:imports[n]||new Proxy({},{get:(_,key)=>key})});return out;}
function nodes(tree){return !tree||typeof tree!=='object'?[]:Array.isArray(tree)?tree.flatMap(nodes):[tree,...nodes(tree.props?.children)];}
const stateUpdates=[];
const react={useState:v=>[v,next=>stateUpdates.push(next)],useEffect(){},useRef:v=>({current:v})};
// Both source types are nested under the single VOD category.
const settings=fs.readFileSync(__dirname+'/src/LiveSettings.tsx','utf8');
assert.match(settings,/\['VOD Settings',\s*ListVideo\]/);
assert.ok(!settings.includes("['Xtream',"));
const vod=compile('settings/m3u/VodSettings.tsx',{react,'../useEngineSettings':{useEngineSettings:()=>({values:{min_download_bytes:0,max_download_bytes:100000000000},save(){}})},'../../SettingsUI':{useSettings:()=>({data:{items:[],defaults:{}},reload(){}}),useAction:()=>({})}});
const vodDefault=nodes(vod.default({csrf:'fixture'}));
assert.equal(vodDefault.filter(n=>n.type==='Button'&&n.props.children==='Add connection').length,1);
assert.ok(!vodDefault.some(n=>n.props['aria-label']==='VOD settings sections'));
const connection=vod.AddConnectionForm({csrf:'fixture',defaults:{},onDone(){}});
const connectionType=nodes(connection.props.connectionChoice).find(n=>n.props['aria-label']==='Connection type');
assert.ok(connectionType,'connection type uses the shared Mantine dropdown');
assert.deepEqual(nodes(connectionType).filter(n=>n.type==='option').map(n=>n.props.value),['m3u','xtream']);
connectionType.props.onChange({target:{value:'xtream'}});
assert.equal(stateUpdates.at(-1),'xtream','native selection retains the source-type state transition');
// The login form never pre-fills the password, masks it and keeps the server URL visible.
const calls=[];
const xtream=compile('settings/m3u/XtreamForm.tsx',{react,'../../SettingsUI':{Field:'Field',Group:'Group',Help:'Help',Loading:'Loading',Notice:'Notice',Row:'Row',Toggle:'Toggle',useSettings:()=>({data:{items:[]},reload(){}}),useAction:()=>({busy:false,error:'',notice:'',run:async(url,body)=>{calls.push({url,body});return{message:'Connected.'};}})}});
const form=nodes(xtream.XtreamForm({csrf:'t',login:{id:'abc',name:'P',state:'idle',message:'',server:'http://tv.example',username:'user',password_saved:true,enabled:true,hours:24},onDone(){}}));
const inputs=form.filter(n=>n.type==='TextInput');
const password=inputs.find(n=>n.props.type==='password');
assert.equal(password.props.value,'');assert.equal(password.props.required,false);
assert.equal(inputs.find(n=>n.props.type==='url').props.value,'http://tv.example');
assert.ok(form.some(n=>n.type==='Toggle'&&n.props.title==='Enabled'));
(async()=>{
 await form.find(n=>n.type==='Button'&&n.props.children==='Test connection').props.onClick();
 assert.deepEqual(JSON.parse(JSON.stringify(calls[0])),{url:'/api/xtream',body:{id:'abc',name:'P',server:'http://tv.example',username:'user',password:'',enabled:true,action:'test'}});
 // Fix match describes lock state in plain words.
 const fix=compile('FixMatch.tsx',{react});
 assert.match(fix.matchSummary({locked:false,mode:'',data:{},tmdb_saved:false}),/Basic local metadata needs no login/);assert.doesNotMatch(fix.matchSummary({locked:false,mode:'',data:{},tmdb_saved:true}),/Matched automatically/);
 assert.match(fix.matchSummary({locked:true,mode:'tmdb',data:{title:'Right'}}),/Matched by you to “Right”. Automatic matching is locked./);
 assert.match(fix.matchSummary({locked:true,mode:'manual',data:{}}),/own details/);
 const library=fs.readFileSync(__dirname+'/src/M3uLibrary.tsx','utf8');
 // Fix match stays a player control, but it is an in-sheet view inside the
 // M3U DetailSheet — never a second stacked dialog.
 assert.match(library,/>Fix match<\/Button>/);assert.match(library,/<FixMatch /);assert.ok(!library.includes('<Dialog'));
 console.log('Xtream settings and Fix match checks passed.');
})().catch(e=>{console.error(e);process.exit(1);});
