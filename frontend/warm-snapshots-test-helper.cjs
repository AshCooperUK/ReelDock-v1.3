const fs=require('node:fs'),vm=require('node:vm'),ts=require('typescript');
module.exports=function(){
 const out={};vm.runInNewContext(ts.transpileModule(fs.readFileSync(__dirname+'/src/warmSnapshots.ts','utf8'),{compilerOptions:{target:ts.ScriptTarget.ES2022,module:ts.ModuleKind.CommonJS}}).outputText,{exports:out,structuredClone});
 return out;
};
