"""Orquestrador multi-nicho da pipeline Fábrica TK com callbacks de progresso e histórico isolado."""

from __future__ import annotations

import shutil
import threading
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

from src import HISTORY_PATH, TEMP_DIR, append_history
from src.highlights_engine import run_highlights_pro_pipeline
from src.meme_frame_engine import run_moldura_viral_pipeline
from src.story_engine import run_long_story_pipeline
from src.media_fetcher import fetch_and_prepare_images, fetch_images_for_scenes
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

            # ==================== MODO HISTÓRIA LONGA YOUTUBE 16:9 (+ THUMB + SHORT 9:16) ====================
            if modo == "historia_longa":
                self._check_cancelled()
                self._progress(1, f"{prefix}1/5 História Longa (Bíblia + Capítulos)", 0.08)
                self._log(f"{prefix}Modo História Longa YouTube 16:9 + Thumbnail High-CTR + Teaser 9:16 [{niche_name}]...")

                def _on_st_pct(ratio: float) -> None:
                    self._progress(5, f"{prefix}5/5 Render 16:9 ({int(ratio * 100)}%)", 0.55 + 0.45 * ratio)

                st_res = run_long_story_pipeline(
                    config=self.config,
                    work_dir=job_dir,
                    exclude_urls=exclude_urls,
                    log_cb=self._log,
                    progress_cb=_on_st_pct,
                    stop_event=self.stop_event,
                )
                article = st_res.article
                script = st_res.script
                if self.on_preview:
                    self.on_preview(article, script)
                if st_res.mp4_path.exists() and st_res.mp4_path.stat().st_size > 10_000:
                    append_history(
                        url=article.url,
                        title=article.title,
                        source=article.source,
                        output_file=st_res.mp4_path.name,
                        history_path=history_path,
                    )
                self._progress(5, f"{prefix}Concluído!", 1.0)
                return BatchItemResult(
                    index=batch_index,
                    success=True,
                    article=article,
                    script=script,
                    mp4_path=st_res.mp4_path,
                    txt_path=st_res.txt_path,
                )

            # ==================== MODO MOLDURA PÁGINA VIRAL (LOGO + SELO ✓ + FRASE + VÍDEO) ====================
            if modo == "moldura_viral":
                self._check_cancelled()
                self._progress(1, f"{prefix}1/5 Moldura Viral (Clipe + IA)", 0.10)
                self._log(f"{prefix}Modo Moldura Página Viral (Logo + Selo ✓ + Frase no Topo) [{niche_name}]...")

                def _on_mv_pct(ratio: float) -> None:
                    self._progress(5, f"{prefix}5/5 Render Moldura ({int(ratio * 100)}%)", 0.45 + 0.55 * ratio)

                mp4_path, txt_path, article, script = run_moldura_viral_pipeline(
                    config=self.config,
                    work_dir=job_dir,
                    exclude_urls=exclude_urls,
                    custom_url=custom_video_url,
                    headline_override=placar_texto,
                    log_cb=self._log,
                    progress_cb=_on_mv_pct,
                    stop_event=self.stop_event,
                )
                if self.on_preview:
                    self.on_preview(article, script)
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

            # ==================== MODO HIGHLIGHTS FUTEBOL PRO (ÁUDIO ORIGINAL NOS GOLS!) ====================
            if modo == "highlights_pro":
                self._check_cancelled()
                self._progress(1, f"{prefix}1/5 Highlights Pro (Cronológico)", 0.08)
                self._log(f"{prefix}Modo Highlights Futebol Pro (Pontapé Inicial → Gols c/ Áudio Original → Apito Final)...")

                def _on_hl_pct(ratio: float) -> None:
                    self._progress(5, f"{prefix}5/5 Render Highlights Pro ({int(ratio * 100)}%)", 0.65 + 0.35 * ratio)

                mp4_path, txt_path, article, script = run_highlights_pro_pipeline(
                    config=self.config,
                    work_dir=job_dir,
                    exclude_urls=exclude_urls,
                    custom_url=custom_video_url,
                    placar_opcional=placar_texto,
                    log_cb=self._log,
                    progress_cb=_on_hl_pct,
                    stop_event=self.stop_event,
                )
                if self.on_preview:
                    self.on_preview(article, script)
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
                # Posição Y=1465 fica limpa logo abaixo do viewport 430..1330 e acima da UI do TikTok
                build_tiktok_ass(cut_plan.word_timings, ass_path, self.config, pos_y=1465)
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
                    pos_y=1445,
                )

                # Etapa 4/5: IA Visão olha os frames reais das janelas baixadas para mostrar EXATAMENTE
                # a ação narrada (ex.: leão esmagando o coco, crocodilo mordendo sensor) e descartar frames vazios!
                self._progress(4, f"{prefix}4/5 Sincronia IA Visão", 0.60)
                source_parts = adapted.source_video_paths or [adapted.source_video_path]
                final_matched = adapted.matched_scenes
                try:
                    target_adp_scenes = max(14, min(20, int(round(tts_result.duration / 3.4))))
                    est_slot_dur = max(2.4, (tts_result.duration + 0.35) / target_adp_scenes)
                    indexed_adp = index_video_pool(
                        contextual_videos=source_parts,
                        library_videos=[],
                        scene_dur=est_slot_dur,
                        work_dir=job_dir,
                        config=self.config,
                        log_cb=self._log,
                    )
                    if indexed_adp:
                        vision_matched = match_scenes_to_narration(
                            word_timings=tts_result.word_timings,
                            total_duration=tts_result.duration,
                            num_scenes=target_adp_scenes,
                            indexed_scenes=indexed_adp,
                            config=self.config,
                            log_cb=self._log,
                        )
                        if vision_matched:
                            final_matched = vision_matched
                            self._log(
                                f"{prefix}[AdaptadorYT] IA Visão selecionou {len(final_matched)} cenas de AÇÃO REAL sincronizadas com a fala!"
                            )
                except Exception as exc:
                    self._log(f"{prefix}[AdaptadorYT] Usando timestamps diretos ({exc}).")

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
                    broll_videos=source_parts,
                    matched_scenes=final_matched,
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
                pos_y=1445,
            )
            self._progress(3, f"{prefix}3/5 Voz/Legenda", 0.56)

            # Etapa 4/5: Caça Contextual de Vídeos (Nível 1) + Sincronia Semântica IA Visão (Nível 2)
            self._check_cancelled()
            self._progress(4, f"{prefix}4/5 Vídeos & Sincronia IA", 0.60)
            video_cfg = self.config.get("video", {})
            usar_broll = bool(video_cfg.get("usar_broll", True))
            preferir_fotos = bool(video_cfg.get("preferir_fotos", False))
            custom_video_url = str(self.config.get("video_url_opcional") or "").strip()
            placar_texto = str(self.config.get("placar_opcional") or "").strip()
            target_scenes = max(6, min(22, int(video_cfg.get("num_cenas_alvo", 18))))

            contextual_videos: list[Path] = []
            library_videos: list[Path] = []
            matched_scenes: list[tuple[Path, float, int, int]] = []
            timed_scenes: list[dict[str, Any]] = []
            broll_videos: list[Path] = []
            image_paths: list[Path] = []

            def _build_timed_hybrid_scenes(
                cenas_list: list[dict[str, str]],
                photo_groups: list[list[Path]],
                word_timings_list: list[Any],
                total_audio_dur: float,
                clip_pool: list[Path] | None = None,
            ) -> list[dict[str, Any]]:
                """
                Alinha cada cena ao segundo EXATO da fala, quebrando em batidas dinâmicas de ~2.8–3.4s
                e mesclando FOTOS HD dos envolvidos + pequenos segmentos de VÍDEO (~3s) para retenção máxima!
                """
                from src.video_fetcher import build_video_scene_slices

                counts = [max(1, len((c.get("fala") or "").split())) for c in cenas_list]
                total_script_w = max(1, sum(counts))
                n_wt = len(word_timings_list)

                vid_slices = (
                    build_video_scene_slices(clip_pool, num_scenes=max(6, len(cenas_list)), scene_dur=3.4)
                    if clip_pool
                    else []
                )
                vid_cursor = 0

                out_scenes: list[dict[str, Any]] = []
                cum_w = 0
                prev_t = 0.0
                for idx_c, (wc_scene, photos) in enumerate(zip(counts, photo_groups)):
                    cum_w += wc_scene
                    if idx_c == len(counts) - 1:
                        end_t = total_audio_dur + 0.35
                    elif n_wt > 0:
                        wt_idx = min(n_wt - 1, max(0, int(round((cum_w / total_script_w) * n_wt)) - 1))
                        end_t = max(prev_t + 1.8, float(word_timings_list[wt_idx].end))
                    else:
                        end_t = total_audio_dur * (cum_w / total_script_w)

                    span = max(1.8, end_t - prev_t)
                    # Quebra cenas longas em cortes dinâmicos de ~3.0s (nunca fica imagem parada por 8s!)
                    n_beats = max(1, min(4, int(round(span / 3.1))))
                    beat_dur = span / float(n_beats)

                    for b in range(n_beats):
                        # Mescla inteligente (~3s de vídeo + fotos exatas da cena):
                        # - Na cena 0 (GANCHO 0-3s): se houver clipe real, abre em movimento no 1º beat!
                        # - Nas demais cenas: intercala foto exata da pessoa -> clipe ~3s -> 2ª foto da pessoa!
                        want_video = False
                        if vid_slices and vid_cursor < len(vid_slices):
                            if idx_c == 0 and b == 0 and n_beats >= 2:
                                want_video = True
                            elif idx_c > 0 and b == 1:
                                want_video = True
                            elif idx_c > 0 and n_beats == 1 and (idx_c % 3 == 2):
                                want_video = True

                        if want_video:
                            vf, v_start, vw, vh = vid_slices[vid_cursor % len(vid_slices)]
                            vid_cursor += 1
                            out_scenes.append(
                                {"type": "video", "path": vf, "start": v_start, "w": vw, "h": vh, "dur": beat_dur}
                            )
                        else:
                            photo_p = photos[b % len(photos)]
                            out_scenes.append({"type": "image", "path": photo_p, "dur": beat_dur})
                    prev_t = end_t
                return out_scenes

            if preferir_fotos:
                if script.cenas:
                    self._log(f"{prefix}Etapa 4/5: Sincronia Fala ↔ Fotos + Clipes 3s ({len(script.cenas)} cenas)...")
                    photo_groups = fetch_images_for_scenes(
                        cenas=script.cenas,
                        output_dir=job_dir,
                        config=self.config,
                        og_image=article.og_image,
                        article_images=article.article_images,
                        log_cb=self._log,
                    )
                    if usar_broll or custom_video_url:
                        try:
                            contextual_videos, library_videos = ensure_broll_videos(
                                config=self.config,
                                article_title=article.title,
                                work_dir=job_dir,
                                custom_video_url=custom_video_url,
                                embedded_videos=article.embedded_videos,
                                contextual_queries=script.buscas_video_youtube[:2],
                                log_cb=self._log,
                            )
                            broll_videos = contextual_videos + library_videos
                        except Exception as exc:
                            self._log(f"{prefix}[B-Roll] Seguindo só com fotos HD ({exc}).")

                    timed_scenes = _build_timed_hybrid_scenes(
                        script.cenas,
                        photo_groups,
                        tts_result.word_timings,
                        tts_result.duration,
                        clip_pool=broll_videos,
                    )
                    image_paths = [p for g in photo_groups for p in g]
                    n_v = sum(1 for s in timed_scenes if s["type"] == "video")
                    n_i = sum(1 for s in timed_scenes if s["type"] == "image")
                    self._log(
                        f"{prefix}[Pipeline] Montagem dinâmica pronta: {n_i} cortes de foto + {n_v} segmentos de vídeo (~3s cada)!"
                    )
                else:
                    self._log(f"{prefix}Etapa 4/5: Modo Fotos + Clipes Rápidos (buscando fotos HD dos envolvidos)...")
                    image_paths = fetch_and_prepare_images(
                        search_terms=script.termos_busca_imagens,
                        output_dir=job_dir,
                        config=self.config,
                        og_image=article.og_image,
                        article_images=article.article_images,
                        log_cb=self._log,
                    )
                    if usar_broll or custom_video_url:
                        try:
                            contextual_videos, library_videos = ensure_broll_videos(
                                config=self.config,
                                article_title=article.title,
                                work_dir=job_dir,
                                custom_video_url=custom_video_url,
                                embedded_videos=article.embedded_videos,
                                contextual_queries=script.buscas_video_youtube[:2],
                                log_cb=self._log,
                            )
                            broll_videos = contextual_videos + library_videos
                        except Exception as exc:
                            self._log(f"{prefix}[B-Roll] Seguindo só com fotos HD ({exc}).")
            else:
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

                if not broll_videos:
                    if script.cenas:
                        self._log(f"{prefix}Etapa 4/5: Sem clipe — usando sincronia Fala ↔ Foto ({len(script.cenas)} cenas)...")
                        photo_groups = fetch_images_for_scenes(
                            cenas=script.cenas,
                            output_dir=job_dir,
                            config=self.config,
                            og_image=article.og_image,
                            article_images=article.article_images,
                            log_cb=self._log,
                        )
                        timed_scenes = _build_timed_hybrid_scenes(
                            script.cenas,
                            photo_groups,
                            tts_result.word_timings,
                            tts_result.duration,
                            clip_pool=None,
                        )
                        image_paths = [p for g in photo_groups for p in g]
                    else:
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
                timed_scenes=timed_scenes,
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
