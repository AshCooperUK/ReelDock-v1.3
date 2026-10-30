import assert from 'node:assert/strict';
import { mkdtemp, mkdir, writeFile, rm } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { collectLicenses, validateCoreNotices } from './licenses.mjs';

const fixture = await mkdtemp(path.join(tmpdir(), 'reeldock-license-graph-'));
async function manifest(dir, pkg) {
  await mkdir(dir, { recursive: true });
  await writeFile(path.join(dir, 'package.json'), JSON.stringify(pkg));
  await writeFile(path.join(dir, 'LICENSE'), 'Fixture licence for ' + pkg.name);
}
try {
  await manifest(fixture, { name: 'fixture', version: '1', dependencies: { parent: '1', child: '2' }, devDependencies: { builder: '1' } });
  const modules = path.join(fixture, 'node_modules');
  await manifest(path.join(modules, 'parent'), { name: 'parent', version: '1', dependencies: { child: '1' }, optionalDependencies: { absent: '1' }, peerDependencies: { unprovided: '1' } });
  await manifest(path.join(modules, 'parent/node_modules/child'), { name: 'child', version: '1', dependencies: { parent: '1' }, exports: { '.': './hidden.js' } });
  await manifest(path.join(modules, 'child'), { name: 'child', version: '2' });
  await manifest(path.join(modules, 'builder'), { name: 'builder', version: '1', license: { type: 'MIT' } });
  await manifest(path.join(modules, '.pnpm/obsolete/node_modules/obsolete'), { name: 'obsolete', version: '1' });
  await manifest(path.join(modules, 'undeclared'), { name: 'undeclared', version: '1' });
  const notices = await collectLicenses(fixture);
  assert.equal(notices.map(pkg => pkg.name + '@' + pkg.version).join('|'), 'builder@1|child@1|child@2|parent@1', 'include root build deps and actual nearest versions, exclude undeclared/stale store packages');
  assert.ok(notices.every(pkg => pkg.notices.includes('Fixture licence')), 'read notices even when package.json is hidden by package exports');
  assert.equal(notices.find(pkg => pkg.name === 'builder').licence, 'MIT');
  await manifest(fixture, { name: 'fixture', version: '1', dependencies: { missingRequired: '1' } });
  await assert.rejects(collectLicenses(fixture), /Missing installed dependency missingRequired/, 'missing mandatory packages fail the notice build');
} finally {
  await rm(fixture, { recursive: true, force: true });
}

const real = await collectLicenses(path.dirname(fileURLToPath(import.meta.url)));
validateCoreNotices(real);
for (const name of ['@mantine/core', '@mantine/hooks', 'react', 'react-dom', 'lucide-react', 'vite', 'typescript']) assert.ok(real.some(pkg => pkg.name === name && pkg.notices), name + ' retains its actual licence notice');
assert.ok(real.every(pkg => !/^@radix-ui\//.test(pkg.name) && !/^@tailwindcss\//.test(pkg.name) && !['tailwindcss', 'class-variance-authority'].includes(pkg.name)), 'removed styling stacks are absent from the reachable notice graph');
assert.throws(() => validateCoreNotices(real.filter(pkg => pkg.name !== 'react')), /react licence notice missing/);
console.log(`Licence graph: ${real.length} reachable frontend/build packages, required Mantine/React notices present, stale packages excluded.`);
