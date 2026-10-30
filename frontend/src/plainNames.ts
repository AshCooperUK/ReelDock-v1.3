const substitutions: Record<string,string> = {'‘':"'",'’':"'",'“':'"','”':'"','–':'-','—':'-','…':'...','ß':'ss','æ':'ae','Æ':'AE','ø':'o','Ø':'O','ł':'l','Ł':'L','œ':'oe'};
export function plainText(value: string): string {
  return value.replace(/[‘’“”–—…ßæÆøØłŁœ]/g, c=>substitutions[c])
    .replace(/[\p{Extended_Pictographic}\p{Regional_Indicator}\uFE0F\u200D\u20E3]/gu,'')
    .normalize('NFKD').replace(/[^\x20-\x7E\n\t]/g,'').replace(/[ \t]+/g,' ').trim();
}
export function plainName(value:string):string {
  let name=plainText(value).replace(/[<>:"/\\|?*]/g,'-').replace(/\s+/g,' ').replace(/^[ .-]+|[ .-]+$/g,'');
  if(/^(CON|PRN|AUX|NUL|COM[0-9]|LPT[0-9])(?:\.|$)/i.test(name))name='_'+name;
  return name||'Untitled';
}
// Restrict cleaning to presentation fields. IDs, paths, credentials and URLs stay exact.
const names=new Set(['name','title','channel_name','group_name','showtitle']);
const prose=new Set(['description','biography','plot']);
export function cleanDisplay<T>(data:T):T {
  if(Array.isArray(data))return data.map(cleanDisplay) as T;
  if(!data||typeof data!=='object')return data;
  return Object.fromEntries(Object.entries(data).map(([key,value])=>[key,
    typeof value==='string'&&names.has(key)?(value?plainName(value):value):
    typeof value==='string'&&prose.has(key)?value.split(/(https?:\/\/[^\s<>]+|www\.[^\s<>]+)/gi).map(part=>/^(https?:\/\/|www\.)/i.test(part)?part:plainText(part)).join(' '):
    typeof value==='object'?cleanDisplay(value):value])) as T;
}
