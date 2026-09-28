import { pathToFileURL } from 'node:url'
import { register } from 'node:module'
register('./loader.mjs', import.meta.url)
