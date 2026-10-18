#!/usr/bin/env python3
"""Install a user-owned Agents clone, keeping a recoverable config backup."""
from datetime import datetime
import getpass
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile

ROOT = Path(__file__).resolve().parent


def main():
    plugin_id = getpass.getuser() + ".agents"
    config = Path.home() / ".config/omarchy"
    target = config / "plugins" / plugin_id
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S-%f")
    backup = ROOT / "backups" / stamp
    backup.mkdir(mode=0o700, parents=True)
    shutil.copy2(config / "shell.json", backup / "shell.json")
    if target.exists():
        manifest = json.loads((target / "manifest.json").read_text())
        if manifest.get("omarchy", {}).get("clonedFrom") != "omarchy.agents":
            raise SystemExit("Target is not an Agents clone; refusing to replace it")
        shutil.copytree(target, backup / plugin_id)
    else:
        subprocess.run(["omarchy", "plugin", "clone", "omarchy.agents"], check=True)

    # Publish a complete plugin at once; never hot-load a partial collector.
    stage = Path(tempfile.mkdtemp(prefix=".agents-stage-", dir=target.parent))
    try:
        shutil.copytree(ROOT / "plugin", stage, dirs_exist_ok=True,
                        ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
        manifest = json.loads((stage / "manifest.json").read_text())
        manifest["id"] = plugin_id
        (stage / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n")
        retired = target.with_name(".agents-retired-" + stamp)
        target.rename(retired)
        try:
            stage.rename(target)
        except BaseException:
            retired.rename(target)
            raise
        shutil.rmtree(retired)
    finally:
        if stage.exists():
            shutil.rmtree(stage)
    subprocess.run(["python3", str(target / "collectors/update.py"), "--local-only", "--force"], check=True)
    subprocess.run(["omarchy-shell", "shell", "rescanPlugins"], check=True)
    print("Installed " + str(target))
    print("Backup: " + str(backup))


if __name__ == "__main__":
    main()
