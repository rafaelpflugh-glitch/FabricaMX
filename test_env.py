"""Script de diagnóstico completo do ambiente Fábrica TK (Multi-Nicho)."""

from __future__ import annotations

import importlib
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

from PIL import Image

from src import CONFIG_PATH, NICHES_DIR, TEMP_DIR, build_runtime_config, get_niche_paths, list_niches
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
    ("ddgs", "ddgs"),
    ("yt-dlp", "yt_dlp"),
]

INTERNAL_MODULES = [
    "src.scraper",
    "src.scriptwriter",
    "src.media_fetcher",
    "src.video_fetcher",
    "src.scene_matcher",
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
    _print_result(CONFIG_PATH.is_file(), "config.json", str(CONFIG_PATH))
    all_ok &= CONFIG_PATH.is_file()

    _print_result(NICHES_DIR.is_dir(), "Pasta nichos/", str(NICHES_DIR))
    all_ok &= NICHES_DIR.is_dir()

    niches = list_niches()
    _print_result(len(niches) >= 2, "Nichos detectados", ", ".join(n["id"] for n in niches))
    all_ok &= len(niches) >= 2

    for n in niches:
        paths = get_niche_paths(n["id"])
        ok_n = all(paths[k].is_dir() for k in ("output_dir", "music_dir", "intros_dir", "broll_dir", "branding_dir"))
        _print_result(ok_n, f"Nicho '{n['nome']}'", str(paths["root"]))
        all_ok &= ok_n

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
        _print_result(True, "FFmpeg", out.splitlines()[0] if out else "ok")
    except Exception as exc:
        _print_result(False, "FFmpeg", str(exc))
        return False

    tmp_dir = Path(tempfile.mkdtemp(prefix="fabricatk_smoke_", dir=str(TEMP_DIR)))
    try:
        img1 = tmp_dir / "img1.jpg"
        img2 = tmp_dir / "img2.jpg"
        Image.new("RGB", (1080, 1920), (20, 15, 55)).save(img1, "JPEG")
        Image.new("RGB", (1080, 1920), (60, 15, 45)).save(img2, "JPEG")

        banner_png = tmp_dir / "banner.png"
        create_hook_banner_png("GTA VI SEGREDO!", banner_png, club_label="GTA VI")

        ass_path = tmp_dir / "smoke.ass"
        words = [
            WordTiming("DETALHE", 0.0, 0.5),
            WordTiming("OCULTO", 0.5, 1.0),
            WordTiming("GTA", 1.0, 1.4),
            WordTiming("VI!", 1.4, 2.0),
        ]
        build_tiktok_ass(words, ass_path, config)

        escaped_ass = escape_ffmpeg_filter_path(ass_path)
        filter_script = tmp_dir / "smoke_filter.txt"
        filter_content = ";\n".join(
            [
                "[0:v]scale=1382:2458,zoompan=z='1.0+0.15*(on/35)':x='iw/2-(iw/zoom/2)':y='ih/2-(ih/zoom/2)':d=36:s=1080x1920:fps=30,setpts=PTS-STARTPTS,settb=AVTB,fps=30,format=yuv420p,setsar=1[s0]",
                "[1:v]scale=1382:2458,zoompan=z='1.15-0.15*(on/35)':x='iw/2-(iw/zoom/2)':y='ih/2-(ih/zoom/2)':d=36:s=1080x1920:fps=30,setpts=PTS-STARTPTS,settb=AVTB,fps=30,format=yuv420p,setsar=1[s1]",
                "[s0][s1]xfade=transition=hblur:duration=0.20:offset=0.90[xf]",
                f"[xf]ass=filename='{escaped_ass}',format=yuv420p[vout]",
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
            _print_result(False, "FFmpeg Smoke Test", "\n".join(proc.stderr.splitlines()[-10:]))
            return False

        _print_result(True, "FFmpeg Smoke Test (zoompan + xfade + ass= Windows)", f"OK ({out_mp4.stat().st_size // 1024} KB)")
        return True
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)


def check_ollama(config: dict) -> bool:
    ollama_url = config.get("ollama_url", "http://localhost:11434")
    configured_model = config.get("ollama_modelo", "qwen3:8b")

    alive, models, err = check_ollama_health(ollama_url)
    if not alive:
        _print_result(False, f"Ollama ({ollama_url})", f"Indisponível ({err})")
        return False
    if not models:
        _print_result(False, f"Ollama ({ollama_url})", f"Nenhum modelo baixado. Execute: ollama pull {configured_model}")
        return False

    status = f"Modelo padrão '{configured_model}' pronto" if configured_model in models else f"Modelos disponíveis: {', '.join(models)}"
    _print_result(True, f"Ollama ({ollama_url})", f"{status} | Total: {len(models)} modelos")
    return True


def main() -> int:
    print("=" * 68)
    print("  FÁBRICA TK — VERIFICAÇÃO DE AMBIENTE MULTI-NICHO")
    print("=" * 68)

    config = build_runtime_config()
    results = [
        check_python(),
        check_structure(),
        check_libraries(),
        check_ffmpeg_and_ass_smoke(config),
        check_ollama(config),
    ]

    print("=" * 68)
    if all(results):
        print("  STATUS FINAL: TUDO PRONTO! Execute 'iniciar_fabrica_tk.bat'.")
        print("=" * 68)
        return 0

    print("  STATUS FINAL: VERIFIQUE OS ITENS COM [ERRO] ACIMA.")
    print("=" * 68)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
