/**
 * Shared path resolution for the UI harnesses (harness / interaction / realbackend /
 * loader hook / render_check).
 *
 * The panel under test is ``desktop/plugin.js`` in THIS repo (two levels up from
 * ``tests/ui/``). React, react-dom and jsdom are never bundled here — they are taken
 * from the first ``node_modules`` that has them:
 *
 *   1. ``$HERMES_AGENT_REPO`` — a Hermes install (its ``node_modules`` ships react)
 *   2. this repo's own ``node_modules`` (``npm install`` in the repo root)
 *   3. ``~/.hermes/hermes-agent``
 *
 * Nothing machine-specific is hardcoded, so a fresh clone on another terminal works
 * after ``npm install`` (or with ``HERMES_AGENT_REPO`` set).
 */
import fs from 'node:fs'
import os from 'node:os'
import path from 'node:path'
import { createRequire } from 'node:module'
import { fileURLToPath } from 'node:url'

export const HERE = path.dirname(fileURLToPath(import.meta.url))
export const REPO_ROOT = path.resolve(HERE, '..', '..')
export const OUT_DIR = path.join(HERE, 'out')
export const FIXTURES_DIR = path.join(HERE, 'fixtures')
export const SDK_STUB = path.join(HERE, 'dt-sdk.mjs')
/** The artefact under test: the desktop panel as it is installed. */
export const PLUGIN = path.join(REPO_ROOT, 'desktop', 'plugin.js')

const candidates = [
  process.env.HERMES_AGENT_REPO,
  REPO_ROOT,
  path.join(os.homedir(), '.hermes', 'hermes-agent'),
].filter(Boolean)

/** A directory whose ``node_modules`` can resolve react / react-dom / jsdom. */
export const REACT_HOST = candidates.find((dir) =>
  fs.existsSync(path.join(dir, 'node_modules', 'react', 'package.json')))

if (!REACT_HOST) {
  throw new Error(
    'No node_modules with react found. Run `npm install` in ' + REPO_ROOT +
    ', or set HERMES_AGENT_REPO to a Hermes install that has node_modules/react ' +
    '(tried: ' + candidates.join(', ') + ')')
}

export const requireFromHost = createRequire(path.join(REACT_HOST, 'package.json'))

/** Load one of ``tests/ui/fixtures/*.json``. */
export function fixture(name) {
  return JSON.parse(fs.readFileSync(path.join(FIXTURES_DIR, name), 'utf8'))
}

/** Absolute path for a harness output file (``tests/ui/out/``, created on demand). */
export function out(name) {
  fs.mkdirSync(OUT_DIR, { recursive: true })
  return path.join(OUT_DIR, name)
}
