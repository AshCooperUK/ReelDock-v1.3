const fs=require('node:fs'),vm=require('node:vm'),ts=require('typescript');
let cached;
module.exports=()=>{if(cached)return cached;cached={};vm.runInNewContext(ts.transpileModule(fs.readFileSync(__dirname+'/src/activityClear.ts','utf8'),{compilerOptions:{module:ts.ModuleKind.CommonJS,target:ts.ScriptTarget.ES2022}}).outputText,{exports:cached});return cached;};
