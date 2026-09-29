import assert from 'node:assert/strict'
import { existsSync, readFileSync } from 'node:fs'
import { fileURLToPath } from 'node:url'
import { dirname, resolve } from 'node:path'
import { withApiBase } from '../src/api/url.ts'

const root = resolve(dirname(fileURLToPath(import.meta.url)), '..')
const read = (path: string) => readFileSync(resolve(root, path), 'utf8')

assert.equal(withApiBase('/api/ping', ''), '/api/ping')
assert.equal(
  withApiBase('/api/ping', 'https://race-lens.onrender.com/'),
  'https://race-lens.onrender.com/api/ping',
)
assert.throws(() => withApiBase('https://example.com/api/ping'))

const config = JSON.parse(read('src-tauri/tauri.conf.json'))
const packageManifest = JSON.parse(read('package.json'))
const packageLock = JSON.parse(read('package-lock.json'))
const cargoManifest = read('src-tauri/Cargo.toml')
const cargoLock = read('src-tauri/Cargo.lock')
const cargoVersion = cargoManifest.match(/^version = "([^"]+)"$/m)?.[1]
const readCargoLockVersion = (contents: string) => contents
  .replaceAll('\r\n', '\n')
  .match(/\[\[package\]\]\nname = "race-lens"\nversion = "([^"]+)"/)?.[1]
const cargoLockVersion = readCargoLockVersion(cargoLock)
const releaseTag = process.env.DESKTOP_RELEASE_VERSION
const expectedVersion = releaseTag?.replace(/^desktop-v/, '') ?? config.version

assert.match(expectedVersion, /^\d+\.\d+\.\d+$/)
assert.equal(readCargoLockVersion(cargoLock.replaceAll('\n', '\r\n')), cargoLockVersion)
for (const [source, version] of Object.entries({
  package: packageManifest.version,
  packageLock: packageLock.version,
  packageLockRoot: packageLock.packages[''].version,
  cargo: cargoVersion,
  cargoLock: cargoLockVersion,
  tauri: config.version,
})) assert.equal(version, expectedVersion, `${source} version must match ${expectedVersion}`)

assert.deepEqual(config.bundle.targets, ['nsis'])
assert.equal(config.build.beforeBuildCommand, 'npm run build:desktop')
assert.equal(existsSync(resolve(root, 'src-tauri/icons/icon.ico')), true, 'Windows app icon exists')
assert.match(config.app.security.csp, /https:\/\/race-lens\.onrender\.com/)
assert.match(config.app.security.csp, /media-src 'self' https:\/\/livetiming\.formula1\.com/)

const api = read('../backend/racelens/api.py')
for (const origin of [
  'https://race-lens.onrender.com',
  'http://tauri.localhost',
  'tauri://localhost',
  'http://localhost:5173',
]) assert.match(api, new RegExp(`"${origin.replaceAll('.', '\\.')}"`))
assert.doesNotMatch(api, /allow_origins=\["\*"\]/)

const workflow = read('../.github/workflows/desktop-release.yml')
assert.match(workflow, /desktop-v\*/)
assert.match(workflow, /windows-latest/)
assert.match(workflow, /node-version:\s*"22"/)
assert.match(workflow, /x86_64-pc-windows-msvc/)
assert.match(workflow, /DESKTOP_RELEASE_VERSION:\s*\$\{\{ github\.ref_name \}\}/)

console.log('Desktop URL, CORS, Tauri, and release config check passed')
