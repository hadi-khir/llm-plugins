# hadi-plugins

Claude Code plugins by [Hadi Khir](https://github.com/hadi-areaxo).

## Install

Add the marketplace (once), then install a plugin:

```
/plugin marketplace add hadi-areaxo/claude-plugins
/plugin install trad-coding@hadi-plugins
```

## Plugins

### trad-coding

Traditional hands-off coding mode. While enabled, Claude never creates or edits project files itself — it investigates your codebase, then gives you the code and concise instructions (file, location, snippet, verify command) so you type every change yourself.

Why: you stay hands-on-keyboard and keep full understanding and ownership of your codebase. Claude navigates, you drive.

- Triggers automatically on any code-change request while the plugin is enabled
- Reading, searching, and read-only diagnostics are still allowed, so instructions cite real files and line numbers
- If a task can't be done without Claude editing files, it asks you to confirm before breaking the rule

Disable anytime with `/plugin` → uninstall, or say "disable trad-coding for this task" in a session.

## License

MIT
