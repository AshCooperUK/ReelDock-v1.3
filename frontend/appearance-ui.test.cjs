const assert=require('node:assert/strict'),fs=require('node:fs'),path=require('node:path'),vm=require('node:vm'),ts=require('typescript');
const React=require('react'),Mantine=require('@mantine/core'),{renderToStaticMarkup}=require('react-dom/server');
function load(file,imports={},globals={}){const result={};vm.runInNewContext(ts.transpileModule(fs.readFileSync(path.join(__dirname,'src',file),'utf8'),{compilerOptions:{module:ts.ModuleKind.CommonJS,jsx:ts.JsxEmit.ReactJSX}}).outputText,{exports:result,...globals,require:name=>imports[name]||require(name)});return result;}
const saved=new Map(),storage={getItem:key=>saved.get(key)||null,setItem:(key,value)=>saved.set(key,value)};
const preferences=load('app/appearance.ts',{}, {localStorage:storage});
const {ACCENTS,APPEARANCE_KEY}=preferences;
assert.equal(ACCENTS.length,12);assert.equal(new Set(ACCENTS.map(accent=>accent.value)).size,12);
const theme=load('mantineTheme.ts').mantineTheme,layout=load('components/layout.tsx');
let appearance=preferences.readAppearance(),changes=[];
const setAppearance=value=>{changes.push(value);appearance=preferences.saveAppearance(value);};
const Appearance=load('settings/SettingsAppearance.tsx',{'../components/layout':layout,'../app/appearance':preferences,'../app/AppearanceProvider':{useAppearance:()=>({appearance,setAppearance})}}).default;
const nodes=node=>!node||typeof node!=='object'?[]:Array.isArray(node)?node.flatMap(nodes):[node,...nodes(node.props?.children)];
function render(width){return renderToStaticMarkup(React.createElement(Mantine.MantineProvider,{theme:{...theme,primaryColor:appearance.accent},forceColorScheme:appearance.scheme},React.createElement('div',{style:{width,maxWidth:'100%'}},React.createElement(Appearance))));}
for(const scheme of ['light','dark'])for(const accent of ACCENTS){
 appearance={scheme,accent:accent.value};
 for(const width of [320,768,1440]){
  const html=render(width),cards=(html.match(/<button\b[^>]*aria-label="Use [^"]+ palette"[^>]*>/g)||[]);
  assert.equal(cards.length,12,scheme+' '+accent.value+' '+width+' offers every palette');
  assert.equal(cards.filter(card=>card.includes('aria-pressed="true"')).length,1);
  assert.ok(cards.find(card=>card.includes('Use '+accent.label+' palette')).includes('aria-pressed="true"'));
  assert.ok(cards.every(card=>/type="button"/.test(card)&&/min-height:calc\(5rem/.test(card)),'palette targets are keyboard-native and at least80px high');
  assert.equal((html.match(/>Selected<\/p>/g)||[]).length,1,'selection is explicit beyond colour');
  assert.match(html,/role="radiogroup"[^>]*aria-label="Colour mode"|aria-label="Colour mode"[^>]*role="radiogroup"/);
  const radios=(html.match(/<input\b[^>]*type="radio"[^>]*>/g)||[]).filter(tag=>tag.includes('name="appearance-scheme"'));
  assert.equal(radios.length,2);assert.equal(radios.filter(tag=>tag.includes('checked=""')).length,1);assert.ok(radios.find(tag=>tag.includes('value="'+scheme+'"')).includes('checked=""'));
  assert.match(html,/min-height:44px/,'scheme labels retain full44px targets');
  assert.match(html,/--sg-cols:1/);assert.match(html,/--sg-cols:2/,'palette grid grows from one phone column to two');
  assert.match(html,new RegExp('var\\(--mantine-color-'+accent.value+'-6\\)'),'previews use actual standard Mantine palette tokens');
 }
}
// Every visible selection goes through the shared provider contract; retain the
// other setting and verify the actual origin-local persistence helper round-trip.
for(const scheme of ['light','dark']){
 appearance={scheme:scheme==='light'?'dark':'light',accent:'blue'};
 const mode=nodes(Appearance()).find(node=>node.type===Mantine.SegmentedControl);
 mode.props.onChange(scheme);assert.equal(appearance.scheme,scheme);assert.equal(appearance.accent,'blue');
 for(const accent of ACCENTS){
  const button=nodes(Appearance()).find(node=>node.props['aria-label']==='Use '+accent.label+' palette');
  button.props.onClick();assert.deepEqual(JSON.parse(JSON.stringify(appearance)),{scheme,accent:accent.value});assert.deepEqual(JSON.parse(saved.get(APPEARANCE_KEY)),{scheme,accent:accent.value});assert.deepEqual(JSON.parse(JSON.stringify(preferences.readAppearance())),{scheme,accent:accent.value});
 }
}
// Background is independent of palette and mode, and persists through both.
for(const scheme of ['light','dark']){
 appearance={scheme,accent:'violet'};
 let toggle=nodes(Appearance()).find(node=>node.type===Mantine.Switch&&node.props.label==='Background image');
 assert.equal(toggle.props.checked,true,'existing saved themes default to a matching background');
 toggle.props.onChange({currentTarget:{checked:false}});assert.equal(appearance.background,false);assert.equal(preferences.readAppearance().background,false);
 nodes(Appearance()).find(node=>node.props['aria-label']==='Use Teal palette').props.onClick();assert.equal(appearance.background,false);assert.equal(appearance.accent,'teal');
 nodes(Appearance()).find(node=>node.type===Mantine.SegmentedControl).props.onChange(scheme==='dark'?'light':'dark');assert.equal(appearance.background,false,'changing mode keeps the background choice');
 toggle=nodes(Appearance()).find(node=>node.type===Mantine.Switch&&node.props.label==='Background image');assert.equal(toggle.props.checked,false);
 toggle.props.onChange({currentTarget:{checked:true}});assert.equal(preferences.readAppearance().background,true);
 const html=render(320);assert.match(html,/role="switch"/);assert.match(html,/Background image/);assert.match(html,/min-height:44px/);
}
const before=changes.length;nodes(Appearance()).find(node=>node.type===Mantine.SegmentedControl).props.onChange('unsupported');assert.equal(changes.length,before);
const source=fs.readFileSync(path.join(__dirname,'src/LiveSettings.tsx'),'utf8');assert.match(source,/section==='Appearance'&&<SettingsAppearance\/>/);assert.doesNotMatch(source,/onTheme|theme:string|One Mantine dark theme/);
assert.match(fs.readFileSync(path.join(__dirname,'src/AuthApp.tsx'),'utf8'),/appearance preference is saved in this browser for this ReelDock address/);
console.log('Appearance UI: all24 light/dark palettes, actual responsive Mantine controls, keyboard/pressed states,44px targets and shared preference persistence callbacks passed.');
