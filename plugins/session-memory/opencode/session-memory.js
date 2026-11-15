// OpenCode side of the session-memory plugin: OpenCode runs no Claude Code hooks, so this
// file gives sessmem.py its two moments. Copy or link it into ~/.config/opencode/plugins/.
import { execFileSync, spawn } from "node:child_process"
import { existsSync, readdirSync, realpathSync } from "node:fs"
import { homedir } from "node:os"
import { dirname, join } from "node:path"
import { fileURLToPath } from "node:url"

const PYTHON = process.platform === "win32" ? "python" : "python3"
const EXPORT_EVERY_MS = 10 * 60 * 1000

// sessmem.py: SESSMEM_SCRIPT, else beside this file's real location, else the installed Claude Code plugin.
function findScript() {
  const beside = join(dirname(realpathSync(fileURLToPath(import.meta.url))), "..", "hooks", "sessmem.py")
  const cache = join(homedir(), ".claude", "plugins", "cache", "hadi-plugins", "session-memory")
  const installed = existsSync(cache)
    ? readdirSync(cache).sort().map((version) => join(cache, version, "hooks", "sessmem.py"))
    : []
  return [process.env.SESSMEM_SCRIPT, beside, ...installed.reverse()].find((path) => path && existsSync(path))
}

export const SessionMemory = async ({ directory }) => {
  const script = findScript()
  if (!script) return {}
  const env = { ...process.env, SESSMEM_OPENCODE: process.execPath }
  const exportedAt = new Map() // session id -> time of its last export
  const unexported = new Set() // sessions with turns since their last export

  const exportSession = (id) => {
    exportedAt.set(id, Date.now())
    unexported.delete(id)
    spawn(PYTHON, [script, "opencode-export", id], { env, detached: true, stdio: "ignore" }).unref()
  }

  let context = ""
  try {
    context = execFileSync(PYTHON, [script, "opencode-context", directory], { env, encoding: "utf8", timeout: 15000 })
  } catch {}

  return {
    // A session is exported when it goes quiet (at most every ten minutes), when it compacts, and at exit.
    event: async ({ event }) => {
      const id = event.properties?.sessionID
      if (event.type === "session.compacted") exportSession(id)
      if (event.type !== "session.idle") return
      if (Date.now() - (exportedAt.get(id) ?? 0) > EXPORT_EVERY_MS) exportSession(id)
      else unexported.add(id)
    },
    dispose: async () => {
      for (const id of [...unexported]) exportSession(id)
    },
    "experimental.chat.system.transform": async (_input, output) => {
      if (context.trim()) output.system.push(context)
    },
  }
}
