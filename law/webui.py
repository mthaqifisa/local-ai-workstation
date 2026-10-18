"""`law webui-reset-password` — list Open WebUI accounts and set a new password.

Runs small Python snippets INSIDE the open-webui container (its own sqlite database and
bcrypt). The new password is passed on stdin, never on the command line, so it doesn't
show up in `ps` or shell history. Chats and settings are untouched.
"""
from __future__ import annotations

import getpass
import json
import subprocess
import sys
from typing import Callable

CONTAINER = "open-webui"
DB = "/app/backend/data/webui.db"

LIST_CODE = (f"import sqlite3,json;c=sqlite3.connect('{DB}');"
             "print(json.dumps([list(r) for r in c.execute('select email, role, name from user order by created_at')]))")
RESET_CODE = ("import sys,json,sqlite3,bcrypt;d=json.load(sys.stdin);"
              "h=bcrypt.hashpw(d['p'].encode(),bcrypt.gensalt()).decode();"
              f"c=sqlite3.connect('{DB}');"
              "n=c.execute('update auth set password=? where email=?',(h,d['e'])).rowcount;"
              "c.commit();print('updated' if n else 'notfound')")

Runner = Callable[[list[str], str | None], tuple[int, str, str]]


def docker_runner(cmd: list[str], stdin: str | None) -> tuple[int, str, str]:
    try:
        r = subprocess.run(cmd, input=stdin, capture_output=True, text=True, timeout=120)
        return r.returncode, r.stdout, r.stderr
    except (OSError, subprocess.SubprocessError) as e:
        return 1, "", str(e)


class WebUIError(RuntimeError):
    pass


def list_users(runner: Runner = docker_runner) -> list[dict]:
    rc, out, err = runner(["docker", "exec", CONTAINER, "python3", "-c", LIST_CODE], None)
    if rc != 0:
        raise WebUIError("could not read Open WebUI accounts — is the container running? "
                         f"(`law status`).  {err.strip()[:300]}")
    try:
        rows = json.loads(out.strip().splitlines()[-1])
    except (ValueError, IndexError) as e:
        raise WebUIError(f"unexpected output from the container: {out[:200]}") from e
    return [{"email": r[0], "role": r[1], "name": r[2] if len(r) > 2 else ""} for r in rows]


def reset_password(email: str, password: str, runner: Runner = docker_runner) -> None:
    if not password:
        raise WebUIError("empty password")
    rc, out, err = runner(["docker", "exec", "-i", CONTAINER, "python3", "-c", RESET_CODE],
                          json.dumps({"e": email, "p": password}))
    if rc != 0:
        raise WebUIError(f"reset failed: {err.strip()[:300]}")
    if "notfound" in out:
        raise WebUIError(f"no Open WebUI account with email {email}")
    if "updated" not in out:
        raise WebUIError(f"unexpected output from the container: {out[:200]}")


def interactive(email: str | None, runner: Runner = docker_runner,
                ask: Callable[[str], str] = input, secret: Callable[[str], str] = getpass.getpass,
                out=sys.stdout) -> int:
    users = list_users(runner)
    if not users:
        out.write("No accounts yet — open http://localhost:3001 and sign up; the first account is the admin.\n")
        return 0
    out.write("Open WebUI accounts:\n")
    for i, u in enumerate(users, 1):
        out.write(f"  {i}. {u['email']}  ({u['role']}{', ' + u['name'] if u['name'] else ''})\n")
    if not email:
        if len(users) == 1:
            email = users[0]["email"]
        else:
            choice = ask("Which account? (number or email): ").strip()
            email = users[int(choice) - 1]["email"] if choice.isdigit() and 0 < int(choice) <= len(users) else choice
    if email not in {u["email"] for u in users}:
        raise WebUIError(f"no Open WebUI account with email {email}")
    p1 = secret(f"New password for {email}: ")
    p2 = secret("Repeat it: ")
    if p1 != p2:
        raise WebUIError("passwords don't match — nothing changed")
    reset_password(email, p1, runner)
    out.write(f"Password updated for {email}. Sign in at http://localhost:3001\n")
    return 0
