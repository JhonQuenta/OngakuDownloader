from __future__ import annotations

import concurrent.futures
import atexit
import html
import json
import os
import queue
import re
import shutil
import signal
import subprocess
import sys
import threading
import time
import tempfile
import unicodedata
import uuid
import webbrowser
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import parse_qs, urlencode, urlparse, urlunparse
from urllib.request import Request, urlopen

import yt_dlp
from flask import Flask, jsonify, render_template, request
from mutagen import File as MutagenFile
from mutagen.id3 import ID3, ID3NoHeaderError, USLT


APP_DIR = Path(__file__).resolve().parent
DEFAULT_DOWNLOAD_DIR = APP_DIR / "downloads"
RUNTIME_DIR = Path(os.environ.get("MUSIC_LIBRARY_RUNTIME", tempfile.gettempdir())) / "MiBibliotecaMusical"
POT_PROVIDER_ENTRY = RUNTIME_DIR / "bgutil-ytdlp-pot-provider-2.0.2" / "server" / "build" / "main.js"
POT_PROVIDER_URL = "http://127.0.0.1:4416"
DEFAULT_DOWNLOAD_DIR.mkdir(parents=True, exist_ok=True)

app = Flask(__name__)
app.config.update(TRUSTED_HOSTS=["127.0.0.1", "localhost"], MAX_CONTENT_LENGTH=4 * 1024 * 1024)


@app.before_request
def protect_local_service():
    if request.remote_addr not in {"127.0.0.1", "::1"}:
        return jsonify({"error": "Solo se permiten conexiones locales."}), 403
    if request.headers.get("Sec-Fetch-Site") == "cross-site":
        return jsonify({"error": "Solicitud desde otro sitio bloqueada."}), 403
    origin = request.headers.get("Origin")
    if origin and origin != request.host_url.rstrip("/"):
        return jsonify({"error": "Origen no permitido."}), 403
    if request.method == "POST":
        if not request.is_json:
            return jsonify({"error": "Se requiere application/json."}), 415
        if not isinstance(request.get_json(silent=True), dict):
            return jsonify({"error": "El cuerpo JSON debe ser un objeto."}), 400


@app.after_request
def security_headers(response):
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["Referrer-Policy"] = "no-referrer"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["Content-Security-Policy"] = (
        "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; "
        "img-src 'self' data: https:; connect-src 'self'; object-src 'none'; "
        "base-uri 'none'; frame-ancestors 'none'; form-action 'self'"
    )
    if request.path.startswith("/api/"):
        response.headers["Cache-Control"] = "no-store"
    return response

jobs: dict[str, "DownloadJob"] = {}
jobs_lock = threading.Lock()
lyrics_lock = threading.Lock()
ANSI_ESCAPE = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")
VIDEO_ID_IN_FILENAME = re.compile(r" \[[A-Za-z0-9_-]{11}\](?=\.)")
LEGACY_ORDER_IN_FILENAME = re.compile(r"^\d{4}\s+-\s+")
WORK_FILE_PREFIX = re.compile(r"^\.__musicdl_\d{4}_[A-Za-z0-9_-]+__\s*")
pot_provider_process: subprocess.Popen | None = None


def pot_provider_ready(timeout: float = 1.0) -> bool:
    try:
        with urlopen(f"{POT_PROVIDER_URL}/ping", timeout=timeout) as response:
            return response.status == 200 and json.load(response).get("version") == "2.0.2"
    except Exception:
        return False


def start_pot_provider() -> bool:
    global pot_provider_process
    if pot_provider_ready():
        return True
    if not POT_PROVIDER_ENTRY.is_file() or not shutil.which("node"):
        return False
    creation_flags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
    pot_provider_process = subprocess.Popen(
        ["node", str(POT_PROVIDER_ENTRY), "--host", "127.0.0.1"],
        cwd=str(POT_PROVIDER_ENTRY.parent.parent),
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        creationflags=creation_flags,
    )
    deadline = time.time() + 20
    while time.time() < deadline:
        if pot_provider_ready():
            return True
        if pot_provider_process.poll() is not None:
            break
        time.sleep(0.25)
    return False


def stop_pot_provider() -> None:
    global pot_provider_process
    if pot_provider_process and pot_provider_process.poll() is None:
        pot_provider_process.terminate()
        try:
            pot_provider_process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            pot_provider_process.kill()
    pot_provider_process = None


atexit.register(stop_pot_provider)


def safe_folder_name(value: str) -> str:
    value = unicodedata.normalize("NFKC", value or "Playlist")
    value = re.sub(r'[<>:"/\\|?*\x00-\x1f]', " ", value)
    value = re.sub(r"\s+", " ", value).strip(" .")
    return (value[:120] or "Playlist")


def clean_download_filename(name: str) -> str:
    """Remove identifiers used by older and current downloader versions."""
    name = VIDEO_ID_IN_FILENAME.sub("", name)
    name = LEGACY_ORDER_IN_FILENAME.sub("", name)
    return WORK_FILE_PREFIX.sub("", name)


def filename_without_video_id(name: str) -> str:
    """Kept for compatibility with callers from previous versions."""
    return VIDEO_ID_IN_FILENAME.sub("", name)


def available_destination(path: Path) -> Path:
    if not path.exists():
        return path
    counter = 2
    while True:
        candidate = path.with_name(f"{path.stem} ({counter}){path.suffix}")
        if not candidate.exists():
            return candidate
        counter += 1


def remove_video_ids_from_filenames(folder: Path) -> int:
    renamed = 0
    if not folder.is_dir():
        return renamed
    for source in folder.iterdir():
        if not source.is_file():
            continue
        clean_name = clean_download_filename(source.name)
        if clean_name == source.name:
            continue
        destination = available_destination(source.with_name(clean_name))
        try:
            source.rename(destination)
            renamed += 1
        except OSError:
            pass
    return renamed


def is_http_url(value: str) -> bool:
    return bool(re.match(r"^https?://", value or "", re.I))


def clean_error_text(value: str) -> str:
    cleaned = ANSI_ESCAPE.sub("", value).replace("\r", "").strip()
    return re.sub(r"^(?:ERROR:\s*)+", "ERROR: ", cleaned, flags=re.I)


def is_youtube_url(value: str) -> bool:
    try:
        parsed = urlparse(value)
        return (
            parsed.scheme == "https"
            and parsed.hostname in {"youtube.com", "www.youtube.com", "music.youtube.com", "m.youtube.com", "youtu.be", "www.youtu.be"}
            and parsed.port in {None, 443}
            and parsed.username is None and parsed.password is None
        )
    except (ValueError, TypeError, AttributeError):
        return False


def is_network_path(value: str) -> bool:
    return value.replace("/", "\\").startswith("\\\\")


def normalize_youtube_url(value: str) -> str:
    """Use YouTube's canonical playlist URL and discard tracking parameters."""
    parsed = urlparse(value.strip())
    host = parsed.netloc.lower().split(":", 1)[0]
    if host not in {"youtube.com", "www.youtube.com", "music.youtube.com", "m.youtube.com"}:
        return value.strip()
    query = parse_qs(parsed.query)
    playlist_id = (query.get("list") or [""])[0]
    if not playlist_id:
        return value.strip()
    return urlunparse(("https", "www.youtube.com", "/playlist", "", urlencode({"list": playlist_id}), ""))


def ffmpeg_available() -> bool:
    return bool(shutil.which("ffmpeg") and shutil.which("ffprobe"))


def public_job(job: "DownloadJob") -> dict:
    with job.lock:
        return {
            "id": job.id,
            "status": job.status,
            "total": job.total,
            "finished": job.finished,
            "succeeded": job.succeeded,
            "failed": job.failed,
            "cancelled_count": job.cancelled_count,
            "active": list(job.active.values()),
            "errors": list(job.errors),
            "results": list(job.results),
            "warnings": list(job.warnings),
            "fatal_error": job.fatal_error,
            "download_path": str(job.download_path),
            "cancel_requested": job.cancel_event.is_set(),
            "created_at": job.created_at,
        }


@dataclass
class DownloadJob:
    id: str
    songs: list[dict]
    download_path: Path
    options: dict
    status: str = "queued"
    finished: int = 0
    succeeded: int = 0
    failed: int = 0
    cancelled_count: int = 0
    errors: list[dict] = field(default_factory=list)
    results: list[dict] = field(default_factory=list)
    warnings: list[dict] = field(default_factory=list)
    fatal_error: str = ""
    active: dict[str, dict] = field(default_factory=dict)
    processes: dict[str, subprocess.Popen] = field(default_factory=dict)
    created_at: float = field(default_factory=time.time)
    cancel_event: threading.Event = field(default_factory=threading.Event)
    lock: threading.RLock = field(default_factory=threading.RLock)

    @property
    def total(self) -> int:
        return len(self.songs)


@app.get("/")
def index():
    return render_template("index.html")


@app.get("/api/health")
def health():
    return jsonify({
        "ok": True,
        "ffmpeg": ffmpeg_available(),
        "yt_dlp": yt_dlp.version.__version__,
        "pot_provider": pot_provider_ready(),
        "default_download_path": str(DEFAULT_DOWNLOAD_DIR),
    })


@app.post("/api/playlist")
def playlist():
    payload = request.get_json(silent=True) or {}
    url = normalize_youtube_url(str(payload.get("url", "")).strip())
    if not is_youtube_url(url):
        return jsonify({"error": "Pega una URL válida de YouTube o YouTube Music."}), 400

    options = {
        "extract_flat": "in_playlist",
        "skip_download": True,
        "quiet": True,
        "no_warnings": True,
        "ignoreerrors": False,
        "socket_timeout": 20,
        "retries": 2,
        "playlistend": 5000,
    }
    auth_browser = str(payload.get("auth_browser") or "none").lower()
    if auth_browser in {"chrome", "edge", "firefox"}:
        options["cookiesfrombrowser"] = (auth_browser,)
    try:
        with yt_dlp.YoutubeDL(options) as ydl:
            info = ydl.extract_info(url, download=False)
    except Exception as exc:
        detail = clean_error_text(str(exc))
        if "10013" in detail or "socket no permitido" in detail.lower():
            detail = "Windows bloqueó la conexión de Python a YouTube. Cierra otras instancias, inicia la aplicación con start.bat y permite Python en el Firewall de Windows si aparece el aviso."
        elif "could not copy chrome cookie database" in detail.lower():
            detail = "Chrome mantiene bloqueada su base de cookies. Abre esta aplicación en Edge o Firefox, cierra Chrome por completo (incluidos sus procesos en segundo plano) y vuelve a elegir Chrome como sesión. También puedes cargar primero la playlist con 'Sin cookies'."
        elif "could not copy edge cookie database" in detail.lower():
            detail = "Edge mantiene bloqueada su base de cookies. Abre esta aplicación en Chrome o Firefox, cierra Edge por completo y vuelve a elegir Edge como sesión."
        elif "could not copy firefox cookie database" in detail.lower():
            detail = "Firefox mantiene bloqueada su base de cookies. Cierra Firefox por completo, abre esta aplicación en otro navegador y vuelve a intentarlo."
        elif "failed to decrypt with dpapi" in detail.lower():
            detail = "Chrome usa un cifrado de cookies que yt-dlp no puede descifrar en este equipo. Inicia sesión en YouTube desde Firefox, cierra Firefox completamente, abre esta aplicación en Chrome y selecciona 'Usar mi sesión de Firefox'."
        elif "private" in detail.lower() or "sign in" in detail.lower() or "login" in detail.lower():
            detail = "La playlist parece privada o requiere iniciar sesión. Cámbiala temporalmente a pública o no listada y vuelve a intentarlo."
        return jsonify({"error": f"No se pudo leer la playlist. {detail}"}), 502

    if not info:
        return jsonify({"error": "YouTube no devolvió información para ese enlace."}), 404

    raw_entries = info.get("entries") or [info]
    songs = []
    for position, entry in enumerate(raw_entries, 1):
        if not entry:
            continue
        video_id = str(entry.get("id") or "")
        track_url = entry.get("webpage_url") or entry.get("url") or ""
        if video_id and not is_http_url(track_url):
            track_url = f"https://www.youtube.com/watch?v={video_id}"
        thumbnails = entry.get("thumbnails") or []
        thumbnail = entry.get("thumbnail") or (thumbnails[-1].get("url") if thumbnails else "")
        songs.append({
            "id": video_id or f"item-{position}",
            "index": position,
            "title": entry.get("title") or "Título desconocido",
            "artist": entry.get("artist") or entry.get("uploader") or entry.get("channel") or "Artista desconocido",
            "duration": entry.get("duration"),
            "thumbnail": thumbnail,
            "url": track_url,
        })

    if not songs:
        return jsonify({"error": "La playlist no contiene canciones accesibles."}), 404

    return jsonify({
        "title": info.get("title") or "Playlist",
        "uploader": info.get("uploader") or info.get("channel") or "",
        "count": len(songs),
        "songs": songs,
    })


def choose_caption(info: dict) -> tuple[str | None, str | None]:
    languages = ["es", "es-419", "es-US", "en", "en-US"]
    sources = [info.get("subtitles") or {}, info.get("automatic_captions") or {}]
    for source in sources:
        ordered = languages + [lang for lang in source if lang not in languages]
        for lang in ordered:
            variants = source.get(lang) or []
            preferred = next((v for v in variants if v.get("ext") in {"vtt", "srv3", "json3"}), None)
            if preferred and preferred.get("url"):
                return preferred["url"], lang
    return None, None


def vtt_to_text(raw: str) -> str:
    lines: list[str] = []
    previous = ""
    for line in raw.replace("\ufeff", "").splitlines():
        line = line.strip()
        if not line or line.startswith(("WEBVTT", "Kind:", "Language:")) or "-->" in line or line.isdigit():
            continue
        line = re.sub(r"<[^>]+>", "", line)
        line = html.unescape(line)
        line = re.sub(r"\s+", " ", line).strip()
        if line and line != previous:
            lines.append(line)
            previous = line
    return "\n".join(lines)


@app.post("/api/track-details")
def track_details():
    payload = request.get_json(silent=True) or {}
    url = str(payload.get("url", "")).strip()
    include_lyrics = bool(payload.get("include_lyrics", False))
    if not is_youtube_url(url):
        return jsonify({"error": "URL de canción inválida."}), 400
    options = {"quiet": True, "no_warnings": True, "skip_download": True, "socket_timeout": 15, "retries": 1}
    try:
        with yt_dlp.YoutubeDL(options) as ydl:
            info = ydl.extract_info(url, download=False)
        caption_url, language = choose_caption(info)
        response = {
            "lyrics_available": bool(caption_url),
            "lyrics_language": language,
            "thumbnail": info.get("thumbnail") or "",
        }
        if include_lyrics and caption_url:
            req = Request(caption_url, headers={"User-Agent": "Mozilla/5.0"})
            with urlopen(req, timeout=15) as remote:
                raw = remote.read(2_000_000).decode("utf-8", errors="replace")
            response["lyrics"] = vtt_to_text(raw)
        return jsonify(response)
    except Exception as exc:
        return jsonify({"error": f"No se pudieron consultar los detalles: {exc}"}), 502


def format_selector(fmt: str) -> str:
    if fmt == "opus":
        return "bestaudio[acodec^=opus]/bestaudio"
    if fmt == "m4a":
        return "bestaudio[ext=m4a]/bestaudio"
    return "bestaudio/best"


def related_files(job: DownloadJob, song: dict) -> list[Path]:
    prefix = work_file_prefix(song)
    legacy_prefix = f"{max(1, int(song.get('index') or 1)):04d} - "
    try:
        return [
            path for path in job.download_path.iterdir()
            if path.is_file() and path.name.startswith((prefix, legacy_prefix))
        ]
    except OSError:
        return []


def work_file_prefix(song: dict) -> str:
    index = max(1, int(song.get("index") or 1))
    identity = re.sub(r"[^A-Za-z0-9_-]", "", str(song.get("id") or "track"))[:24] or "track"
    return f".__musicdl_{index:04d}_{identity}__ "


def output_template(job: DownloadJob, song: dict) -> str:
    return str(job.download_path / f"{work_file_prefix(song)}%(title).160B.%(ext)s")


def finalize_audio_filenames(job: DownloadJob, audio_files: list[Path]) -> None:
    for source in audio_files:
        if not source.exists():
            continue
        destination = source.with_name(clean_download_filename(source.name))
        with job.lock:
            destination = available_destination(destination)
            try:
                source.rename(destination)
            except OSError as exc:
                job.warnings.append({
                    "song": {"title": source.stem},
                    "warning": f"Se descargó el audio, pero no se pudo limpiar su nombre: {exc}",
                })


def embed_lyrics_and_cleanup(job: DownloadJob, song: dict, success: bool) -> None:
    files = related_files(job, song)
    auxiliary = {".jpg", ".jpeg", ".png", ".webp", ".vtt", ".srt", ".ass", ".lrc"}
    subtitles = [path for path in files if path.suffix.lower() in {".vtt", ".srt"}]
    audio_files = [path for path in files if path.suffix.lower() in {".opus", ".m4a", ".mp3"}]

    if success and job.options.get("lyrics") and subtitles and audio_files:
        preferred = sorted(subtitles, key=lambda path: (".es" not in path.name.lower(), ".en" not in path.name.lower()))[0]
        try:
            lyrics = vtt_to_text(preferred.read_text(encoding="utf-8", errors="replace"))
            if lyrics:
                audio_path = audio_files[0]
                if audio_path.suffix.lower() == ".mp3":
                    try:
                        tags = ID3(audio_path)
                    except ID3NoHeaderError:
                        tags = ID3()
                    tags.delall("USLT")
                    tags.add(USLT(encoding=3, lang="spa", desc="Letra", text=lyrics))
                    tags.save(audio_path)
                else:
                    audio = MutagenFile(audio_path)
                    if audio is not None:
                        if audio_path.suffix.lower() == ".m4a":
                            audio["©lyr"] = [lyrics]
                        else:
                            audio["LYRICS"] = lyrics
                        audio.save()
        except Exception as exc:
            with job.lock:
                job.warnings.append({"song": song, "warning": f"Audio descargado, pero no se pudo incrustar la letra: {exc}"})

    for path in files:
        if path.suffix.lower() in auxiliary:
            try:
                path.unlink(missing_ok=True)
            except OSError:
                pass
    if success:
        finalize_audio_filenames(job, audio_files)


def terminate_process_tree(process: subprocess.Popen) -> None:
    if process.poll() is not None:
        return
    try:
        if os.name == "nt":
            subprocess.run(
                ["taskkill", "/PID", str(process.pid), "/T", "/F"],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=5,
                check=False,
            )
        else:
            os.killpg(os.getpgid(process.pid), signal.SIGTERM)
    except Exception:
        process.kill()


def build_command(job: DownloadJob, song: dict, player_clients: str = "default,mweb") -> list[str]:
    fmt = job.options["format"]
    output = output_template(job, song)
    cmd = [
        sys.executable, "-m", "yt_dlp", "--ignore-config",
        "--newline", "--no-playlist", "--no-overwrites", "--continue",
        "--socket-timeout", "25", "--retries", "5", "--fragment-retries", "5",
        "--retry-sleep", "http:linear=2::8", "--retry-sleep", "fragment:linear=1::5",
        "--concurrent-fragments", "1", "--sleep-requests", "0.5",
        "--sleep-interval", "1", "--max-sleep-interval", "3",
        "--force-ipv4", "--remote-components", "ejs:github",
        "--extractor-args", f"youtube:player_client={player_clients}", "--extract-audio",
        "--audio-format", fmt, "--audio-quality", "0",
        "--format", format_selector(fmt), "--output", output,
        "--embed-metadata", "--windows-filenames",
    ]
    if job.options.get("cover"):
        size = str(job.options.get("cover_size", 500))
        cmd += [
            "--convert-thumbnails", "jpg", "--embed-thumbnail",
            "--postprocessor-args", f"ThumbnailsConvertor:-vf scale={size}:{size}:force_original_aspect_ratio=increase,crop={size}:{size}",
        ]
    auth_browser = job.options.get("auth_browser")
    if auth_browser in {"chrome", "edge", "firefox"}:
        cmd += ["--cookies-from-browser", auth_browser]
    cmd.append(str(song["url"]))
    return cmd


def invalidate_pot_cache() -> None:
    try:
        request_ = Request(f"{POT_PROVIDER_URL}/invalidate_caches", data=b"", method="POST")
        with urlopen(request_, timeout=5):
            pass
    except Exception:
        # A failed invalidation must not hide the original download error.
        pass


def retry_profiles_for_error(error_text: str) -> list[str]:
    lowered = error_text.lower()
    if "http error 429" in lowered or "too many requests" in lowered:
        return ["web_safari"]
    token_or_403 = (
        "http error 403" in lowered
        or "gvs po token which was not provided" in lowered
        or "po token provider" in lowered
    )
    if token_or_403:
        return ["mweb", "web_safari"]
    if "requested format is not available" in lowered:
        return ["web_safari"]
    if "video unavailable" in lowered:
        return ["web_embedded,android_vr"]
    return []


def build_lyrics_command(job: DownloadJob, song: dict) -> list[str]:
    output = output_template(job, song)
    cmd = [
        sys.executable, "-m", "yt_dlp", "--ignore-config", "--skip-download", "--no-playlist",
        "--socket-timeout", "20", "--retries", "1", "--force-ipv4",
        "--remote-components", "ejs:github",
        "--extractor-args", "youtube:player_client=default,mweb",
        "--write-subs", "--write-auto-subs", "--sub-langs", "es.*,en.*",
        "--sub-format", "vtt/best", "--output", output,
    ]
    auth_browser = job.options.get("auth_browser")
    if auth_browser in {"chrome", "edge", "firefox"}:
        cmd += ["--cookies-from-browser", auth_browser]
    cmd.append(str(song["url"]))
    return cmd


def fetch_lyrics_best_effort(job: DownloadJob, song: dict) -> None:
    if not job.options.get("lyrics") or job.cancel_event.is_set():
        return
    child_env = os.environ.copy()
    child_env["PYTHONUTF8"] = "1"
    child_env["PYTHONIOENCODING"] = "utf-8"
    try:
        # Serializing this optional request avoids triggering YouTube's subtitle
        # rate limit when several audio downloads finish at the same time.
        with lyrics_lock:
            result = subprocess.run(
                build_lyrics_command(job, song),
                cwd=str(APP_DIR),
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                env=child_env,
                timeout=60,
                check=False,
            )
        if result.returncode != 0:
            combined = clean_error_text("\n".join((result.stdout, result.stderr)))
            detail = next((line for line in reversed(combined.splitlines()) if line.strip()), "Letra no disponible")
            with job.lock:
                job.warnings.append({"song": song, "warning": f"Audio descargado; no se pudo añadir la letra: {detail[:500]}"})
    except subprocess.TimeoutExpired:
        with job.lock:
            job.warnings.append({"song": song, "warning": "Audio descargado; la consulta de la letra superó 60 segundos."})
    except Exception as exc:
        with job.lock:
            job.warnings.append({"song": song, "warning": f"Audio descargado; no se pudo consultar la letra: {exc}"})
    finally:
        embed_lyrics_and_cleanup(job, song, success=True)


def build_preflight_command(options: dict, song: dict, player_clients: str = "default,mweb") -> list[str]:
    cmd = [
        sys.executable, "-m", "yt_dlp", "--ignore-config",
        "--simulate", "--get-url", "--no-playlist", "--force-ipv4",
        "--socket-timeout", "25", "--retries", "2",
        "--remote-components", "ejs:github",
        "--extractor-args", f"youtube:player_client={player_clients}",
        "--format", format_selector(str(options.get("format", "opus"))),
    ]
    auth_browser = options.get("auth_browser")
    if auth_browser in {"chrome", "edge", "firefox"}:
        cmd += ["--cookies-from-browser", auth_browser]
    cmd.append(str(song["url"]))
    return cmd


def preflight_download(options: dict, song: dict) -> tuple[bool, str]:
    if not pot_provider_ready():
        return False, "El proveedor automático de tokens de YouTube no está activo. Cierra la aplicación y vuelve a iniciarla con start.bat."
    child_env = os.environ.copy()
    child_env["PYTHONUTF8"] = "1"
    child_env["PYTHONIOENCODING"] = "utf-8"
    profiles = ["default,mweb"]
    index = 0
    detail = ""
    while index < len(profiles):
        try:
            result = subprocess.run(
                build_preflight_command(options, song, profiles[index]),
                cwd=str(APP_DIR),
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                env=child_env,
                timeout=90,
                check=False,
            )
        except subprocess.TimeoutExpired:
            return False, "La prueba de acceso superó 90 segundos. No se inició la cola para evitar cientos de fallos."
        combined = clean_error_text("\n".join(part for part in (result.stdout, result.stderr) if part))
        if result.returncode == 0:
            return True, ""
        useful = [line for line in combined.splitlines() if line.strip()]
        detail = "\n".join(useful[-10:]) or f"yt-dlp terminó con código {result.returncode}"

        # An unavailable first item is a problem with that video, not with the
        # downloader. Let the queue continue so other songs are not blocked.
        if "video unavailable" in detail.lower():
            return True, detail[:4000]

        # A rate limit or an incomplete format list affects this request/video,
        # not necessarily the whole playlist. The per-track retry engine will
        # try alternate clients without blocking every other selected song.
        transient_access_error = any(marker in detail.lower() for marker in (
            "http error 429",
            "too many requests",
            "only images are available",
            "requested format is not available",
        ))

        fallbacks = retry_profiles_for_error(detail)
        for fallback in fallbacks:
            if fallback not in profiles:
                profiles.append(fallback)
        if not fallbacks or index + 1 >= len(profiles):
            break
        if "403" in detail or "po token" in detail.lower():
            invalidate_pot_cache()
        time.sleep(min(3, 1 + index))
        index += 1
        if transient_access_error and index >= len(profiles):
            return True, detail[:4000]
    if any(marker in detail.lower() for marker in (
        "http error 429", "too many requests", "only images are available",
        "requested format is not available",
    )):
        return True, detail[:4000]
    return False, detail[:4000]


def download_one(
    job: DownloadJob,
    song: dict,
    lyrics_executor: concurrent.futures.ThreadPoolExecutor | None = None,
) -> None:
    song_key = str(song.get("id") or uuid.uuid4())
    if job.cancel_event.is_set():
        with job.lock:
            job.cancelled_count += 1
            job.finished += 1
        return

    started = time.time()
    with job.lock:
        job.active[song_key] = {"id": song_key, "title": song.get("title", "Canción"), "started_at": started}

    creation_flags = subprocess.CREATE_NEW_PROCESS_GROUP if os.name == "nt" else 0
    process = None
    completed_successfully = False
    lyrics_scheduled = False
    try:
        child_env = os.environ.copy()
        child_env["PYTHONUTF8"] = "1"
        child_env["PYTHONIOENCODING"] = "utf-8"
        deadline = started + int(job.options.get("timeout_minutes", 15)) * 60
        profiles = ["default,mweb"]
        attempted_profiles: list[str] = []
        final_error = ""
        attempt_index = 0

        while attempt_index < len(profiles):
            profile = profiles[attempt_index]
            attempted_profiles.append(profile)
            with job.lock:
                if song_key in job.active:
                    job.active[song_key]["attempt"] = attempt_index + 1
                    job.active[song_key]["profile"] = profile

            process = subprocess.Popen(
                build_command(job, song, profile),
                cwd=str(APP_DIR),
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                encoding="utf-8",
                errors="replace",
                creationflags=creation_flags,
                start_new_session=(os.name != "nt"),
                env=child_env,
            )
            with job.lock:
                job.processes[song_key] = process

            output_lines: list[str] = []
            output_queue: queue.Queue[str | None] = queue.Queue()

            def read_output(current_process: subprocess.Popen = process) -> None:
                if current_process.stdout:
                    for captured_line in iter(current_process.stdout.readline, ""):
                        output_queue.put(captured_line)
                output_queue.put(None)

            threading.Thread(target=read_output, daemon=True, name=f"output-{song_key[:8]}-{attempt_index}").start()
            while process.poll() is None:
                if job.cancel_event.is_set():
                    terminate_process_tree(process)
                    raise InterruptedError("Cancelada por el usuario")
                if time.time() > deadline:
                    terminate_process_tree(process)
                    raise TimeoutError(f"Superó el límite de {job.options.get('timeout_minutes', 15)} minutos")
                try:
                    line = output_queue.get(timeout=0.2)
                    if line:
                        output_lines.append(line.rstrip())
                        output_lines = output_lines[-40:]
                except queue.Empty:
                    pass
            while True:
                try:
                    line = output_queue.get_nowait()
                    if line:
                        output_lines.append(line.rstrip())
                except queue.Empty:
                    break

            if process.returncode == 0:
                final_error = ""
                break

            meaningful = [line for line in output_lines if line.strip()]
            final_error = clean_error_text("\n".join(meaningful[-12:])) or f"yt-dlp terminó con código {process.returncode}"
            fallbacks = retry_profiles_for_error(final_error)
            for fallback in fallbacks:
                if fallback not in profiles:
                    profiles.append(fallback)
            if not fallbacks or attempt_index + 1 >= len(profiles):
                break

            if "403" in final_error or "po token" in final_error.lower():
                invalidate_pot_cache()
            embed_lyrics_and_cleanup(job, song, success=False)
            time.sleep(min(5, 2 + attempt_index))
            attempt_index += 1

        if final_error:
            tried = ", ".join(attempted_profiles)
            if "video unavailable" in final_error.lower():
                raise RuntimeError(
                    f"El video original ya no está disponible en YouTube. Se probaron otros clientes ({tried}) sin éxito.\n{final_error}"
                )
            raise RuntimeError(f"Falló después de probar estos clientes: {tried}.\n{final_error}")

        if job.options.get("lyrics") and lyrics_executor is not None:
            lyrics_executor.submit(fetch_lyrics_best_effort, job, song)
            lyrics_scheduled = True
        with job.lock:
            job.succeeded += 1
            result = {"song": song, "status": "completed", "profile": attempted_profiles[-1]}
            if len(attempted_profiles) > 1:
                result["recovered_after_retry"] = True
                job.warnings.append({
                    "song": song,
                    "warning": f"Descarga recuperada automáticamente tras {len(attempted_profiles)} intentos.",
                })
            job.results.append(result)
        completed_successfully = True
    except InterruptedError as exc:
        with job.lock:
            job.cancelled_count += 1
            job.results.append({"song": song, "status": "cancelled", "error": str(exc)})
    except Exception as exc:
        error_text = clean_error_text(str(exc))[:4000]
        with job.lock:
            job.failed += 1
            job.errors.append({"song": song, "error": error_text})
            job.results.append({"song": song, "status": "failed", "error": error_text})
            auth_blocked = "sign in to confirm you’re not a bot" in error_text.lower() or "sign in to confirm you're not a bot" in error_text.lower()
            ejs_blocked = "the page needs to be reloaded" in error_text.lower() or "signature solving failed" in error_text.lower()
            if auth_blocked or ejs_blocked:
                job.fatal_error = (
                    "YouTube exige una sesión válida. Abre la aplicación en un navegador distinto, cierra por completo el navegador cuyas cookies usarás y vuelve a intentarlo con esa sesión seleccionada."
                    if auth_blocked else
                    "YouTube rechazó el desafío JavaScript. Reinicia la aplicación para instalar/cargar el solucionador EJS y vuelve a intentarlo."
                )
                job.cancel_event.set()
    finally:
        if not completed_successfully or not lyrics_scheduled:
            embed_lyrics_and_cleanup(job, song, completed_successfully)
        with job.lock:
            job.active.pop(song_key, None)
            job.processes.pop(song_key, None)
            job.finished += 1


def run_job(job: DownloadJob) -> None:
    with job.lock:
        job.status = "downloading"
    workers = max(1, min(4, int(job.options.get("concurrency", 2))))
    lyrics_executor = concurrent.futures.ThreadPoolExecutor(max_workers=1, thread_name_prefix="lyrics")
    try:
        with concurrent.futures.ThreadPoolExecutor(max_workers=workers, thread_name_prefix="track") as executor:
            futures = [executor.submit(download_one, job, song, lyrics_executor) for song in job.songs]
            for future in concurrent.futures.as_completed(futures):
                try:
                    future.result()
                except Exception:
                    pass
        with job.lock:
            if job.options.get("lyrics") and not job.cancel_event.is_set():
                job.status = "adding_lyrics"
    finally:
        lyrics_executor.shutdown(wait=True, cancel_futures=job.cancel_event.is_set())
    with job.lock:
        job.status = "blocked" if job.fatal_error else ("cancelled" if job.cancel_event.is_set() else ("completed_with_errors" if job.failed else "completed"))


@app.post("/api/downloads")
def start_download():
    payload = request.get_json(silent=True) or {}
    songs = payload.get("songs") or []
    if not isinstance(songs, list) or not songs:
        return jsonify({"error": "Selecciona al menos una canción."}), 400
    if len(songs) > 5000:
        return jsonify({"error": "El límite es de 5000 canciones por trabajo."}), 400
    if any(not isinstance(song, dict) or not is_youtube_url(song.get("url", "")) for song in songs):
        return jsonify({"error": "Todas las canciones deben tener una URL HTTPS de YouTube."}), 400
    if not ffmpeg_available():
        return jsonify({"error": "FFmpeg no está disponible. Instálalo o añádelo al PATH antes de descargar."}), 503

    fmt = str(payload.get("format", "opus")).lower()
    if fmt not in {"opus", "m4a", "mp3"}:
        return jsonify({"error": "Formato no permitido."}), 400
    cover_size = int(payload.get("cover_size", 500))
    if cover_size not in {320, 500, 1000}:
        cover_size = 500

    root_value = str(payload.get("download_root") or DEFAULT_DOWNLOAD_DIR).strip()
    if is_network_path(root_value):
        return jsonify({"error": "Selecciona una carpeta de una unidad local."}), 400
    root = Path(root_value).expanduser().resolve()
    try:
        root.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        return jsonify({"error": f"No se puede usar la carpeta de destino: {exc}"}), 400
    playlist_dir = root / safe_folder_name(str(payload.get("playlist_name") or "Playlist"))
    playlist_dir.mkdir(parents=True, exist_ok=True)
    remove_video_ids_from_filenames(playlist_dir)

    job_id = str(uuid.uuid4())
    options = {
        "format": fmt,
        "cover": bool(payload.get("cover", True)),
        "cover_size": cover_size,
        "lyrics": bool(payload.get("lyrics", True)),
        "concurrency": max(1, min(4, int(payload.get("concurrency", 2)))),
        "timeout_minutes": max(2, min(60, int(payload.get("timeout_minutes", 15)))),
        "auth_browser": str(payload.get("auth_browser") or "none").lower(),
    }
    access_ok, access_error = preflight_download(options, songs[0])
    if not access_ok:
        return jsonify({
            "error": "La prueba previa falló; no se inició la cola completa. " + access_error,
            "preflight_failed": True,
        }), 422
    job = DownloadJob(job_id, songs, playlist_dir, options)
    with jobs_lock:
        jobs[job_id] = job
    threading.Thread(target=run_job, args=(job,), daemon=True, name=f"job-{job_id[:8]}").start()
    return jsonify({"job_id": job_id, "download_path": str(playlist_dir)}), 202


@app.get("/api/downloads/<job_id>")
def download_status(job_id: str):
    with jobs_lock:
        job = jobs.get(job_id)
    if not job:
        return jsonify({"error": "Trabajo no encontrado."}), 404
    return jsonify(public_job(job))


@app.post("/api/downloads/<job_id>/cancel")
def cancel_download(job_id: str):
    with jobs_lock:
        job = jobs.get(job_id)
    if not job:
        return jsonify({"error": "Trabajo no encontrado."}), 404
    job.cancel_event.set()
    with job.lock:
        job.status = "cancelling"
        running = list(job.processes.values())
    for process in running:
        terminate_process_tree(process)
    return jsonify({"ok": True, "status": "cancelling"})


@app.post("/api/open-folder")
def open_folder():
    payload = request.get_json(silent=True) or {}
    path_value = str(payload.get("path") or DEFAULT_DOWNLOAD_DIR)
    if is_network_path(path_value):
        return jsonify({"error": "Selecciona una carpeta de una unidad local."}), 400
    path = Path(path_value).resolve()
    if not path.exists() or not path.is_dir():
        return jsonify({"error": "La carpeta no existe."}), 404
    try:
        if os.name == "nt":
            os.startfile(str(path))
        elif sys.platform == "darwin":
            subprocess.Popen(["open", str(path)])
        else:
            subprocess.Popen(["xdg-open", str(path)])
        return jsonify({"ok": True})
    except OSError as exc:
        return jsonify({"error": str(exc)}), 500


@app.post("/api/select-folder")
def select_folder():
    payload = request.get_json(silent=True) or {}
    initial_value = str(payload.get("initial") or DEFAULT_DOWNLOAD_DIR)
    if is_network_path(initial_value):
        return jsonify({"error": "Selecciona una carpeta de una unidad local."}), 400
    initial = Path(initial_value).expanduser()
    if not initial.is_dir():
        initial = DEFAULT_DOWNLOAD_DIR
    try:
        import tkinter as tk
        from tkinter import filedialog

        window = tk.Tk()
        window.withdraw()
        window.attributes("-topmost", True)
        window.update()
        try:
            selected = filedialog.askdirectory(
                parent=window,
                title="Elegir carpeta base para las descargas",
                initialdir=str(initial),
                mustexist=True,
            )
        finally:
            window.destroy()
        return jsonify({"path": selected or "", "cancelled": not bool(selected)})
    except Exception as exc:
        return jsonify({"error": f"No se pudo abrir el selector de carpetas: {exc}"}), 500


def open_local_app() -> None:
    url = "http://127.0.0.1:8766"
    if os.name == "nt":
        chrome_candidates = [
            Path(os.environ.get("PROGRAMFILES", "")) / "Google/Chrome/Application/chrome.exe",
            Path(os.environ.get("PROGRAMFILES(X86)", "")) / "Google/Chrome/Application/chrome.exe",
            Path(os.environ.get("LOCALAPPDATA", "")) / "Google/Chrome/Application/chrome.exe",
        ]
        chrome = next((path for path in chrome_candidates if str(path) and path.is_file()), None)
        if chrome:
            # Chrome reuses the currently active window and opens a new tab.
            # If Chrome is closed, it will start a window normally.
            subprocess.Popen([str(chrome), url], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            return
        edge_candidates = [
            Path(os.environ.get("PROGRAMFILES(X86)", "")) / "Microsoft/Edge/Application/msedge.exe",
            Path(os.environ.get("PROGRAMFILES", "")) / "Microsoft/Edge/Application/msedge.exe",
            Path(os.environ.get("LOCALAPPDATA", "")) / "Microsoft/Edge/Application/msedge.exe",
        ]
        edge = next((path for path in edge_candidates if str(path) and path.is_file()), None)
        if edge:
            subprocess.Popen([str(edge), url], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            return
    webbrowser.open(url)


if __name__ == "__main__":
    if not start_pot_provider():
        raise SystemExit("No se pudo iniciar el proveedor automático de tokens. Ejecuta start.bat para repararlo.")
    threading.Timer(1.0, open_local_app).start()
    app.run(host="127.0.0.1", port=8766, debug=False, threaded=True)
