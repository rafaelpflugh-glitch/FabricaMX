"""Script de diagnóstico completo do ambiente FábricaMX."""

from __future__ import annotations

import importlib
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

from PIL import Image

from src import (
    BRANDING_DIR,
    CONFIG_PATH,
    HISTORY_PATH,
    INTROS_DIR,
    MUSIC_DIR,
    OUTPUT_DIR,
    TEMP_DIR,
    load_config,
)
from src.scriptwriter import check_ollama_health
from src.tts_subtitles import WordTiming, build_tiktok_ass
from src.video_engine import create_hook_banner_png, escape_ffmpeg_filter_path

REQUIRED_MODULES = [
    ("customtkinter", "customtkinter"),
    ("feedparser", "feedparser"),
    ("beautifulsoup4", "bs4"),
    ("requests", "requests"),
    ("edge-tts", "edge_tts"),
    ("Pillow", "PIL"),
    ("duckduckgo-search", "duckduckgo_search"),
]

INTERNAL_MODULES = [
    "src.scraper",
    "src.scriptwriter",
    "src.media_fetcher",
    "src.tts_subtitles",
    "src.video_engine",
    "src.pipeline",
]


def _print_result(ok: bool, label: str, detail: str = "") -> None:
    tag = "[ OK ]" if ok else "[ERRO]"
    suffix = f" -> {detail}" if detail else ""
    print(f"  {tag} {label}{suffix}")


def check_python() -> bool:
    ver = f"{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}"
    _print_result(True, "Python", f"{ver} ({sys.executable})")
    return True


def check_structure() -> bool:
    all_ok = True
    paths = [
        ("config.json", CONFIG_PATH, False),
        ("history.json", HISTORY_PATH, False),
        ("assets/music/", MUSIC_DIR, True),
        ("assets/intros/", INTROS_DIR, True),
        ("assets/branding/", BRANDING_DIR, True),
        ("temp/", TEMP_DIR, True),
        ("output/", OUTPUT_DIR, True),
    ]
    for name, path, is_dir in paths:
        exists = path.is_dir() if is_dir else path.is_file()
        _print_result(exists, f"Estrutura: {name}", str(path))
        all_ok = all_ok and exists
    return all_ok


def check_libraries() -> bool:
    all_ok = True
    for pkg_name, import_name in REQUIRED_MODULES:
        try:
            mod = importlib.import_module(import_name)
            ver = getattr(mod, "__version__", "ok")
            _print_result(True, f"Biblioteca {pkg_name}", str(ver))
        except Exception as exc:
            _print_result(False, f"Biblioteca {pkg_name}", str(exc))
            all_ok = False

    for mod_name in INTERNAL_MODULES:
        try:
            importlib.import_module(mod_name)
            _print_result(True, f"Módulo interno {mod_name}")
        except Exception as exc:
            _print_result(False, f"Módulo interno {mod_name}", str(exc))
            all_ok = False

    return all_ok


def check_ffmpeg_and_ass_smoke(config: dict) -> bool:
    ffmpeg_bin = shutil.which("ffmpeg")
    ffprobe_bin = shutil.which("ffprobe")
    if not ffmpeg_bin or not ffprobe_bin:
        _print_result(False, "FFmpeg/FFprobe", "Não encontrados no PATH.")
        return False

    try:
        out = subprocess.check_output(
            [ffmpeg_bin, "-version"],
            text=True,
            timeout=10,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        first_line = out.splitlines()[0] if out else "desconhecido"
        _print_result(True, "FFmpeg", first_line)
    except Exception as exc:
        _print_result(False, "FFmpeg", str(exc))
        return False

    # Smoke test real no Windows: zoompan + xfade + banner PNG + ass= com caminho absoluto
    tmp_dir = Path(tempfile.mkdtemp(prefix="fabricamx_smoke_", dir=str(TEMP_DIR)))
    try:
        img1 = tmp_dir / "img1.jpg"
        img2 = tmp_dir / "img2.jpg"
        Image.new("RGB", (1080, 1920), (15, 32, 65)).save(img1, "JPEG")
        Image.new("RGB", (1080, 1920), (45, 25, 10)).save(img2, "JPEG")

        banner_png = tmp_dir / "banner.png"
        create_hook_banner_png("¡BOMBAZO EN COAPA!", banner_png, club_label="CLUB AMÉRICA")

        ass_path = tmp_dir / "smoke.ass"
        words = [
            WordTiming("¡BOMBAZO", 0.0, 0.5),
            WordTiming("EN", 0.5, 0.8),
            WordTiming("COAPA!", 0.8, 1.3),
            WordTiming("LIGA", 1.3, 1.6),
            WordTiming("MX!", 1.6, 2.0),
        ]
        build_tiktok_ass(words, ass_path, config)

        escaped_ass = escape_ffmpeg_filter_path(ass_path)
        filter_script = tmp_dir / "smoke_filter.txt"
        filter_content = ";\n".join(
            [
                "[0:v]scale=1296:2304,zoompan=z='1.0+0.10*(on/35)':x='iw/2-(iw/zoom/2)':y='ih/2-(ih/zoom/2)':d=36:s=1080x1920:fps=30,setpts=PTS-STARTPTS,settb=AVTB,fps=30,format=yuv420p,setsar=1[s0]",
                "[1:v]scale=1296:2304,zoompan=z='1.10-0.10*(on/35)':x='iw/2-(iw/zoom/2)':y='ih/2-(ih/zoom/2)':d=36:s=1080x1920:fps=30,setpts=PTS-STARTPTS,settb=AVTB,fps=30,format=yuv420p,setsar=1[s1]",
                "[s0][s1]xfade=transition=hblur:duration=0.35:offset=0.85[xf]",
                "[xf]vignette=PI/4.8,eq=brightness=-0.04:contrast=1.05[vg]",
                "[2:v]format=rgba,fade=t=in:st=0:d=0.2:alpha=1,fade=t=out:st=1.5:d=0.3:alpha=1[bn]",
                "[vg][bn]overlay=0:0:eof_action=pass:enable='between(t,0,1.8)'[vb]",
                f"[vb]ass=filename='{escaped_ass}',format=yuv420p[vout]",
            ]
        )
        filter_script.write_text(filter_content, encoding="utf-8")

        out_mp4 = tmp_dir / "smoke_out.mp4"
        cmd = [
            ffmpeg_bin,
            "-y",
            "-hide_banner",
            "-i",
            str(img1),
            "-i",
            str(img2),
            "-loop",
            "1",
            "-t",
            "2.0",
            "-i",
            str(banner_png),
            "-/filter_complex",
            str(filter_script),
            "-map",
            "[vout]",
            "-t",
            "2.0",
            "-c:v",
            "libx264",
            "-preset",
            "ultrafast",
            "-pix_fmt",
            "yuv420p",
            str(out_mp4),
        ]
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=60,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        if proc.returncode != 0 or not out_mp4.exists() or out_mp4.stat().st_size < 5000:
            err_tail = "\n".join(proc.stderr.splitlines()[-10:])
            _print_result(False, "FFmpeg Smoke Test (zoompan+xfade+banner+ass)", err_tail)
            return False

        _print_result(
            True,
            "FFmpeg Smoke Test (zoompan + xfade + banner PNG + ass= Windows)",
            f"OK ({out_mp4.stat().st_size // 1024} KB gerados e validados)",
        )
        return True
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)


def check_ollama(config: dict) -> bool:
    ollama_url = config.get("ollama_url", "http://localhost:11434")
    configured_model = config.get("ollama_modelo", "llama3.1:8b")

    alive, models, err = check_ollama_health(ollama_url)
    if not alive:
        _print_result(False, f"Ollama ({ollama_url})", f"Indisponível ({err})")
        return False

    if not models:
        _print_result(
            False,
            f"Ollama ({ollama_url})",
            f"Servidor ativo, mas nenhum modelo baixado. Execute: ollama pull {configured_model}",
        )
        return False

    has_configured = configured_model in models
    if has_configured:
        _print_result(
            True,
            f"Ollama ({ollama_url})",
            f"Modelo '{configured_model}' pronto | Instalados: {', '.join(models)}",
        )
        return True

    _print_result(
        True,
        f"Ollama ({ollama_url})",
        f"Servidor ativo! Modelos instalados: {', '.join(models)} "
        f"(aviso: config.json aponta para '{configured_model}')",
    )
    return True


def main() -> int:
    print("=" * 68)
    print("  FÁBRICAMX — VERIFICAÇÃO DE AMBIENTE")
    print("=" * 68)

    config = load_config()
    results = [
        check_python(),
        check_structure(),
        check_libraries(),
        check_ffmpeg_and_ass_smoke(config),
        check_ollama(config),
    ]

    print("=" * 68)
    if all(results):
        print("  STATUS FINAL: TUDO PRONTO! Execute 'app.py' com o Python do .venv.")
        print("=" * 68)
        return 0

    print("  STATUS FINAL: VERIFIQUE OS ITENS COM [ERRO] ACIMA.")
    print("=" * 68)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
