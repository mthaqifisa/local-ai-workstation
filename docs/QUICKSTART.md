# Quick start

## 1. Check it's healthy

```bash
law doctor
```

All rows should say **up**. The bottom of the output lists the addresses for your other devices, for example:

```
Chat / Open WebUI : http://192.168.1.20:3000
LLM API           : http://192.168.1.20:8000/v1
Dashboard         : http://192.168.1.20:3010
law CLI elsewhere : export LAW_SERVER=http://192.168.1.20:8100
```

You can also use the Mac's network name instead of the IP: `http://<mac-name>.local:3000`.

Open the **LAW Console** at `http://<mac-name>.local:8100` from any device: live CPU/memory/GPU/storage of the Mac,
running jobs with a Cancel button, persona editing, and Telegram settings.

## 2. Ask something

```bash
law ask "Compare leasing vs buying a company car for a 10-person firm"
law ask --role architect "Design a document-approval workflow for a small bank"
law ask --quick "What is 17% of 2,340?"      # skips the review loop for quick questions
```

By default every answer is drafted, reviewed by an independent model, and revised if the review finds gaps.
Progress lines (`… stage 2/3: judge`) show where it is.

Roles: `assistant` (default), `architect`, `coder`, `reviewer`, `writer`, `analyst`, `designer`, `researcher`, `operator`.

## 3. Create things

```bash
law image "A clean isometric illustration of a data centre, pastel colours"
law edit photo.jpg "Remove the background and make it pure white"
law tts "Good morning. Here is today's summary."
law stt meeting.m4a
law music "Calm lo-fi piano for focus" --duration 60
law video "A paper boat drifting down a rain-soaked street at dusk, cinematic" --seconds 5
law video --image boat.png "The boat slowly drifts forward as rain falls"
```

Video uses Wan 2.2 (14B) and takes several minutes per clip; it runs one at a time in the queue.

Files land in `~/LAW/outputs/{images,audio,video,docs}`; the command prints the path.

## 4. Code with an agent

```bash
mkdir -p ~/SourceCode/expense-tracker
law code --dir ~/SourceCode/expense-tracker "Build a FastAPI expense tracker with SQLite, tests and a README"
```

## 5. Let an agent use your Mac

The **operator** role can open apps, play music, change the volume, run Shortcuts and take screenshots to check its work.

```bash
law agent operator "Play some calm jazz on Spotify and set the volume to 25%"
```

macOS asks once for permission (Accessibility, Automation, Screen Recording) the first time each app is controlled.
Approve it in System Settings → Privacy & Security.

## 6. Telegram

1. In Telegram, talk to **@BotFather** → `/newbot` → copy the token.
2. Get your numeric user ID from **@userinfobot**.
3. Put both in LAW and restart the bot:

   ```bash
   ./install.sh --secrets my-secrets.env   # file with LAW_TELEGRAM_TOKEN=... and LAW_TELEGRAM_USER_IDS=...
   ```

4. Message your bot: plain text to ask, a voice note to talk, a photo with a caption. Tap the menu button next to the
   attachment icon (or send `/help`) for every command: `/image`, `/edit` (photo caption), `/video`, `/animate` (photo
   caption), `/music` (add `lyrics:` for your own words, and a duration like `90s`), `/say`, `/code`, `/agent`, `/search`,
   `/jobs`, `/cancel`. While Tony works, its status message shows live thinking.

## 7. Use it from another computer

```bash
export LAW_SERVER=http://<mac-name>.local:8100
law ask "…"        # runs on the AI Mac
```

Any OpenAI-compatible app can use `http://<mac-name>.local:8000/v1` with any API key.
