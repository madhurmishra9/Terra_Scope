import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'
import { spawn } from 'child_process'
import { existsSync } from 'fs'
import { join } from 'path'
import { fileURLToPath } from 'url'
import { dirname } from 'path'

const __filename = fileURLToPath(import.meta.url)
const __dirname  = dirname(__filename)

/**
 * backendPlugin — spawns `python -m backend.main` alongside the Vite dev server.
 *
 * Python resolution order:
 *   1. ../.venv/Scripts/python.exe  (Windows venv)
 *   2. ../.venv/bin/python3         (Unix/Mac venv)
 *   3. system `python` / `python3`
 *
 * To skip auto-start (e.g. you launched the backend manually):
 *   TERRASCOPE_NO_BACKEND=1 npm run dev
 */
function backendPlugin() {
  let proc = null

  return {
    name: 'terrascope-backend',

    configureServer(server) {
      if (process.env.TERRASCOPE_NO_BACKEND === '1') {
        console.log('[TerraScope] TERRASCOPE_NO_BACKEND=1 — skipping backend auto-start')
        return
      }

      const rootDir = join(__dirname, '..')
      const isWindows = process.platform === 'win32'

      const venvPython = isWindows
        ? join(rootDir, '.venv', 'Scripts', 'python.exe')
        : join(rootDir, '.venv', 'bin', 'python3')

      const python = existsSync(venvPython) ? venvPython : (isWindows ? 'python' : 'python3')

      console.log(`\n[TerraScope] Starting backend  →  ${python} -m backend.main`)

      proc = spawn(python, ['-m', 'backend.main'], {
        cwd: rootDir,
        stdio: 'inherit',
        shell: false,
      })

      proc.on('error', err => {
        console.error(`[TerraScope] Backend spawn error: ${err.message}`)
      })

      proc.on('exit', code => {
        // code 1 on Windows means port already in use — treat as "already running"
        if (code !== 0 && code !== null) {
          if (code === 1) {
            console.warn('[TerraScope] Backend exited quickly — it may already be running on port 8000')
          } else {
            console.warn(`[TerraScope] Backend exited with code ${code}`)
          }
        }
        proc = null
      })

      const cleanup = () => {
        if (proc && !proc.killed) {
          console.log('\n[TerraScope] Shutting down backend…')
          proc.kill()
          proc = null
        }
      }

      // Kill backend when Vite server closes or process exits
      server.httpServer?.once('close', cleanup)
      process.once('exit',    cleanup)
      process.once('SIGINT',  () => { cleanup(); process.exit(0) })
      process.once('SIGTERM', () => { cleanup(); process.exit(0) })
    },
  }
}

export default defineConfig({
  plugins: [react(), backendPlugin()],
  server: {
    port: 5173,
    proxy: {
      '/api': {
        target: 'http://localhost:8000',
        changeOrigin: true,
        // No proxy timeouts: docgen/curation endpoints make many sequential
        // local-LLM calls and can legitimately run >5 min. A proxyTimeout here
        // destroys the upstream socket mid-request ("socket hang up") while
        // the backend is still working.
        // Retry proxy requests for a few seconds while the backend boots
        configure: (proxy) => {
          proxy.on('error', (_err, _req, res) => {
            if (res && !res.headersSent) {
              res.writeHead(503, { 'Content-Type': 'application/json' })
              res.end(JSON.stringify({ detail: 'Backend starting up — please wait a moment' }))
            }
          })
        },
      }
    }
  }
})
