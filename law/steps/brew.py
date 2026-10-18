"""Homebrew formulae: install missing ones; uninstall ones this installer added that
the registry no longer lists (only if nothing else depends on them)."""
from __future__ import annotations

from ..engine import Action, Ctx, Step
from ..manifest import load_manifest
from ..registry import load


def plan_brew(ctx: Ctx) -> list[Action]:
    want = load(ctx.paths).brew_formulae
    have = ctx.sys.brew_installed()
    man = load_manifest(ctx.paths)
    acts = []
    for f in want:
        if f not in have:
            def fn(f=f):
                ctx.sys.brew_install(f)
                load_manifest(ctx.paths).own_brew(f)
            acts.append(Action("add", f"brew {f}", fn))
    for f in list(man.data["brew"]):
        if f in want:
            continue
        if f not in have:                       # already gone → just forget it
            acts.append(Action("remove", f"forget {f} (already uninstalled)",
                               lambda f=f: load_manifest(ctx.paths).disown_brew(f)))
            continue
        if ctx.sys.brew_dependents(f):
            continue                            # still needed by something else → keep
        def rm(f=f):
            ctx.sys.brew_uninstall(f)
            load_manifest(ctx.paths).disown_brew(f)
        acts.append(Action("remove", f"brew {f} (no longer needed)", rm))
    # GUI casks the workstation needs (e.g. a browser for the screenshot/cowork tools). Install-only:
    # we never auto-uninstall a GUI app the user may rely on.
    casks = load(ctx.paths).raw.get("brew", {}).get("casks", []) or []
    have_casks = ctx.sys.brew_casks_installed()
    # a browser cask only needs installing if NO headless-capable browser exists yet (chromium is only
    # there for the screenshot tool). Check the filesystem too, so a manually-installed Chrome counts.
    from pathlib import Path
    browser_casks = {"google-chrome", "chromium", "brave-browser"}
    browser_present = (bool(have_casks & browser_casks)
                       or any(Path(p).exists() for p in ("/Applications/Google Chrome.app",
                              "/Applications/Chromium.app", "/Applications/Brave Browser.app")))
    for c in casks:
        if c in have_casks or (c in browser_casks and browser_present):
            continue
        acts.append(Action("add", f"brew cask {c}", lambda c=c: ctx.sys.brew_install_cask(c)))
    return acts


STEPS = [Step("brew", "System packages (Homebrew)", plan_brew, macos_only=True)]
