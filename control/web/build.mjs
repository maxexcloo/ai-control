import { build } from 'esbuild';
import { copyFile, mkdir, readFile, rm, writeFile } from 'node:fs/promises';

await rm('dist', { force: true, recursive: true });
await mkdir('dist');
await build({
  bundle: true,
  entryPoints: ['entry.js'],
  format: 'esm',
  legalComments: 'inline',
  minify: true,
  outfile: 'dist/app.js',
  target: ['es2022'],
});
await copyFile('index.html', 'dist/index.html');
await writeFile('dist/THIRD-PARTY-LICENCES.txt', (await Promise.all([
  'node_modules/@awesome.me/webawesome/LICENSE.md',
  'node_modules/@lit/reactive-element/LICENSE',
  'node_modules/lit-html/LICENSE',
  'node_modules/lit-element/LICENSE',
].map((path) => readFile(path, 'utf8')))).join('\n\n'));
