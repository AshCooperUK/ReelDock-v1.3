const assert=require('node:assert/strict'),fs=require('node:fs'),vm=require('node:vm'),ts=require('typescript');
const jsx=(type,props)=>({type,props:props||{}});
function compile(file,imports){const out={};const code=ts.transpileModule(fs.readFileSync(__dirname+'/src/'+file,'utf8'),{compilerOptions:{module:ts.ModuleKind.CommonJS,jsx:ts.JsxEmit.ReactJSX}}).outputText;vm.runInNewContext(code,{exports:out,encodeURIComponent,window:{dispatchEvent(){}},Event:class {},require:n=>n==='react/jsx-runtime'?{jsx,jsxs:jsx,Fragment:'Fragment'}:imports[n]||new Proxy({},{get:(_,key)=>key})});return out;}
function nodes(tree){return !tree||typeof tree!=='object'?[]:Array.isArray(tree)?tree.flatMap(nodes):[tree,...nodes(tree.props?.children)];}
(async()=>{
 const calls=[],saved=[],notices=[];
 const ui={Group:'Group',Row:'Row',Toggle:'Toggle',Loading:'Loading',Notice:'Notice',useSettings:()=>({data:{options:{pause_downloads:false},metadata_type:'emby'},setData:v=>saved.push(v),reload(){}}),useAction:()=>({run:async(url,body)=>{calls.push({url,body});return {message:'All stopped.',acknowledged:true};},setNotice:value=>notices.push(value)})};
 const controls=compile('settings/DownloadControls.tsx',{'../SettingsUI':ui,'../components/ui':{Button:'Button'}}).default({csrf:'token'});
 await nodes(controls).find(n=>n.props.title==='Pause downloads').props.onChange(true);
 assert.equal(calls[0].body.options.pause_downloads,true);
 assert.ok(!nodes(controls).some(n=>n.props.title==='Metadata format'));
 assert.equal(nodes(controls).filter(n=>n.props.children==='Cancel all downloads').length,1);
 const format=compile('settings/MetadataFormat.tsx',{'../SettingsUI':ui}).default({csrf:'token'});
 await nodes(format).find(n=>n.props['aria-label']==='Metadata format').props.onChange({target:{value:'kodi'}});
 assert.equal(calls[1].body.action,'metadata');assert.equal(calls[1].body.metadata_type,'kodi');
 assert.equal(saved[1].metadata_type,'kodi');
 await nodes(controls).find(n=>n.props.children==='Cancel all downloads').props.onClick();
 assert.equal(calls[2].body.action,'cancel_all');assert.equal(notices[0],'All stopped.');
 const shell=fs.readFileSync(__dirname+'/src/LiveSettings.tsx','utf8');
 assert.ok(!shell.includes("['Downloads',CloudDownload]"));
 assert.match(shell,/section\s*===\s*'General'[\s\S]*?<DownloadControls csrf=/);
 assert.ok(!shell.includes("['Folders',"));
 assert.match(shell,/\['Downloads',\s*'Folders'\]\.includes\(initial\)\s*\?\s*'General'/);
 const metadata=fs.readFileSync(__dirname+'/src/settings/MetadataSettings.tsx','utf8');
 assert.match(metadata,/<MetadataFormat csrf=/);
 console.log('Settings organisation: General download controls, Metadata format and retired category routing passed.');
})().catch(e=>{console.error(e);process.exitCode=1;});
