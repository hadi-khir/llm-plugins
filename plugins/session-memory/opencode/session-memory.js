// OpenCode side of the session-memory plugin: OpenCode runs no Claude Code hooks, so this
// file gives sessmem.py its two moments. Copy or link it into ~/.config/opencode/plugins/.
// The default export serves both plugin APIs: OpenCode 2 calls setup(), OpenCode 1 calls server().
import { execFileSync, spawn } from "node:child_process"
import { existsSync, readdirSync, realpathSync } from "node:fs"
import { homedir } from "node:os"
import { dirname, join } from "node:path"
import { fileURLToPath } from "node:url"

const PYTHON = process.platform === "win32" ? "python" : "python3"
const EXPORT_EVERY_MS = 10 * 60 * 1000
const TURN_ENDED = ["session.execution.succeeded", "session.execution.failed", "session.execution.interrupted"]
const COMPACTED = "session.compaction.ended"

// sessmem.py: SESSMEM_SCRIPT, else beside this file's real location, else the installed Claude Code plugin.
function findScript() {
  const beside = join(dirname(realpathSync(fileURLToPath(import.meta.url))), "..", "hooks", "sessmem.py")
  const cache = join(homedir(), ".claude", "plugins", "cache", "hadi-plugins", "session-memory")
  const installed = existsSync(cache)
    ? readdirSync(cache).sort().map((version) => join(cache, version, "hooks", "sessmem.py"))
    : []
  return [process.env.SESSMEM_SCRIPT, beside, ...installed.reverse()].find((path) => path && existsSync(path))
}

// What both APIs share: the start context, and exports at most every ten minutes per session,
// on compaction and at exit. Null when sessmem.py is missing or this is sessmem's own export run.
function memory(directory, v1) {
  const script = findScript()
  if (!script || process.env.SESSMEM_CHILD) return null
  const env = { ...process.env, SESSMEM_OPENCODE: process.execPath, ...(v1 ? { SESSMEM_OPENCODE_V1: "1" } : {}) }
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
    context: context.trim(),
    exportSession,
    turnEnded(id) {
      if (Date.now() - (exportedAt.get(id) ?? 0) > EXPORT_EVERY_MS) exportSession(id)
      else unexported.add(id)
    },
    exit() {
      for (const id of [...unexported]) exportSession(id)
    },
  }
}

export default {
  id: "session-memory",

  async setup(ctx) {
    const mem = memory(ctx.location.directory, false)
    if (!mem) return
    if (mem.context) {
      await ctx.session.hook("context", (event) => {
        event.system.push({ type: "text", text: mem.context })
      })
    }
    const controller = new AbortController()
    void (async () => {
      const events = ctx.event.subscribe({ types: [...TURN_ENDED, COMPACTED], signal: controller.signal })
      for await (const event of events) {
        const id = event.data?.sessionID ?? event.durable?.aggregateID
        if (!id) continue
        if (event.type === COMPACTED) mem.exportSession(id)
        else mem.turnEnded(id)
      }
    })().catch(() => {})
    return () => {
      mem.exit()
      controller.abort()
    }
  },

  async server({ directory }) {
    const mem = memory(directory, true)
    if (!mem) return {}
    return {
      event: async ({ event }) => {
        const id = event.properties?.sessionID
        if (event.type === "session.compacted") mem.exportSession(id)
        if (event.type === "session.idle") mem.turnEnded(id)
      },
      dispose: async () => mem.exit(),
      "experimental.chat.system.transform": async (_input, output) => {
        if (mem.context) output.system.push(mem.context)
      },
    }
  },
}
