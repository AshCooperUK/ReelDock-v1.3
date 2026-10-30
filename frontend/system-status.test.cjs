const assert=require('node:assert/strict'),fs=require('node:fs'),vm=require('node:vm'),ts=require('typescript');
const React=require('react'),{renderToStaticMarkup}=require('react-dom/server'),{MantineProvider}=require('@mantine/core');
const layout={};
vm.runInNewContext(ts.transpileModule(fs.readFileSync(__dirname+'/src/components/layout.tsx','utf8'),{compilerOptions:{module:ts.ModuleKind.CommonJS,jsx:ts.JsxEmit.ReactJSX}}).outputText,{exports:layout,require});
const output={};
vm.runInNewContext(ts.transpileModule(fs.readFileSync(__dirname+'/src/dashboard/SystemStatus.tsx','utf8'),{compilerOptions:{module:ts.ModuleKind.CommonJS,jsx:ts.JsxEmit.ReactJSX,esModuleInterop:true}}).outputText,{
 exports:output,require:name=>{
  if(['react','react/jsx-runtime','@mantine/core'].includes(name))return require(name);
  if(name==='lucide-react')return{HardDrive:()=>null};
  if(name==='../components/layout')return layout;
  return{__esModule:true,default:()=>null,TorrentProgress:()=>null,NzbProgress:()=>null,YoutubeDownloadProgress:()=>null,VodDownloadProgress:()=>null};
 }
});
const ready={csrf:'fixture',overview:{media_disk:{total:2e12,used:5e11,free:1.5e12},storage_disks:[
 {id:'system',label:'Application data',paths:['/data'],total:2e12,used:5e11,free:1.5e12},
 {id:'media',label:'Media',paths:['/Media Drive/TV Shows','/Media Drive/alias'],total:8e12,used:3e12,free:5e12},
]}};
const render=()=>renderToStaticMarkup(React.createElement(MantineProvider,{forceColorScheme:'dark'},React.createElement(output.default,{ready})));
let html=render();
assert.doesNotMatch(html,/Storage configuration/,'healthy configuration adds no warning');
assert.match(html,/Application data/);assert.match(html,/5.0 TB free/);assert.match(html,/8.0 TB total/);
assert.match(html,/\/Media Drive\/TV Shows · \/Media Drive\/alias/,'aliases share one filesystem row');
assert.equal((html.match(/role="progressbar"/g)||[]).length,2,'both distinct filesystems have a capacity indicator');
assert.match(html,/aria-label="Application data \(\/data\) storage used"/);
assert.match(html,/aria-label="Media \(\/Media Drive\/TV Shows\) storage used"/,'each indicator has a distinct accessible name');
ready.overview.storage_disks.push({id:'missing',label:'Media',paths:['/Media Drive/offline'],total:null,used:null,free:null});
ready.overview.storage_disks_incomplete=true;html=render();
assert.match(html,/Storage information is unavailable for this location/);assert.match(html,/Some mounted storage locations could not be listed/);
assert.match(html,/5.0 TB free/,'unavailable storage does not hide healthy filesystems');
assert.equal((html.match(/role="progressbar"/g)||[]).length,2,'missing capacity never appears as a zero-size drive');
ready.overview.storage_configuration_warnings=['REELDOCK_STORAGE_ROOTS must be a JSON array of dedicated absolute mount paths. Invalid additional locations were disabled.'];
html=render();assert.match(html,/role="alert"/);assert.match(html,/Storage configuration/);assert.match(html,/Invalid additional locations were disabled/,'the sanitized actionable server diagnostic is visible');
ready.overview.storage_configuration_warnings=[];assert.doesNotMatch(render(),/Storage configuration/,'corrected configuration clears the warning');
delete ready.overview.storage_disks;delete ready.overview.storage_disks_incomplete;
assert.match(render(),/1.5 TB free/,'older DTOs retain their media capacity');
ready.overview.media_disk=null;assert.match(render(),/Storage information is unavailable/);
console.log('System status storage: distinct filesystem capacities, merged aliases, accessible names, partial failure and legacy DTO fallback pass.');
