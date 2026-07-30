# hadi-plugins

Claude Code plugins by [Hadi Khir](https://github.com/hadi-khir).

## Install

Add the marketplace (once), then install a plugin:

```
/plugin marketplace add hadi-khir/claude-plugins
/plugin install trad-coding@hadi-plugins
```

## Plugins

### trad-coding

If you're tired of atrophy and feel yourself becoming duller by the day, this is just the skill. Have LLM's go back to just providing you with the code and you can either type it out by hand and learn by doing, or go back to the stack overflow days and copy paste. Take a break from autopilot. 

- Triggers automatically on any code-change request while the plugin is enabled
- Reading, searching, and read-only diagnostics are still allowed, so instructions cite real files and line numbers
- If a task can't be done without Claude editing files, it asks you to confirm before breaking the rule

#### Example

**You:** add a POST /echo endpoint to my express app

**Claude** (instead of editing anything):

> Add a POST /echo route after the existing GET route.
>
> `src/app.js` — after line 8 (the closing `});` of the `app.get('/', ...)` route):
>
> ```js
> app.post('/echo', (req, res) => {
>   res.json(req.body);
> });
> ```
>
> `express.json()` is already mounted on line 4, so `req.body` will be the parsed JSON.
>
> Verify:
>
> ```bash
> curl -X POST localhost:3000/echo -H "Content-Type: application/json" -d '{"hello":"world"}'
> ```

Disable anytime with `/plugin` → uninstall, or say "disable trad-coding for this task" in a session.

## License

MIT
