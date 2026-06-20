Review the current `script3.sh` script architecture and implement the following three critical upgrades to the embedded Python orchestrator, `team.yaml` generation logic, and compilation pipeline.

### Task 1: Implement Async Detach & Alert (Fire-and-Forget Workflow)
Refactor the `orchestrator.py` generation block inside the script to prevent Telegram timeouts during heavy 70B+ model generations.
1. Modify the main message event handler loop. When a new project or large task is initiated, immediately update the state in `projects.json`.
2. Send an immediate asynchronous reply back to the user via Telegram: "🚀 Task detached to background processors. I will ping you here with the artifacts once the team finishes generation."
3. Decouple the sequential execution loops (`proposal_drafting`, `development`, etc.) from the live webhook/polling loop using native Python `asyncio.create_task()` or a background background worker thread pool.
4. Once background execution finishes, use the cached `TELEGRAM_CHAT_ID` to call `context.bot.send_document()` or `send_message()` to spontaneously alert the user and upload the final files.

### Task 2: Fix Wireframe Compilation in PDF Pipeline
Update the document generation pipeline to ensure wireframes render visually inside the final WeasyPrint PDF.
1. Update Mira's system prompt inside the `team.yaml` block: Change her instructions so that instead of raw draw.io XML blocks, she outputs clean, native inline SVG code wrapped inside standard markdown file headers (e.g., `### assets/wireframe.svg \n ```xml\n<svg>...</svg>\n```).
2. Update the Python file parser and WeasyPrint compiler block: Ensure that when parsing markdown files, any generated `.svg` assets are correctly referenced as standard image elements or injected inline within the HTML template before WeasyPrint processes it.

### Task 3: Parametrize Hardcoded Workspace Paths
Clean up configuration paths to prevent systemic directory errors.
1. Locate the multi-line string configurations for `team.yaml` and the system prompts. Remove all instances of hardcoded absolute paths like `/Users/thaqifisa/`.
2. Replace them with dynamic python string tokens or environment placeholders (e.g., `{DOCS_WORKSPACE}` and `{CODE_WORKSPACE}`).
3. Ensure the Python configuration loader maps these tokens at runtime using variables derived dynamically from the script's `WORKDIR` and `WORKSPACE` variables.
4. set the master folder to be ~/OneDrive instead

Please output the fully updated, complete `script3.sh` script with these structural enhancements integrated seamlessly.
