// Load the real shared control in CommonJS VM tests without importing app data.
const fs=require('node:fs'),vm=require('node:vm'),ts=require('typescript');
let cached;
module.exports=function selectFieldModule(){
 if(cached)return cached;
 const out={};
 const code=ts.transpileModule(fs.readFileSync(__dirname+'/src/components/SelectField.tsx','utf8'),{compilerOptions:{module:ts.ModuleKind.CommonJS,jsx:ts.JsxEmit.ReactJSX,esModuleInterop:true}}).outputText;
 vm.runInNewContext(code,{exports:out,require,Event,Error,console});
 cached=out;return out;
};
