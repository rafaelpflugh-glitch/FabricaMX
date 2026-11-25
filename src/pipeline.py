"""Orquestrador principal da pipeline FábricaMX com suporte a lotes e callbacks em tempo real."""

from __future__ import annotations

import shutil
import threading
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

from src import TEMP_DIR, append_history
from src.media_fetcher import fetch_and_prepare_images
from src.scraper import NewsArticle, fetch_next_news
from src.scriptwriter import ScriptPackage, generate_script, pick_next_top5_topic
from src.tts_subtitles import generate_narration_and_subtitles
from src.video_engine import render_video

LogCallback = Callable[[str], None]
ProgressCallback = Callable[[int, int, str, float], None]
PreviewCallback = Callable[[NewsArticle | None, ScriptPackage | None], None]


@dataclass
class BatchItemResult:
    """Resultado de um vídeo individual dentro de um lote."""

    index: int
    success: bool
    article: NewsArticle | None = None
    script: ScriptPackage | None = None
    mp4_path: Path | None = None
    txt_path: Path | None = None
    error: str = ""


class FabricaPipeline:
    """Orquestra as 5 etapas da produção de vídeos verticais da Liga MX."""

    STAGES = [
        "1/5 Coletando notícia",
        "2/5 Roteiro Ollama",
        "3/5 Imagens",
        "4/5 Voz/Legenda",
        "5/5 Render FFmpeg",
    ]

    def __init__(
        self,
        config: dict[str, Any],
        on_log: LogCallback | None = None,
        on_progress: ProgressCallback | None = None,
        on_preview: PreviewCallback | None = None,
        stop_event: threading.Event | None = None,
    ) -> None:
        self.config = config
        self.on_log = on_log
        self.on_progress = on_progress
        self.on_preview = on_preview
        self.stop_event = stop_event or threading.Event()

    def _log(self, message: str) -> None:
        if self.on_log:
            self.on_log(message)

    def _progress(self, step: int, stage_label: str, overall_ratio: float) -> None:
        if self.on_progress:
            self.on_progress(step, 5, stage_label, max(0.0, min(1.0, overall_ratio)))

    def _check_cancelled(self) -> None:
        if self.stop_event.is_set():
            raise RuntimeError("Processo interrompido pelo usuário.")

    def _cleanup_job_dir(self, job_dir: Path) -> None:
        try:
            if job_dir.exists():
                shutil.rmtree(job_dir, ignore_errors=True)
        except Exception:
            pass

    def _cleanup_temp_root(self) -> None:
        if not TEMP_DIR.exists():
            return
        for child in TEMP_DIR.iterdir():
            if child.name == ".gitkeep":
                continue
            try:
                if child.is_dir():
                    shutil.rmtree(child, ignore_errors=True)
                else:
                    child.unlink(missing_ok=True)
            except Exception:
                pass

    def run_single(
        self,
        batch_index: int = 1,
        batch_total: int = 1,
        exclude_urls: set[str] | None = None,
    ) -> BatchItemResult:
        """Executa a pipeline completa para 1 vídeo."""
        prefix = f"[Vídeo {batch_index}/{batch_total}] " if batch_total > 1 else ""
        job_id = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
        job_dir = TEMP_DIR / f"job_{job_id}"
        job_dir.mkdir(parents=True, exist_ok=True)

        limpar_temp = bool(self.config.get("video", {}).get("limpar_temp", True))
        article: NewsArticle | None = None
        script: ScriptPackage | None = None

        try:
            # Etapa 1/5: Coletando notícia ou selecionando tema Top 5 Viral
            self._check_cancelled()
            self._progress(1, f"{prefix}{self.STAGES[0]}", 0.05)
            modo = str(self.config.get("modo_conteudo", "noticia")).strip().lower()
            if modo == "top5":
                self._log(f"{prefix}Iniciando etapa 1/5: Selecionando tema Top 5 Viral estilo @yaguilas1...")
                article = pick_next_top5_topic(
                    club_focus=self.config.get("clube_foco", "Club América"),
                    exclude_urls=exclude_urls,
                )
                self._log(f"{prefix}[Top5] Tema escolhido: {article.title}")
            else:
                self._log(f"{prefix}Iniciando etapa 1/5: Coletando notícia quente da Liga MX...")
                article = fetch_next_news(
                    config=self.config,
                    exclude_urls=exclude_urls,
                    log_cb=self._log,
                )
            if self.on_preview:
                self.on_preview(article, None)
            self._progress(1, f"{prefix}{self.STAGES[0]}", 0.18)

            # Etapa 2/5: Roteiro Ollama
            self._check_cancelled()
            self._progress(2, f"{prefix}{self.STAGES[1]}", 0.22)
            self._log(f"{prefix}Iniciando etapa 2/5: Gerando roteiro no Ollama...")
            script = generate_script(
                article=article,
                config=self.config,
                log_cb=self._log,
            )
            if self.on_preview:
                self.on_preview(article, script)
            self._progress(2, f"{prefix}{self.STAGES[1]}", 0.40)

            # Etapa 3/5: Imagens
            self._check_cancelled()
            self._progress(3, f"{prefix}{self.STAGES[2]}", 0.44)
            self._log(f"{prefix}Iniciando etapa 3/5: Buscando e normalizando imagens (1080x1920)...")
            image_paths = fetch_and_prepare_images(
                search_terms=script.termos_busca_imagens,
                output_dir=job_dir,
                config=self.config,
                og_image=article.og_image,
                article_images=article.article_images,
                log_cb=self._log,
            )
            self._progress(3, f"{prefix}{self.STAGES[2]}", 0.62)

            # Etapa 4/5: Voz / Legenda
            self._check_cancelled()
            self._progress(4, f"{prefix}{self.STAGES[3]}", 0.66)
            self._log(f"{prefix}Iniciando etapa 4/5: Gerando narração Edge-TTS e legendas .ass...")
            tts_result = generate_narration_and_subtitles(
                script_text=script.roteiro,
                work_dir=job_dir,
                config=self.config,
                log_cb=self._log,
            )
            self._progress(4, f"{prefix}{self.STAGES[3]}", 0.78)

            # Etapa 5/5: Render FFmpeg
            self._check_cancelled()
            self._progress(5, f"{prefix}{self.STAGES[4]}", 0.80)
            self._log(f"{prefix}Iniciando etapa 5/5: Renderizando vídeo final via FFmpeg...")

            def _on_ffmpeg_pct(ratio: float) -> None:
                self._progress(5, f"{prefix}{self.STAGES[4]} ({int(ratio * 100)}%)", 0.80 + 0.20 * ratio)

            mp4_path, txt_path = render_video(
                image_paths=image_paths,
                audio_path=tts_result.audio_path,
                ass_path=tts_result.ass_path,
                audio_duration=tts_result.duration,
                gancho_tela=script.gancho_tela,
                article_title=article.title,
                article_source=article.source,
                article_url=article.url,
                descricao_tiktok=script.descricao_tiktok,
                hashtags=script.hashtags,
                roteiro=script.roteiro,
                work_dir=job_dir,
                config=self.config,
                log_cb=self._log,
                progress_cb=_on_ffmpeg_pct,
                stop_event=self.stop_event,
            )

            # Só atualiza history.json quando o .mp4 foi concluído com sucesso!
            if mp4_path.exists() and mp4_path.stat().st_size > 10_000:
                append_history(
                    url=article.url,
                    title=article.title,
                    source=article.source,
                    output_file=mp4_path.name,
                )
                self._log(f"{prefix}Histórico atualizado em history.json.")

            self._progress(5, f"{prefix}Concluído!", 1.0)
            return BatchItemResult(
                index=batch_index,
                success=True,
                article=article,
                script=script,
                mp4_path=mp4_path,
                txt_path=txt_path,
            )

        except Exception as exc:
            self._log(f"{prefix}ERRO: {exc}")
            return BatchItemResult(
                index=batch_index,
                success=False,
                article=article,
                script=script,
                error=str(exc),
            )
        finally:
            if limpar_temp:
                self._cleanup_job_dir(job_dir)
                self._log(f"{prefix}Arquivos temporários limpos.")

    def run_batch(self, count: int = 1) -> list[BatchItemResult]:
        """Executa geração em lote de N vídeos (1 a 5), evitando repetir notícias."""
        total = max(1, min(5, int(count)))
        results: list[BatchItemResult] = []
        used_urls: set[str] = set()

        self._log(f"=== Iniciando lote de {total} vídeo(s) | Foco: {self.config.get('clube_foco')} ===")

        for idx in range(1, total + 1):
            if self.stop_event.is_set():
                self._log("Lote interrompido pelo usuário.")
                break

            res = self.run_single(batch_index=idx, batch_total=total, exclude_urls=used_urls)
            results.append(res)
            if res.article and res.article.url:
                used_urls.add(res.article.url)

            if self.stop_event.is_set():
                break

        if bool(self.config.get("video", {}).get("limpar_temp", True)):
            self._cleanup_temp_root()

        ok_count = sum(1 for r in results if r.success)
        self._log(f"=== Lote finalizado: {ok_count}/{total} vídeo(s) gerados com sucesso. ===")
        return results
