"""Every external effect goes through `SysAPI` so the installer can be tested with fakes."""

from __future__ import annotations

import os
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class Result:
    code: int
    out: str

    @property
    def ok(self) -> bool:
        return self.code == 0


class SysAPI:
    """Real implementation backed by subprocess."""

    def run(self, cmd: list[str], *, env: dict[str, str] | None = None, timeout: float = 1800,
            cwd: Path | None = None) -> Result:
        full_env = {**os.environ, **(env or {})}
        try:
            proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, env=full_env,
                                  cwd=cwd, check=False)
        except FileNotFoundError as exc:
            return Result(127, str(exc))
        except subprocess.TimeoutExpired:
            return Result(124, f"timeout after {timeout}s: {' '.join(cmd)}")
        return Result(proc.returncode, (proc.stdout + proc.stderr).strip())

    def which(self, name: str) -> str | None:
        return shutil.which(name)

    def uid(self) -> int:
        return os.getuid()

    # launchd -------------------------------------------------------------
    def launchd_loaded(self, label: str) -> bool:
        return self.run(["launchctl", "print", f"gui/{self.uid()}/{label}"]).ok

    def launchd_bootstrap(self, plist: Path) -> Result:
        return self.run(["launchctl", "bootstrap", f"gui/{self.uid()}", str(plist)])

    def launchd_bootout(self, label: str) -> Result:
        return self.run(["launchctl", "bootout", f"gui/{self.uid()}/{label}"])

    def launchd_kickstart(self, label: str) -> Result:
        return self.run(["launchctl", "kickstart", "-k", f"gui/{self.uid()}/{label}"])
