export const APPEARANCE_KEY = 'reeldock-appearance-v13';
export const ACCENTS = [
  { value:'blue', label:'Blue' }, { value:'cyan', label:'Cyan' },
  { value:'teal', label:'Teal' }, { value:'green', label:'Green' },
  { value:'lime', label:'Lime' }, { value:'yellow', label:'Yellow' },
  { value:'orange', label:'Orange' }, { value:'red', label:'Red' },
  { value:'pink', label:'Pink' }, { value:'grape', label:'Grape' },
  { value:'violet', label:'Violet' }, { value:'indigo', label:'Indigo' },
] as const;
export type Appearance = { scheme:'dark'|'light'; accent:typeof ACCENTS[number]['value']; background?:boolean };
export const DEFAULT_APPEARANCE:Appearance = { scheme:'dark', accent:'blue' };
export function parseAppearance(raw:string|null):Appearance {
  try {
    const value=JSON.parse(raw||'null');
    if (value && (value.scheme==='dark'||value.scheme==='light') && ACCENTS.some(item=>item.value===value.accent))
      return { scheme:value.scheme, accent:value.accent, ...(typeof value.background==='boolean'?{background:value.background}:{}) };
  } catch { /* A damaged or unsupported preference uses the original default. */ }
  return { ...DEFAULT_APPEARANCE };
}
export function readAppearance():Appearance {
  try { return parseAppearance(localStorage.getItem(APPEARANCE_KEY)); }
  catch { return { ...DEFAULT_APPEARANCE }; }
}
export function saveAppearance(value:Appearance):Appearance {
  const valid=parseAppearance(JSON.stringify(value));
  try { localStorage.setItem(APPEARANCE_KEY,JSON.stringify(valid)); } catch { /* Still applies for this visit. */ }
  return valid;
}
