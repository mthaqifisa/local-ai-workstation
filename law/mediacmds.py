"""`law image` / `law speak` / `law video` / `law vision` / `law transcribe` — the media tools
from the terminal. Each is a normal queue job (same as the dashboard and Telegram), so it waits
its turn, frees the language model first, and shows up in `law jobs` and the dashboard."""
from __future__ import annotations

import json
import sys
from pathlib import Path

from .paths import get_paths
from .runcmds import _dim, _worker_warning, tail_job
from .runtime.db import Queue
from .util import err, ok, warn


def _run(kind: str, model: str, payload: dict, label: str, a, persona: str | None = None) -> int:
    q = Queue(get_paths().queue_db)
    jid = q.enqueue(model, payload, persona=persona, source="cli", kind=kind)
    print(_dim(f"[job {jid} · {label}]"))
    _worker_warning(q)
    if getattr(a, "detach", False):
        print(f"queued. Follow it: law jobs   ·   cancel: law cancel {jid}")
        return 0
    try:
        st = tail_job(q, jid)
    except KeyboardInterrupt:
        q.cancel(jid, reason="Ctrl+C")
        warn(f"cancelled job {jid}")
        return 130
    if st != "done":
        return 1
    res = json.loads((q.get(jid) or {}).get("result") or "{}")
    media = res.get(kind) or {}
    if media.get("path"):
        ok(f"saved: {media['path']}")
        if getattr(a, "open", False) and sys.platform == "darwin":
            import subprocess
            subprocess.run(["open", media["path"]], check=False)
    elif (res.get("message") or {}).get("content"):
        print((res["message"]["content"]).strip())
    return 0


def _text(parts: list[str] | None) -> str:
    if parts:
        return " ".join(parts).strip()
    try:
        return "" if sys.stdin.isatty() else sys.stdin.read().strip()
    except (OSError, ValueError):                # no usable stdin (launchd, pytest)
        return ""


def cmd_image(a) -> int:
    prompt = _text(a.prompt)
    if not prompt:
        err('empty prompt — usage: law image "a red fox in snow"'); return 2
    params = {k: v for k, v in (("model", a.model), ("steps", a.steps), ("width", a.width),
                                ("height", a.height)) if v is not None}
    return _run("image", "flux", {"prompt": prompt, "params": params, "seed": a.seed},
                f"image · {prompt[:50]}", a)


def cmd_speak(a) -> int:
    text = _text(a.text)
    if not text:
        err('empty text — usage: law speak "hello there"'); return 2
    params = {"voice": a.voice, **({"speed": a.speed} if a.speed else {})}
    return _run("tts", "kokoro", {"text": text, "params": params}, f"speech · {a.voice}", a)


def cmd_video(a) -> int:
    prompt = _text(a.prompt)
    if not prompt:
        err('empty prompt — usage: law video "waves at sunset"'); return 2
    image = str(Path(a.image).expanduser().resolve()) if a.image else None
    if image and not Path(image).is_file():
        err(f"not a file: {image}"); return 2
    params = {"duration_s": a.seconds} if a.seconds else {}
    return _run("video", "ltx23", {"prompt": prompt, "params": params, "image": image},
                f"video · {prompt[:50]}", a)


def cmd_vision(a) -> int:
    img = Path(a.image).expanduser().resolve()
    if not img.is_file():
        err(f"not a file: {img}"); return 2
    return _run("vision", "qwen3-vl-8b", {"image": str(img), "prompt": " ".join(a.question or [])},
                f"vision · {img.name}", a, persona="vision")


def cmd_transcribe(a) -> int:
    audio = Path(a.audio).expanduser().resolve()
    if not audio.is_file():
        err(f"not a file: {audio}"); return 2
    return _run("stt", "whisper", {"audio": str(audio)}, f"transcribe · {audio.name}", a)


def add_parsers(sub) -> None:
    s = sub.add_parser("image", help='generate an image:  law image "a red fox in snow"')
    s.add_argument("prompt", nargs="*", help="what to draw (or pipe it on stdin)")
    s.add_argument("--model", help="z-image-turbo (default) | z-image | qwen | flux2 | fibo | schnell")
    s.add_argument("--steps", type=int)
    s.add_argument("--width", type=int)
    s.add_argument("--height", type=int)
    s.add_argument("--seed", type=int)
    s.add_argument("--open", action="store_true", help="open the result when done")
    s.add_argument("-d", "--detach", action="store_true", help="queue it and return immediately")
    s.set_defaults(fn=cmd_image)

    s = sub.add_parser("speak", help='text to speech (Kokoro):  law speak "hello"')
    s.add_argument("text", nargs="*", help="what to say (or pipe it on stdin)")
    s.add_argument("--voice", default="af_sky", help="af_sky (default), af_heart, am_adam, am_echo, bf_emma, bm_lewis")
    s.add_argument("--speed", type=float)
    s.add_argument("--open", action="store_true", help="play the result when done")
    s.add_argument("-d", "--detach", action="store_true")
    s.set_defaults(fn=cmd_speak)

    s = sub.add_parser("video", help='text/image to video (LTX-2.3):  law video "waves at sunset"')
    s.add_argument("prompt", nargs="*")
    s.add_argument("--image", help="start from this image (image-to-video)")
    s.add_argument("--seconds", type=float, help="clip length")
    s.add_argument("--open", action="store_true")
    s.add_argument("-d", "--detach", action="store_true")
    s.set_defaults(fn=cmd_video)

    s = sub.add_parser("vision", help='describe / ask about an image:  law vision shot.png "what is wrong?"')
    s.add_argument("image")
    s.add_argument("question", nargs="*")
    s.add_argument("-d", "--detach", action="store_true")
    s.set_defaults(fn=cmd_vision)

    s = sub.add_parser("transcribe", help="speech to text (whisper):  law transcribe memo.m4a")
    s.add_argument("audio")
    s.add_argument("-d", "--detach", action="store_true")
    s.set_defaults(fn=cmd_transcribe)
