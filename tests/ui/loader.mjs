/**
 * ESM loader hook: map the plugin's bare specifiers to the SDK stub and the host's
 * real react. Registered with `node --import`.
 *
 * Run the harnesses from this directory:
 *   node --import ./loader-hook.mjs harness.mjs
 */
import { pathToFileURL } from 'node:url'
import { SDK_STUB, requireFromHost } from './paths.mjs'

const SDK_URL = pathToFileURL(SDK_STUB).href
const REACT_URL = pathToFileURL(requireFromHost.resolve('react')).href
const JSX_URL = pathToFileURL(requireFromHost.resolve('react/jsx-runtime')).href

export function resolve(specifier, context, next) {
  if (specifier === '@hermes/plugin-sdk') return { url: SDK_URL, shortCircuit: true }
  if (specifier === 'react') return { url: REACT_URL, shortCircuit: true }
  if (specifier === 'react/jsx-runtime') return { url: JSX_URL, shortCircuit: true }
  return next(specifier, context)
}
