from __future__ import annotations

import os
import hashlib
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path


APP_DIR = Path(__file__).resolve().parent
RUNTIME_DIR = Path(os.environ.get("MUSIC_LIBRARY_RUNTIME", tempfile.gettempdir())) / "MiBibliotecaMusical"
PROVIDER_VERSION = "2.0.2"
PROVIDER_DIR = RUNTIME_DIR / f"bgutil-ytdlp-pot-provider-{PROVIDER_VERSION}"
PROVIDER_ENTRY = PROVIDER_DIR / "server" / "build" / "main.js"
PROVIDER_REPOSITORY = "https://github.com/Brainicism/bgutil-ytdlp-pot-provider.git"


def require_program(name: str) -> str:
    resolved = shutil.which(name)
    if not resolved:
        raise RuntimeError(f"No se encontró {name} en el PATH.")
    return resolved


def run(command: list[str], cwd: Path | None = None) -> None:
    subprocess.run(command, cwd=str(cwd or APP_DIR), check=True)


def main() -> int:
    manifest_dir = APP_DIR / "runtime"
    lock_file = manifest_dir / "provider-package-lock.json"
    lock_hash = hashlib.sha256(lock_file.read_bytes()).hexdigest()
    ready_marker = PROVIDER_DIR / ".ongaku-lock-sha256"
    if PROVIDER_ENTRY.is_file() and ready_marker.is_file() and ready_marker.read_text().strip() == lock_hash:
        print("Proveedor de acceso de YouTube listo.")
        return 0

    try:
        git = require_program("git")
        require_program("node")
        npm = require_program("npm")
        npx = require_program("npx")
        RUNTIME_DIR.mkdir(parents=True, exist_ok=True)

        print("Preparando el proveedor de tokens de YouTube (solo la primera vez)...")
        if not (PROVIDER_DIR / "server" / "package.json").is_file():
            if PROVIDER_DIR.exists():
                raise RuntimeError(
                    f"La instalación incompleta no se puede reparar automáticamente: {PROVIDER_DIR}"
                )
            run([
                git, "clone", "--depth", "1", "--branch", PROVIDER_VERSION,
                PROVIDER_REPOSITORY, str(PROVIDER_DIR),
            ])
        server_dir = PROVIDER_DIR / "server"
        npm_cache = RUNTIME_DIR / "npm-cache"
        npm_cache.mkdir(parents=True, exist_ok=True)
        shutil.copy2(manifest_dir / "provider-package.json", server_dir / "package.json")
        shutil.copy2(lock_file, server_dir / "package-lock.json")
        run([npm, "ci", "--include=dev", "--cache", str(npm_cache)], server_dir)
        run([npx, "--no-install", "tsc"], server_dir)
        run([npm, "prune", "--omit=dev", "--ignore-scripts"], server_dir)

        if not PROVIDER_ENTRY.is_file():
            raise RuntimeError("La compilación terminó, pero no creó server/build/main.js.")
        ready_marker.write_text(lock_hash, encoding="ascii")
        print("Proveedor de acceso de YouTube instalado correctamente.")
        return 0
    except Exception as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
