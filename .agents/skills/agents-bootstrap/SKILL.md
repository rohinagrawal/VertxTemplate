# Agent Assets Bootstrap

Use this skill when creating, importing, or refreshing a shared `.agents/` setup in a new repository.

## Goal

Create a lightweight, repo-specific agent setup that works with any LLM surface that reads `AGENTS.md`, `.agents/README.md`, or tool-specific adapters.

## What to Copy

Start from the shared `.agents/` folder and keep only what the new project actually needs:

- `.agents/README.md`
- `.agents/manifest.json`
- `.agents/rules/`
- `.agents/skills/`
- `.agents/scripts/`
- `.agents/workspace/`

Add `.agents/mcp/` only when the project actually needs a shared MCP server or local tool surface.

## Quick Setup Checklist

1. Create a root `AGENTS.md` file that points to `.agents/README.md`.
2. Rename the guide title and any project references inside `.agents/README.md`.
3. Update `.agents/manifest.json` so every listed `source`, `doc`, `adapter`, and `config_reference` path exists in the new repo.
4. Remove stale assets that belong to the old project.
5. Keep new project-specific rules in `.agents/rules/` and task playbooks in `.agents/skills/`.
6. If a tool needs an adapter, expose the shared asset through a symlink or the smallest possible wrapper.
7. Keep secrets out of committed files and use an ignored local env file for tokens.
8. Run validation before you consider the bootstrap complete.

## Recommended New-Project Flow

```bash
cp -R /path/to/template/.agents /path/to/new-project/
cat > /path/to/new-project/AGENTS.md <<'EOF'
# AGENTS.md

See `.agents/README.md` for the shared agent guide.
EOF
python3 /path/to/new-project/.agents/scripts/validate-agent-assets.py --mode adapters
```

If the new project also uses Codex or Cursor adapters, add the tool-specific entries only after the shared source is stable, and keep them as thin symlinks or wrappers.

## Manifest Rules

- Every asset in `.agents/manifest.json` should match files that really exist.
- `adapter_hosts` should only list hosts that truly need direct adapter coverage.
- If a host does not need a shared asset, omit it instead of keeping a stale placeholder.
- Keep docs and validation commands aligned with the files that are actually present.

## Finish Line

A bootstrap is complete when:

- `AGENTS.md` exists at the repo root.
- `.agents/README.md` names the correct project.
- `.agents/manifest.json` contains only live assets.
- The validation command passes.
- The next person can copy the folder into a new repo and understand what to change first.

