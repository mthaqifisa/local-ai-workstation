"""In-memory stand-in for law.system.System, so reconcile logic runs anywhere."""
from __future__ import annotations

from pathlib import Path


class FakeSystem:
    def __init__(self, hf_cache: Path):
        self.hf_cache = hf_cache
        self.brew: set[str] = set()
        self.brew_deps: dict[str, list[str]] = {}
        self.colima: dict | None = None
        self.containers: dict[str, dict] = {}
        self.venvs: dict[str, str] = {}
        self.pkgs: dict[str, str] = {}
        self.free = 500.0
        self.calls: list[str] = []
        self.fail_download: set[str] = set()
        self.loaded: dict[str, str] = {}

    # brew
    def brew_installed(self): return set(self.brew)
    def brew_install(self, f): self.calls.append(f"brew+{f}"); self.brew.add(f)
    def brew_uninstall(self, f): self.calls.append(f"brew-{f}"); self.brew.discard(f)
    def brew_dependents(self, f): return self.brew_deps.get(f, [])
    def brew_casks_installed(self): return set(getattr(self, "casks", set()))
    def brew_install_cask(self, c):
        self.calls.append(f"cask+{c}")
        if not hasattr(self, "casks"): self.casks = set()
        self.casks.add(c)

    # colima
    def colima_info(self): return dict(self.colima) if self.colima else None
    def colima_start(self, cpu, mem, disk):
        self.calls.append(f"colima start {cpu}/{mem}/{disk}")
        self.colima = {"status": "Running", "cpus": cpu, "memory_gb": mem, "disk_gb": disk}
    def colima_stop(self):
        self.calls.append("colima stop"); self.colima["status"] = "Stopped"

    # docker
    def docker_ok(self): return bool(self.colima and self.colima["status"] == "Running")
    def docker_containers(self):
        return [dict(name=n, **c) for n, c in self.containers.items()]
    def docker_rm(self, n): self.calls.append(f"rm {n}"); self.containers.pop(n, None)
    def docker_start(self, n): self.calls.append(f"start {n}"); self.containers[n]["state"] = "running"
    def docker_pull(self, image): self.calls.append(f"pull {image}")
    def docker_run(self, args):
        name = args[args.index("--name") + 1]
        assert name not in self.containers, f"name conflict {name}"
        labels = dict(args[i + 1].split("=", 1) for i, a in enumerate(args) if a == "--label")
        self.calls.append(f"run {name}")
        self.containers[name] = {"state": "running", "image": args[-1], "labels": labels}

    # python
    def venv_python_ok(self, venv, version): return self.venvs.get(str(venv)) == version
    def venv_create(self, venv, version):
        self.calls.append("venv"); self.venvs[str(venv)] = version
        (Path(venv) / "bin").mkdir(parents=True, exist_ok=True)
    def uv_compile(self, req_in, lock, python):
        self.calls.append("compile")
        pins = [l.split(">=")[0].split("==")[0].strip() for l in Path(req_in).read_text().splitlines()
                if l.strip() and not l.lstrip().startswith("#")]
        Path(lock).write_text("# lock\n" + "".join(f"{p}==1.0\n" for p in pins))
    def uv_sync(self, lock, python):
        self.calls.append("sync"); self.pkgs[str(python)] = Path(lock).read_text()
    def uv_freeze(self, python): return self.pkgs.get(str(python), "")

    # hf
    def hf_download(self, python, repo, token):
        self.calls.append(f"dl {repo}")
        if repo in self.fail_download:
            raise RuntimeError("network down")
        d = self.hf_cache / ("models--" + repo.replace("/", "--"))
        (d / "snapshots" / "abc").mkdir(parents=True, exist_ok=True)
        (d / "blobs").mkdir(exist_ok=True)
        for f in (d / "blobs").glob("*.incomplete"):
            f.unlink()

    def free_gb(self, path): return self.free
    def launchctl_bootout(self, label): self.calls.append(f"bootout {label}")

    # launchd services
    def launchd_loaded(self, label): return label in self.loaded
    def launchd_load(self, label, plist):
        self.calls.append(f"load {label}"); self.loaded[label] = Path(plist).read_text()
    def launchd_unload(self, label):
        self.calls.append(f"unload {label}"); self.loaded.pop(label, None)
    def launchd_kickstart(self, label): self.calls.append(f"kickstart {label}")
    def http_ok(self, url, wait=20): return "com.law.gateway" in self.loaded
