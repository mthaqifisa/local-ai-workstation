# ADR-0005: Data layout
Status: Accepted (2026-10-19)

- Code: `~/SourceCode`; released code in `~/SourceCode/OpenSourceLocalAI/v6`.
- Portable non-sensitive state in v6: `settings/`, `knowledge/`, `trained/` (Git LFS), `templates/`.
- Machine-local, disposable: `~/LAW/{config,state,data,models,runtime,colima,docker,outputs,logs,backups,trash,archive,cache}`.
- Secrets only in `~/LAW/state/.env` (0600). Tools are pointed at `~/LAW` via `~/LAW/config/env.sh`, sourced from one marked line in `~/.zprofile`.
