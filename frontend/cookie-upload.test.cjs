const assert=require('node:assert/strict'),fs=require('node:fs'),vm=require('node:vm'),ts=require('typescript');
const jsx=(type,props)=>({type,props:props||{}});
const nodes=tree=>!tree||typeof tree!=='object'?[]:Array.isArray(tree)?tree.flatMap(nodes):[tree,...nodes(tree.props?.children)];
let cursor=0,hooks=[],calls=[],outcomes=[],saved=[],response;
const react={useEffect(){},useState(initial){const i=cursor++;if(!(i in hooks))hooks[i]=initial;return[hooks[i],v=>hooks[i]=typeof v==='function'?v(hooks[i]):v];},useRef(initial){const i=cursor++;return hooks[i]??=( {current:initial} );}};
const settings={useSettings:url=>url==='/api/guide/sync'?{data:{source_message:'Current source'},error:'',reload(){}}:{data:{saved:true},setData:d=>saved.push(d)},useAction:()=>({}),Group:'Group',Field:'Field',Loading:'Loading',Notice:'Notice',Row:'Row',Toggle:'Toggle'};
class FormDataFixture{fields=[];append(...v){this.fields.push(v);}}
const out={};vm.runInNewContext(ts.transpileModule(fs.readFileSync(__dirname+'/src/settings/youtube/YouTubeSettings.tsx','utf8'),{compilerOptions:{module:ts.ModuleKind.CommonJS,jsx:ts.JsxEmit.ReactJSX}}).outputText,{exports:out,Error,FormData:FormDataFixture,fetch:async(url,options)=>{calls.push({url,options});return response();},require:n=>n==='react'?react:n==='react/jsx-runtime'?{jsx,jsxs:jsx,Fragment:'Fragment'}:n==='../../SettingsUI'?settings:n==='../../notifications'?{notify:n=>outcomes.push(n)}:new Proxy({},{get:(_,k)=>k})});
function render(){cursor=0;return nodes(out.default({csrf:'fixture-token'}));}
const button=(tree,label)=>tree.find(n=>n.type==='Button'&&n.props.children===label);
const input=tree=>tree.find(n=>n.type==='FileInput'&&n.props.name==='cookies');
function reset(){cursor=0;hooks=[];calls=[];outcomes=[];saved=[];}
function select(){let tree=render(),field=input(tree),native={value:'selected-cookies.txt'};field.props.resetRef.current=()=>{native.value='';};field.props.onChange({name:'selected-cookies.txt'});return{tree:render(),native};}
(async()=>{
 reset();let {tree,native}=select(),resolve;
 response=()=>new Promise(r=>resolve=r);
 const action=button(tree,'Upload cookies').props.onClick,pending=action();await action();
 assert.equal(calls.length,1,'rapid repeated activation makes one request');
 assert.equal(input(render()).props.disabled,true,'file cannot change during upload');
 assert.equal(native.value,'selected-cookies.txt','selection stays while server is pending');
 assert.equal(calls[0].url,'/api/youtube/cookies');assert.equal(calls[0].options.credentials,'same-origin');assert.equal(calls[0].options.headers['X-CSRF-Token'],'fixture-token');assert.equal(calls[0].options.body.fields[0][0],'cookies');
 resolve({ok:true,json:async()=>({saved:true})});await pending;
 assert.equal(native.value,'');assert.equal(input(render()).props.value,null,'controlled FileInput clears only after a confirmed save');assert.equal(button(render(),'Upload cookies').props.disabled,true);assert.equal(saved.length,1);assert.equal(saved[0].saved,true);assert.equal(outcomes[0].message,'Cookies saved and applied.');
 for(const failure of [()=>Promise.resolve({ok:false,json:async()=>({error:'Invalid cookie format.'})}),()=>Promise.resolve({ok:true,json:async()=>({saved:false})}),()=>Promise.resolve({ok:true,json:async()=>{throw Error('Invalid JSON.');}}),()=>Promise.reject(Error('Network unavailable.'))]){
  reset();({tree,native}=select());response=failure;await button(tree,'Upload cookies').props.onClick();
  assert.equal(native.value,'selected-cookies.txt','failure preserves native selection');assert.equal(input(render()).props.value.name,'selected-cookies.txt','controlled picker retains a failed upload for retry');assert.equal(button(render(),'Upload cookies').props.disabled,false,'retry is enabled');assert.equal(saved.length,0);assert.equal(outcomes.length,1,'failure is published even if the form has closed');assert.equal(outcomes[0].kind,'error','failure never confirms success');assert.ok(render().some(n=>n.type==='Notice'&&n.props.error&&n.props.text),'error goes through shared notification contract');
 }
 reset();({tree,native}=select());response=async()=>({ok:true,json:async()=>({saved:true})});await button(tree,'Remove cookies').props.onClick();assert.equal(native.value,'selected-cookies.txt');assert.equal(outcomes[0].kind,'error','unconfirmed removal reports failure');
 response=async()=>({ok:true,json:async()=>({saved:false})});await button(render(),'Remove cookies').props.onClick();assert.equal(native.value,'');assert.equal(saved[0].saved,false);assert.equal(outcomes.at(-1).message,'Cookies removed.');
 console.log('Cookie upload: confirmed save/removal, native reset, CSRF, rapid-click gate, and HTTP/unconfirmed/JSON/network failure retention passed.');
})().catch(e=>{console.error(e);process.exitCode=1});
