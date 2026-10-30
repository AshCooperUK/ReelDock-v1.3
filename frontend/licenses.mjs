import { readdir, readFile, realpath, writeFile } from 'node:fs/promises';
import { createRequire } from 'node:module';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const root = path.dirname(fileURLToPath(import.meta.url));

async function packageAt(file) {
  const resolved = await realpath(file);
  return { file: resolved, dir: path.dirname(resolved), pkg: JSON.parse(await readFile(resolved, 'utf8')) };
}

async function resolvePackage(name, parent) {
  // Use Node's nearest dependency search paths. Reading the manifest directly
  // also works for packages that intentionally hide package.json in exports.
  const resolver = createRequire(path.join(parent, 'package.json'));
  for (const modules of resolver.resolve.paths(name) || []) {
    try { return await packageAt(path.join(modules, name, 'package.json')); }
    catch (error) { if (error.code !== 'ENOENT' && error.code !== 'ENOTDIR') throw error; }
  }
  return null;
}

function requirements(pkg, includeDevelopment = false) {
  const required = new Map(Object.keys(pkg.dependencies || {}).map(name => [name, true]));
  if (includeDevelopment) for (const name of Object.keys(pkg.devDependencies || {})) required.set(name, true);
  for (const name of Object.keys(pkg.optionalDependencies || {})) required.set(name, false);
  // Installed peers are part of the actual resolved graph. Optional/unprovided
  // peers are not dependencies that this build can distribute.
  for (const name of Object.keys(pkg.peerDependencies || {})) if (!required.has(name)) required.set(name, false);
  for (const name of Array.isArray(pkg.bundledDependencies) ? pkg.bundledDependencies : Array.isArray(pkg.bundleDependencies) ? pkg.bundleDependencies : []) if (!required.has(name)) required.set(name, true);
  return required;
}

function licenceName(pkg) {
  if (typeof pkg.license === 'string') return pkg.license;
  if (pkg.license?.type) return pkg.license.type;
  if (Array.isArray(pkg.licenses)) return pkg.licenses.map(value => typeof value === 'string' ? value : value.type).filter(Boolean).join(' OR ');
  return 'See project licence notices';
}

export async function collectLicenses(projectRoot) {
  const project = await packageAt(path.join(projectRoot, 'package.json'));
  const entries = new Map(), visited = new Set();
  async function visit(parent, dependencies) {
    for (const [name, required] of dependencies) {
      const dependency = await resolvePackage(name, parent);
      if (!dependency) {
        if (required) throw new Error(`Missing installed dependency ${name} required by ${parent}`);
        continue;
      }
      if (visited.has(dependency.file)) continue;
      visited.add(dependency.file);
      const { pkg, dir } = dependency;
      if (!pkg.name || !pkg.version) throw new Error(`Dependency manifest has no name/version: ${dependency.file}`);
      const key = `${pkg.name}@${pkg.version}`;
      if (!entries.has(key)) {
        const notices = [];
        for (const file of (await readdir(dir)).sort()) {
          if (/^(licen[cs]e|copying|notice)([.-]|$)/i.test(file)) {
            try { notices.push(`${file}\n\n${await readFile(path.join(dir, file), 'utf8')}`); }
            catch (error) { if (error.code !== 'EISDIR') throw error; }
          }
        }
        entries.set(key, { name: pkg.name, version: pkg.version, licence: licenceName(pkg),
          url: `https://www.npmjs.com/package/${pkg.name}/v/${pkg.version}`,
          notices: notices.join('\n\n----------------------------------------\n\n') });
      }
      // The same version can have multiple peer contexts; traverse each real
      // location even though its licence notice is emitted only once.
      await visit(dir, requirements(pkg));
    }
  }
  await visit(project.dir, requirements(project.pkg, true));
  return [...entries.values()].sort((a, b) => a.name.localeCompare(b.name) || a.version.localeCompare(b.version));
}

export function validateCoreNotices(entries) {
  for (const name of ['@mantine/core', '@mantine/hooks', 'react', 'react-dom']) {
    if (!entries.some(pkg => pkg.name === name && pkg.notices)) throw new Error(`${name} licence notice missing`);
  }
}

if (process.argv[1] && path.resolve(process.argv[1]) === fileURLToPath(import.meta.url)) {
  const result = await collectLicenses(root);
  validateCoreNotices(result);
  await writeFile(path.join(root, 'dist/third-party-notices.json'), JSON.stringify(result, null, 2));
  console.log(`Bundled licence notices for ${result.length} reachable frontend and build packages.`);
}
