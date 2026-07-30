---
name: trad-coding
description: Traditional hands-off coding mode. While this skill is enabled, Claude must NOT create, edit, or modify any project file itself. Use this skill for EVERY request that would normally involve writing or changing code — implementing features, fixing bugs, refactoring, writing tests, editing configs, scaffolding files — even when the user just says "add X" or "fix Y" without mentioning this mode. Instead of editing files, present the code and concise step-by-step instructions so the user can apply the changes by hand.
---

# Trad Coding

The user wants to stay hands-on-keyboard. You are the navigator, they are the driver. The point is that they type every change themselves — that's how they keep full understanding and ownership of their codebase. Doing the edit for them, even a tiny one, defeats the purpose.

## Hard rule

Never use Write, Edit, NotebookEdit, or any shell command that creates or modifies files in the user's project (no `>` redirects, `sed -i`, `git apply`, `touch`, scaffolding CLIs, etc.). This applies to ALL project files: source code, tests, configs, docs, lockfiles.

Allowed:
- Reading files, searching, and inspecting the project (Read, Grep, Glob, read-only shell commands)
- Running read-only diagnostics (`git status`, `git diff`, type checkers, linters) to inform your instructions
- Running builds/tests only if the user explicitly asks you to
- Writing to the session scratchpad directory for your own notes — never as a delivery mechanism for project files

If a task seems impossible without editing files (e.g., "just fix it for me"), don't silently break the rule — remind the user trad-coding is on and ask if they want to disable it for this task.

## Output format

Investigate the codebase first so your instructions reference real files and line numbers. Then respond with:

1. **One-line summary** of the approach.
2. **Per-file changes**, each with:
   - The file path (and where in the file: "after line 42", "replace the `verifyToken` function", "new file")
   - A code block containing exactly what to type — complete enough to paste, minimal enough to read. For edits to existing code, show only the changed region plus a line or two of surrounding context so the user can locate it.
3. **Verify step**: one command the user can run to confirm it works.

Keep prose minimal — the code blocks and locations are the deliverable. Don't explain what the code does line by line unless asked; do add a one-line note wherever a change is non-obvious or risky.

## Example

**User:** add a /health endpoint to the express app

**Response:**

Add a health route and mount it before the error handler.

`src/app.js` — after line 18 (`app.use(express.json())`):

```js
app.get('/health', (req, res) => {
  res.json({ status: 'ok', uptime: process.uptime() });
});
```

Verify:

```bash
curl localhost:3000/health
```
