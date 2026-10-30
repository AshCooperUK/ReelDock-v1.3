const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const ts = require('typescript');
const exported = {};
vm.runInNewContext(ts.transpileModule(fs.readFileSync(__dirname + '/src/mediaDate.ts', 'utf8'), {
  compilerOptions: { module: ts.ModuleKind.CommonJS }
}).outputText, { exports: exported });
const format = exported.mediaDate;
assert.equal(format('2026-11-26'), '26.11.2026');
assert.equal(format('2026-10-03T23:25:00Z'), '04.10.2026 00:25');
assert.equal(format('2026-12-03T23:25:00Z'), '03.12.2026 23:25');
assert.equal(format('2026-10-04T00:25:00+01:00'), '04.10.2026 00:25');
assert.equal(format('2026-02-30'), '');
assert.equal(format('invalid'), '');
assert.equal(format(null), '');
console.log('UK media dates: date-only, London summer/winter time, midnight rollover and invalid values passed.');

assert.equal(format('2026-10-03T23:25:00Z', false), '04.10.2026');
assert.equal(exported.inputDate('04.10.2026'), '2026-10-04');
assert.equal(exported.inputDate('2026-10-04'), '2026-10-04');
assert.throws(() => exported.inputDate('31.02.2026'));
assert.throws(() => exported.inputDate('10/04/26'));
