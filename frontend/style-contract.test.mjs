import assert from 'node:assert/strict';
import {readdirSync,readFileSync} from 'node:fs';
import path from 'node:path';
import {createRequire} from 'node:module';
import {fileURLToPath} from 'node:url';
const require=createRequire(import.meta.url),postcss=createRequire(require.resolve('vite'))('postcss');
const src=fileURLToPath(new URL('./src/',import.meta.url));
function files(dir){return readdirSync(dir,{withFileTypes:true}).flatMap(e=>e.isDirectory()?files(path.join(dir,e.name)):[path.join(dir,e.name)]);}
const all=files(src),css=all.filter(f=>f.endsWith('.css'));
for(const file of css){assert.ok(file.endsWith('.module.css')||file===path.join(src,'app/base.css'),'Every feature stylesheet must be a scoped module: '+file);const text=readFileSync(file,'utf8');postcss.parse(text,{from:file});assert.ok(!/reeldock-legacy|var\(--(?:mint|panel|surface|raised|line|muted|text-body|control-gap)\b/.test(text),'No legacy style/token compatibility: '+file);assert.ok(!/nth-child\([^)]*\)[^{]*\{[^}]*display\s*:\s*none/.test(text),'Never hide real cards by item position');}
for(const file of all.filter(f=>/\.tsx?$/.test(f))){const text=readFileSync(file,'utf8');for(const match of text.matchAll(/import\s+(?:[^;\n]*?from\s+)?['"]([^'"]+\.css)['"]/g)){const name=match[1];assert.ok(name==='@mantine/core/styles.css'||name.endsWith('.module.css')||name==='./app/base.css','Legacy/unscoped stylesheet import: '+file+' '+name);}}
const main=readFileSync(path.join(src,'main.tsx'),'utf8');assert.match(main,/<AppearanceProvider>/);const appearance=readFileSync(path.join(src,"app/AppearanceProvider.tsx"),"utf8");assert.match(appearance,/forceColorScheme=\{appearance.scheme\}/);assert.match(main,/@mantine\/core\/styles.css/);
const theme=readFileSync(path.join(src,'mantineTheme.ts'),'utf8');assert.ok(!/colors:|primaryColor:|fontFamily:/.test(theme),'Use the standard Mantine palette and typeface');assert.match(theme,/h:\s*44/);assert.match(theme,/minHeight:\s*44/);assert.match(theme,/\['label','input','description','error'\]/,'Field descriptions follow aligned inputs');
console.log('Fresh Mantine style contract: light/dark preferences, stock palette, consistent fields, scoped geometry only, no legacy CSS or positional content hiding.');
