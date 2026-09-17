#!/usr/bin/env node
/**
 * Full pipeline: Nintendo AU scrape → parse → Steam match → public/deals.json
 * No AI at runtime. Output is static JSON for Cloudflare Pages.
 */
const { spawnSync } = require('child_process');
const path = require('path');

const ROOT = path.resolve(__dirname, '..');

function run(cmd, args, opts = {}) {
  console.log(`\n>>> ${cmd} ${args.join(' ')}`);
  const r = spawnSync(cmd, args, {
    cwd: ROOT,
    stdio: 'inherit',
    env: process.env,
    ...opts,
  });
  if (r.status !== 0) {
    process.exit(r.status || 1);
  }
}

function pythonBin() {
  if (process.env.PYTHON) return process.env.PYTHON;
  for (const c of ['python3', 'python']) {
    const r = spawnSync(c, ['--version'], { encoding: 'utf8' });
    if (r.status === 0) return c;
  }
  throw new Error('python3 not found');
}

const py = pythonBin();
run('node', [path.join('scripts', 'scrape_pages.js')]);
run(py, [path.join('scripts', 'parse_offers.py')]);
run(py, [path.join('scripts', 'build_deals.py')]);
// DekuDeals AU sale end dates — best-effort; never fail the snapshot.
{
  console.log(`\n>>> ${py} scripts/enrich_dekudeals_ends.py`);
  const r = spawnSync(py, [path.join('scripts', 'enrich_dekudeals_ends.py')], {
    cwd: ROOT,
    stdio: 'inherit',
    env: process.env,
  });
  if (r.status !== 0) {
    console.warn('enrich_dekudeals_ends failed; publishing deals without sale end dates');
  }
}
console.log('\nPipeline complete → public/deals.json');
