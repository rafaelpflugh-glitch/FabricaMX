"""Orquestrador multi-nicho da pipeline Fábrica TK com callbacks de progresso e histórico isolado."""

from __future__ import annotations

import shutil
import threading
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

from src import HISTORY_PATH, TEMP_DIR, append_history
from src.media_fetcher import fetch_and_prepare_images
from src.scene_matcher import index_video_pool, match_scenes_to_narration
from src.scraper import NewsArticle, fetch_next_news
from src.scriptwriter import ScriptPackage, generate_script, pick_next_top5_topic
from src.tts_subtitles import build_tiktok_ass, generate_narration_and_subtitles
from src.video_engine import render_original_cut_video, render_video
from src.video_fetcher import ensure_broll_videos
from src.youtube_transcript import run_original_cut_stage, run_youtube_adapter_stage

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
    """Orquestra as 5 etapas da produção de vídeos verticais em qualquer nicho configurado."""

    STAGES = [
        "1/5 Coletando pauta",
        "2/5 Roteiro IA",
        "3/5 Cenas/Imagens",
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
        """Executa a pipeline completa para 1 vídeo no nicho ativo."""
        prefix = f"[Vídeo {batch_index}/{batch_total}] " if batch_total > 1 else ""
        job_id = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
        job_dir = TEMP_DIR / f"job_{job_id}"
        job_dir.mkdir(parents=True, exist_ok=True)

        limpar_temp = bool(self.config.get("video", {}).get("limpar_temp", True))
        paths_dict = self.config.get("paths") or {}
        history_path = Path(paths_dict["history_path"]) if paths_dict.get("history_path") else HISTORY_PATH
        niche_name = str(self.config.get("nicho_nome", "Fábrica TK"))

        article: NewsArticle | None = None
        script: ScriptPackage | None = None

        try:
            modo = str(self.config.get("modo_conteudo", "noticia")).strip().lower()
            custom_video_url = str(self.config.get("video_url_opcional") or "").strip()
            placar_texto = str(self.config.get("placar_opcional") or "").strip()

            # ==================== MODO CORTADOR VIRAL (ÁUDIO ORIGINAL) ====================
            if modo == "corte_original":
                self._check_cancelled()
                self._progress(1, f"{prefix}1/5 Buscando vídeo YouTube", 0.08)
                self._log(f"{prefix}Modo Cortador Viral (Áudio Original) [{niche_name}]...")
                cut_plan = run_original_cut_stage(
                    config=self.config,
                    work_dir=job_dir,
                    exclude_urls=exclude_urls,
                    custom_url=custom_video_url,
                    log_cb=self._log,
                )
                article = cut_plan.article
                script = ScriptPackage(
                    roteiro=cut_plan.transcript_excerpt,
                    gancho_tela=cut_plan.gancho_tela,
                    descricao_tiktok=cut_plan.descricao_tiktok,
                    hashtags=cut_plan.hashtags,
                    word_count=len(cut_plan.word_timings),
                )
                if self.on_preview:
                    self.on_preview(article, script)
                self._progress(3, f"{prefix}3/5 Gerando legendas .ass", 0.58)

                ass_path = job_dir / "cut_subtitles.ass"
                build_tiktok_ass(cut_plan.word_timings, ass_path, self.config)
                self._log(f"{prefix}[CortadorViral] Legendas sincronizadas com {len(cut_plan.word_timings)} palavras originais.")
                self._progress(5, f"{prefix}5/5 Render FFmpeg", 0.72)

                def _on_cut_pct(ratio: float) -> None:
                    self._progress(5, f"{prefix}5/5 Render FFmpeg ({int(ratio * 100)}%)", 0.72 + 0.28 * ratio)

                mp4_path, txt_path = render_original_cut_video(
                    clip_path=cut_plan.clip_path,
                    ass_path=ass_path,
                    clip_duration=cut_plan.clip_duration,
                    gancho_tela=cut_plan.gancho_tela,
                    article_title=article.title,
                    article_source=article.source,
                    article_url=article.url,
                    descricao_tiktok=cut_plan.descricao_tiktok,
                    hashtags=cut_plan.hashtags,
                    transcript_excerpt=cut_plan.transcript_excerpt,
                    work_dir=job_dir,
                    config=self.config,
                    log_cb=self._log,
                    progress_cb=_on_cut_pct,
                    stop_event=self.stop_event,
                )

                if mp4_path.exists() and mp4_path.stat().st_size > 10_000:
                    append_history(
                        url=article.url,
                        title=article.title,
                        source=article.source,
                        output_file=mp4_path.name,
                        history_path=history_path,
                    )
                self._progress(5, f"{prefix}Concluído!", 1.0)
                return BatchItemResult(
                    index=batch_index,
                    success=True,
                    article=article,
                    script=script,
                    mp4_path=mp4_path,
                    txt_path=txt_path,
                )

            # ==================== MODO ADAPTADOR YOUTUBE (TIMESTAMPS EXATOS) ====================
            if modo == "adaptador_yt":
                self._check_cancelled()
                self._progress(1, f"{prefix}1/5 Transcrição YouTube", 0.08)
                self._log(f"{prefix}Modo Adaptador YouTube (Sincronia Exata) [{niche_name}]...")
                adapted = run_youtube_adapter_stage(
                    config=self.config,
                    work_dir=job_dir,
                    exclude_urls=exclude_urls,
                    custom_url=custom_video_url,
                    log_cb=self._log,
                )
                article = adapted.article
                script = adapted.script
                if self.on_preview:
                    self.on_preview(article, script)
                self._progress(3, f"{prefix}3/5 Voz/Legenda", 0.50)

                tts_result = generate_narration_and_subtitles(
                    script_text=script.roteiro,
                    work_dir=job_dir,
                    config=self.config,
                    log_cb=self._log,
                )
                self._progress(5, f"{prefix}5/5 Render FFmpeg", 0.74)

                def _on_adp_pct(ratio: float) -> None:
                    self._progress(5, f"{prefix}5/5 Render FFmpeg ({int(ratio * 100)}%)", 0.74 + 0.26 * ratio)

                mp4_path, txt_path = render_video(
                    image_paths=[],
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
                    broll_videos=[adapted.source_video_path],
                    matched_scenes=adapted.matched_scenes,
                    placar_texto=placar_texto,
                    log_cb=self._log,
                    progress_cb=_on_adp_pct,
                    stop_event=self.stop_event,
                )
                if mp4_path.exists() and mp4_path.stat().st_size > 10_000:
                    append_history(
                        url=article.url,
                        title=article.title,
                        source=article.source,
                        output_file=mp4_path.name,
                        history_path=history_path,
                    )
                self._progress(5, f"{prefix}Concluído!", 1.0)
                return BatchItemResult(
                    index=batch_index,
                    success=True,
                    article=article,
                    script=script,
                    mp4_path=mp4_path,
                    txt_path=txt_path,
                )

            # ==================== MODOS NOTÍCIA VIRAL / TOP 5 ====================
            # Etapa 1/5: Pauta (Top 5 Viral ou Notícia Quente do nicho)
            self._check_cancelled()
            self._progress(1, f"{prefix}{self.STAGES[0]}", 0.05)
            if modo == "top5":
                self._log(f"{prefix}Etapa 1/5 [{niche_name}]: Selecionando tema Top 5 Viral...")
                article = pick_next_top5_topic(config=self.config, exclude_urls=exclude_urls)
                self._log(f"{prefix}[Top5] Tema: {article.title}")
            else:
                self._log(f"{prefix}Etapa 1/5 [{niche_name}]: Coletando notícia quente...")
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
            self._log(f"{prefix}Etapa 2/5: Gerando roteiro de alta retenção no Ollama...")
            script = generate_script(article=article, config=self.config, log_cb=self._log)
            if self.on_preview:
                self.on_preview(article, script)
            self._progress(2, f"{prefix}{self.STAGES[1]}", 0.40)

            # Etapa 3/5: Voz / Legenda primeiro (gera timeline exata das palavras para casar cenas!)
            self._check_cancelled()
            self._progress(3, f"{prefix}3/5 Voz/Legenda", 0.44)
            self._log(f"{prefix}Etapa 3/5: Gerando narração Edge-TTS e legendas sincronizadas .ass...")
            tts_result = generate_narration_and_subtitles(
                script_text=script.roteiro,
                work_dir=job_dir,
                config=self.config,
                log_cb=self._log,
            )
            self._progress(3, f"{prefix}3/5 Voz/Legenda", 0.56)

            # Etapa 4/5: Caça Contextual de Vídeos (Nível 1) + Sincronia Semântica IA Visão (Nível 2)
            self._check_cancelled()
            self._progress(4, f"{prefix}4/5 Vídeos & Sincronia IA", 0.60)
            video_cfg = self.config.get("video", {})
            usar_broll = bool(video_cfg.get("usar_broll", True))
            custom_video_url = str(self.config.get("video_url_opcional") or "").strip()
            placar_texto = str(self.config.get("placar_opcional") or "").strip()
            target_scenes = max(12, min(22, int(video_cfg.get("num_cenas_alvo", 18))))

            contextual_videos: list[Path] = []
            library_videos: list[Path] = []
            matched_scenes: list[tuple[Path, float, int, int]] = []
            broll_videos: list[Path] = []

            if usar_broll or custom_video_url:
                self._log(f"{prefix}Etapa 4/5: Caçando vídeos contextuais HD + sincronizando cenas com a narração...")
                contextual_videos, library_videos = ensure_broll_videos(
                    config=self.config,
                    article_title=article.title,
                    work_dir=job_dir,
                    custom_video_url=custom_video_url,
                    embedded_videos=article.embedded_videos,
                    contextual_queries=script.buscas_video_youtube,
                    log_cb=self._log,
                )
                broll_videos = contextual_videos + library_videos

                if broll_videos:
                    est_scene_dur = max(2.2, (tts_result.duration + 0.35) / target_scenes)
                    indexed = index_video_pool(
                        contextual_videos=contextual_videos,
                        library_videos=library_videos,
                        scene_dur=est_scene_dur,
                        work_dir=job_dir,
                        config=self.config,
                        log_cb=self._log,
                    )
                    if indexed:
                        matched_scenes = match_scenes_to_narration(
                            word_timings=tts_result.word_timings,
                            total_duration=tts_result.duration,
                            num_scenes=target_scenes,
                            indexed_scenes=indexed,
                            config=self.config,
                            log_cb=self._log,
                        )

            image_paths: list[Path] = []
            if not broll_videos:
                self._log(f"{prefix}Etapa 4/5: Sem clipe de vídeo — buscando fotos HD de fallback...")
                image_paths = fetch_and_prepare_images(
                    search_terms=script.termos_busca_imagens,
                    output_dir=job_dir,
                    config=self.config,
                    og_image=article.og_image,
                    article_images=article.article_images,
                    log_cb=self._log,
                )
            self._progress(4, f"{prefix}4/5 Vídeos & Sincronia IA", 0.78)

            # Etapa 5/5: Render FFmpeg
            self._check_cancelled()
            self._progress(5, f"{prefix}{self.STAGES[4]}", 0.80)
            self._log(f"{prefix}Etapa 5/5: Renderizando vídeo final via FFmpeg...")

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
                broll_videos=broll_videos,
                matched_scenes=matched_scenes,
                placar_texto=placar_texto,
                log_cb=self._log,
                progress_cb=_on_ffmpeg_pct,
                stop_event=self.stop_event,
            )

            if mp4_path.exists() and mp4_path.stat().st_size > 10_000:
                append_history(
                    url=article.url,
                    title=article.title,
                    source=article.source,
                    output_file=mp4_path.name,
                    history_path=history_path,
                )
                self._log(f"{prefix}Histórico do nicho atualizado em {history_path.relative_to(history_path.parent.parent.parent)}.")

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
        """Executa geração em lote de 1 a 5 vídeos no nicho selecionado."""
        total = max(1, min(5, int(count)))
        results: list[BatchItemResult] = []
        used_urls: set[str] = set()

        self._log(
            f"=== Fábrica TK | Lote de {total} vídeo(s) | "
            f"Nicho: {self.config.get('nicho_nome')} | "
            f"Modelo: {self.config.get('ollama_modelo')} ==="
        )

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
