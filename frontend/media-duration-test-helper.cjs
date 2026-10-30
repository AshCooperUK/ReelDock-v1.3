const fs=require('node:fs'),vm=require('node:vm'),ts=require('typescript');let cache;
module.exports=()=>{if(cache)return cache;cache={};vm.runInNewContext(ts.transpileModule(fs.readFileSync(__dirname+'/src/mediaDuration.ts','utf8'),{compilerOptions:{target:ts.ScriptTarget.ES2022,module:ts.ModuleKind.CommonJS}}).outputText,{exports:cache});return cache;};
