"""FábricaMX - Interface Desktop Minimalista (CustomTkinter)."""

from __future__ import annotations

import os
import subprocess
import sys
import threading
from datetime import datetime
from typing import Any

import customtkinter as ctk

from src import OUTPUT_DIR, load_config, save_config
from src.pipeline import FabricaPipeline
from src.scraper import NewsArticle
from src.scriptwriter import ScriptPackage, list_ollama_models

# Paleta Dark Minimalista (Obsidiana + Ciano + Amarelo)
BG_APP = "#0B0F17"
BG_PANEL = "#121826"
BG_CARD = "#171F30"
BG_INPUT = "#0F1522"
BORDER_SUBTLE = "#253149"
TEXT_PRIMARY = "#F1F5F9"
TEXT_SECONDARY = "#94A3B8"
ACCENT_CYAN = "#22D3EE"
ACCENT_CYAN_DARK = "#0891B2"
ACCENT_YELLOW = "#FACC15"
ACCENT_YELLOW_HOVER = "#EAB308"
ACCENT_DANGER = "#EF4444"
ACCENT_DANGER_HOVER = "#DC2626"
ACCENT_GREEN = "#22C55E"

VOICES = [
    "es-MX-JorgeNeural",
    "es-MX-DaliaNeural",
]

CLUB_OPTIONS = [
    "Club América",
    "Chivas",
    "Cruz Azul",
    "Tigres",
    "Liga MX Geral",
]

PIPELINE_STEPS = [
    "1/5 Notícia",
    "2/5 Roteiro",
    "3/5 Imagens",
    "4/5 Voz/Ass",
    "5/5 FFmpeg",
]


class FabricaMXApp(ctk.CTk):
    """Aplicação Desktop FábricaMX para geração automatizada de TikToks de Liga MX."""

    def __init__(self) -> None:
        super().__init__()

        ctk.set_appearance_mode("dark")
        ctk.set_default_color_theme("dark-blue")

        self.title("FábricaMX — Automação TikTok Liga MX")
        self.geometry("1100x680")
        self.minsize(1040, 650)
        self.configure(fg_color=BG_APP)

        self.config_data: dict[str, Any] = load_config()
        self.is_running = False
        self.stop_event = threading.Event()
        self.worker_thread: threading.Thread | None = None

        self._build_layout()
        self._load_values_from_config()
        self._append_log("FábricaMX pronto. Configure o clube/modelo e clique em GERAR VÍDEOS.")

        # Busca modelos locais do Ollama em segundo plano ao abrir
        self.after(300, self._refresh_ollama_models_async)

    def _build_layout(self) -> None:
        self.grid_columnconfigure(0, weight=0, minsize=390)
        self.grid_columnconfigure(1, weight=1)
        self.grid_rowconfigure(0, weight=1)

        # ==================== PAINEL ESQUERDO (CONTROLES) ====================
        self.left_panel = ctk.CTkFrame(
            self,
            fg_color=BG_PANEL,
            corner_radius=14,
            border_width=1,
            border_color=BORDER_SUBTLE,
        )
        self.left_panel.grid(row=0, column=0, sticky="nsew", padx=(14, 7), pady=14)
        self.left_panel.grid_columnconfigure(0, weight=1)
        self.left_panel.grid_rowconfigure(1, weight=1)

        # Cabeçalho / Marca
        header = ctk.CTkFrame(self.left_panel, fg_color="transparent")
        header.grid(row=0, column=0, sticky="ew", padx=16, pady=(14, 8))
        header.grid_columnconfigure(0, weight=1)

        brand_row = ctk.CTkFrame(header, fg_color="transparent")
        brand_row.grid(row=0, column=0, sticky="w")

        ctk.CTkLabel(
            brand_row,
            text="FÁBRICA",
            font=ctk.CTkFont(family="Segoe UI", size=22, weight="bold"),
            text_color=TEXT_PRIMARY,
        ).pack(side="left")
        ctk.CTkLabel(
            brand_row,
            text="MX",
            font=ctk.CTkFont(family="Segoe UI", size=22, weight="bold"),
            text_color=ACCENT_YELLOW,
        ).pack(side="left", padx=(2, 8))
        ctk.CTkLabel(
            brand_row,
            text="LIGA MX • 9:16",
            font=ctk.CTkFont(family="Consolas", size=11, weight="bold"),
            text_color=ACCENT_CYAN,
            fg_color=BG_CARD,
            corner_radius=6,
            padx=8,
            pady=2,
        ).pack(side="left")

        # Área rolável de controles para manter tudo confortável em 1100x680
        controls = ctk.CTkScrollableFrame(
            self.left_panel,
            fg_color="transparent",
            scrollbar_button_color=BORDER_SUBTLE,
            scrollbar_button_hover_color=ACCENT_CYAN_DARK,
        )
        controls.grid(row=1, column=0, sticky="nsew", padx=10, pady=2)
        controls.grid_columnconfigure(0, weight=1)

        # 1. Clube / Nicho
        self._section_label(controls, "CLUBE / NICHO", row=0)
        self.club_var = ctk.StringVar(value="Club América")
        self.club_menu = ctk.CTkOptionMenu(
            controls,
            values=CLUB_OPTIONS,
            variable=self.club_var,
            command=self._on_club_changed,
            fg_color=BG_INPUT,
            button_color=ACCENT_CYAN_DARK,
            button_hover_color=ACCENT_CYAN,
            dropdown_fg_color=BG_CARD,
            dropdown_hover_color=BORDER_SUBTLE,
            text_color=TEXT_PRIMARY,
            height=34,
        )
        self.club_menu.grid(row=1, column=0, sticky="ew", padx=6, pady=(2, 8))

        # Palavras-chave
        self._section_label(controls, "PALAVRAS-CHAVE (SEPARADAS POR VÍRGULA)", row=2)
        self.keywords_entry = ctk.CTkEntry(
            controls,
            placeholder_text="Club América, Águilas, Coapa, Liga MX",
            fg_color=BG_INPUT,
            border_color=BORDER_SUBTLE,
            text_color=TEXT_PRIMARY,
            height=34,
        )
        self.keywords_entry.grid(row=3, column=0, sticky="ew", padx=6, pady=(2, 10))

        # 2. Voz Edge-TTS
        self._section_label(controls, "VOZ DE NARRAÇÃO (EDGE-TTS)", row=4)
        self.voice_var = ctk.StringVar(value=VOICES[0])
        self.voice_menu = ctk.CTkOptionMenu(
            controls,
            values=VOICES,
            variable=self.voice_var,
            fg_color=BG_INPUT,
            button_color=ACCENT_CYAN_DARK,
            button_hover_color=ACCENT_CYAN,
            dropdown_fg_color=BG_CARD,
            dropdown_hover_color=BORDER_SUBTLE,
            text_color=TEXT_PRIMARY,
            height=34,
        )
        self.voice_menu.grid(row=5, column=0, sticky="ew", padx=6, pady=(2, 10))

        # 3. Modelo Ollama + botão Listar Modelos
        self._section_label(controls, "MODELO OLLAMA LOCAL", row=6)
        ollama_row = ctk.CTkFrame(controls, fg_color="transparent")
        ollama_row.grid(row=7, column=0, sticky="ew", padx=6, pady=(2, 10))
        ollama_row.grid_columnconfigure(0, weight=1)

        self.model_var = ctk.StringVar(value="llama3.1:latest")
        self.model_combo = ctk.CTkComboBox(
            ollama_row,
            values=["llama3.1:latest"],
            variable=self.model_var,
            fg_color=BG_INPUT,
            border_color=BORDER_SUBTLE,
            button_color=ACCENT_CYAN_DARK,
            button_hover_color=ACCENT_CYAN,
            dropdown_fg_color=BG_CARD,
            text_color=TEXT_PRIMARY,
            height=34,
        )
        self.model_combo.grid(row=0, column=0, sticky="ew", padx=(0, 6))

        self.refresh_models_btn = ctk.CTkButton(
            ollama_row,
            text="Listar",
            width=72,
            height=34,
            fg_color=BG_CARD,
            hover_color=BORDER_SUBTLE,
            border_width=1,
            border_color=ACCENT_CYAN_DARK,
            text_color=ACCENT_CYAN,
            font=ctk.CTkFont(size=12, weight="bold"),
            command=self._refresh_ollama_models_async,
        )
        self.refresh_models_btn.grid(row=0, column=1)

        # 4. Quantidade de vídeos no lote (1 a 5)
        batch_header = ctk.CTkFrame(controls, fg_color="transparent")
        batch_header.grid(row=8, column=0, sticky="ew", padx=6, pady=(4, 2))
        ctk.CTkLabel(
            batch_header,
            text="VÍDEOS NO LOTE",
            font=ctk.CTkFont(size=11, weight="bold"),
            text_color=TEXT_SECONDARY,
        ).pack(side="left")
        self.batch_value_label = ctk.CTkLabel(
            batch_header,
            text="1 vídeo",
            font=ctk.CTkFont(size=12, weight="bold"),
            text_color=ACCENT_YELLOW,
        )
        self.batch_value_label.pack(side="right")

        self.batch_seg = ctk.CTkSegmentedButton(
            controls,
            values=["1", "2", "3", "4", "5"],
            command=self._on_batch_changed,
            fg_color=BG_INPUT,
            selected_color=ACCENT_CYAN_DARK,
            selected_hover_color=ACCENT_CYAN,
            unselected_color=BG_CARD,
            unselected_hover_color=BORDER_SUBTLE,
            text_color=TEXT_PRIMARY,
            height=32,
        )
        self.batch_seg.set("1")
        self.batch_seg.grid(row=9, column=0, sticky="ew", padx=6, pady=(2, 12))

        # 5. Switches
        switch_card = ctk.CTkFrame(
            controls,
            fg_color=BG_CARD,
            corner_radius=10,
            border_width=1,
            border_color=BORDER_SUBTLE,
        )
        switch_card.grid(row=10, column=0, sticky="ew", padx=6, pady=(2, 6))
        switch_card.grid_columnconfigure(0, weight=1)

        self.music_var = ctk.BooleanVar(value=True)
        self.music_switch = ctk.CTkSwitch(
            switch_card,
            text="Usar música de fundo (assets/music)",
            variable=self.music_var,
            progress_color=ACCENT_CYAN_DARK,
            button_color=ACCENT_CYAN,
            text_color=TEXT_PRIMARY,
            font=ctk.CTkFont(size=12),
        )
        self.music_switch.grid(row=0, column=0, sticky="w", padx=12, pady=(10, 6))

        self.intro_var = ctk.BooleanVar(value=True)
        self.intro_switch = ctk.CTkSwitch(
            switch_card,
            text="Usar intro do Flow (assets/intros)",
            variable=self.intro_var,
            progress_color=ACCENT_CYAN_DARK,
            button_color=ACCENT_CYAN,
            text_color=TEXT_PRIMARY,
            font=ctk.CTkFont(size=12),
        )
        self.intro_switch.grid(row=1, column=0, sticky="w", padx=12, pady=6)

        self.clean_temp_var = ctk.BooleanVar(value=True)
        self.clean_temp_switch = ctk.CTkSwitch(
            switch_card,
            text="Limpar pasta temp ao finalizar",
            variable=self.clean_temp_var,
            progress_color=ACCENT_CYAN_DARK,
            button_color=ACCENT_CYAN,
            text_color=TEXT_PRIMARY,
            font=ctk.CTkFont(size=12),
        )
        self.clean_temp_switch.grid(row=2, column=0, sticky="w", padx=12, pady=(6, 10))

        # Botões de ação no rodapé do painel esquerdo
        actions = ctk.CTkFrame(self.left_panel, fg_color="transparent")
        actions.grid(row=2, column=0, sticky="ew", padx=16, pady=(8, 14))
        actions.grid_columnconfigure(0, weight=1)

        self.generate_btn = ctk.CTkButton(
            actions,
            text="GERAR VÍDEOS",
            height=48,
            corner_radius=10,
            fg_color=ACCENT_YELLOW,
            hover_color=ACCENT_YELLOW_HOVER,
            text_color="#090D16",
            font=ctk.CTkFont(family="Segoe UI", size=15, weight="bold"),
            command=self._on_generate_clicked,
        )
        self.generate_btn.grid(row=0, column=0, sticky="ew", pady=(0, 8))

        self.open_output_btn = ctk.CTkButton(
            actions,
            text="Abrir Pasta Output",
            height=36,
            corner_radius=8,
            fg_color=BG_CARD,
            hover_color=BORDER_SUBTLE,
            border_width=1,
            border_color=ACCENT_CYAN_DARK,
            text_color=ACCENT_CYAN,
            font=ctk.CTkFont(size=13, weight="bold"),
            command=self._open_output_folder,
        )
        self.open_output_btn.grid(row=1, column=0, sticky="ew")

        # ==================== PAINEL DIREITO (MONITORAMENTO) ====================
        self.right_panel = ctk.CTkFrame(
            self,
            fg_color=BG_PANEL,
            corner_radius=14,
            border_width=1,
            border_color=BORDER_SUBTLE,
        )
        self.right_panel.grid(row=0, column=1, sticky="nsew", padx=(7, 14), pady=14)
        self.right_panel.grid_columnconfigure(0, weight=1)
        self.right_panel.grid_rowconfigure(1, weight=3)  # Prévia
        self.right_panel.grid_rowconfigure(2, weight=4)  # Console

        # 1. Card de Progresso + Etapas
        progress_card = ctk.CTkFrame(
            self.right_panel,
            fg_color=BG_CARD,
            corner_radius=10,
            border_width=1,
            border_color=BORDER_SUBTLE,
        )
        progress_card.grid(row=0, column=0, sticky="ew", padx=14, pady=(14, 8))
        progress_card.grid_columnconfigure(0, weight=1)

        status_row = ctk.CTkFrame(progress_card, fg_color="transparent")
        status_row.grid(row=0, column=0, sticky="ew", padx=14, pady=(10, 6))
        status_row.grid_columnconfigure(0, weight=1)

        self.status_label = ctk.CTkLabel(
            status_row,
            text="Aguardando início — pronto para produzir",
            font=ctk.CTkFont(size=13, weight="bold"),
            text_color=TEXT_PRIMARY,
            anchor="w",
        )
        self.status_label.grid(row=0, column=0, sticky="w")

        self.percent_label = ctk.CTkLabel(
            status_row,
            text="0%",
            font=ctk.CTkFont(family="Consolas", size=13, weight="bold"),
            text_color=ACCENT_CYAN,
        )
        self.percent_label.grid(row=0, column=1, sticky="e")

        self.progress_bar = ctk.CTkProgressBar(
            progress_card,
            height=10,
            corner_radius=5,
            fg_color=BG_INPUT,
            progress_color=ACCENT_CYAN,
        )
        self.progress_bar.set(0.0)
        self.progress_bar.grid(row=1, column=0, sticky="ew", padx=14, pady=(0, 10))

        # Indicador visual das 5 etapas
        steps_row = ctk.CTkFrame(progress_card, fg_color="transparent")
        steps_row.grid(row=2, column=0, sticky="ew", padx=10, pady=(0, 10))
        self.step_badges: list[ctk.CTkLabel] = []
        for idx, label_txt in enumerate(PIPELINE_STEPS):
            steps_row.grid_columnconfigure(idx, weight=1)
            badge = ctk.CTkLabel(
                steps_row,
                text=label_txt,
                font=ctk.CTkFont(family="Consolas", size=11, weight="bold"),
                text_color=TEXT_SECONDARY,
                fg_color=BG_INPUT,
                corner_radius=6,
                height=24,
            )
            badge.grid(row=0, column=idx, sticky="ew", padx=3)
            self.step_badges.append(badge)

        # 2. Card de Prévia (Pauta Atual + Roteiro Gerado)
        preview_card = ctk.CTkFrame(
            self.right_panel,
            fg_color=BG_CARD,
            corner_radius=10,
            border_width=1,
            border_color=BORDER_SUBTLE,
        )
        preview_card.grid(row=1, column=0, sticky="nsew", padx=14, pady=6)
        preview_card.grid_columnconfigure(0, weight=1)
        preview_card.grid_rowconfigure(2, weight=1)

        preview_hdr = ctk.CTkFrame(preview_card, fg_color="transparent")
        preview_hdr.grid(row=0, column=0, sticky="ew", padx=12, pady=(8, 2))
        ctk.CTkLabel(
            preview_hdr,
            text="PRÉVIA DA PAUTA & ROTEIRO",
            font=ctk.CTkFont(size=11, weight="bold"),
            text_color=ACCENT_YELLOW,
        ).pack(side="left")

        self.preview_meta_label = ctk.CTkLabel(
            preview_hdr,
            text="Nenhuma pauta carregada",
            font=ctk.CTkFont(size=11),
            text_color=TEXT_SECONDARY,
        )
        self.preview_meta_label.pack(side="right")

        self.hook_preview_label = ctk.CTkLabel(
            preview_card,
            text="Gancho em tela: —",
            font=ctk.CTkFont(size=12, weight="bold"),
            text_color=ACCENT_CYAN,
            anchor="w",
        )
        self.hook_preview_label.grid(row=1, column=0, sticky="ew", padx=12, pady=(0, 4))

        self.preview_box = ctk.CTkTextbox(
            preview_card,
            fg_color=BG_INPUT,
            text_color=TEXT_PRIMARY,
            border_width=1,
            border_color=BORDER_SUBTLE,
            font=ctk.CTkFont(family="Segoe UI", size=12),
            wrap="word",
        )
        self.preview_box.grid(row=2, column=0, sticky="nsew", padx=12, pady=(0, 10))
        self.preview_box.insert("1.0", "A pauta coletada e o roteiro em espanhol aparecerão aqui em tempo real.")
        self.preview_box.configure(state="disabled")

        # 3. Console de Logs (Estilo Terminal)
        log_card = ctk.CTkFrame(
            self.right_panel,
            fg_color=BG_CARD,
            corner_radius=10,
            border_width=1,
            border_color=BORDER_SUBTLE,
        )
        log_card.grid(row=2, column=0, sticky="nsew", padx=14, pady=(6, 14))
        log_card.grid_columnconfigure(0, weight=1)
        log_card.grid_rowconfigure(1, weight=1)

        log_hdr = ctk.CTkFrame(log_card, fg_color="transparent")
        log_hdr.grid(row=0, column=0, sticky="ew", padx=12, pady=(8, 4))
        ctk.CTkLabel(
            log_hdr,
            text="CONSOLE DE LOGS EM TEMPO REAL",
            font=ctk.CTkFont(size=11, weight="bold"),
            text_color=ACCENT_CYAN,
        ).pack(side="left")

        ctk.CTkButton(
            log_hdr,
            text="Limpar",
            width=60,
            height=22,
            fg_color=BG_INPUT,
            hover_color=BORDER_SUBTLE,
            text_color=TEXT_SECONDARY,
            font=ctk.CTkFont(size=11),
            command=self._clear_log,
        ).pack(side="right")

        self.log_box = ctk.CTkTextbox(
            log_card,
            fg_color="#070B12",
            text_color="#CBD5E1",
            border_width=1,
            border_color=BORDER_SUBTLE,
            font=ctk.CTkFont(family="Consolas", size=12),
            wrap="word",
        )
        self.log_box.grid(row=1, column=0, sticky="nsew", padx=12, pady=(0, 10))
        self.log_box.configure(state="disabled")

    def _section_label(self, parent: Any, text: str, row: int) -> None:
        ctk.CTkLabel(
            parent,
            text=text,
            font=ctk.CTkFont(size=11, weight="bold"),
            text_color=TEXT_SECONDARY,
            anchor="w",
        ).grid(row=row, column=0, sticky="w", padx=6, pady=(4, 0))

    def _load_values_from_config(self) -> None:
        club = self.config_data.get("clube_foco", "Club América")
        if club in CLUB_OPTIONS:
            self.club_var.set(club)
        keywords = self.config_data.get("palavras_chave", [])
        self.keywords_entry.delete(0, "end")
        self.keywords_entry.insert(0, ", ".join(keywords))

        voice = self.config_data.get("tts_voz", VOICES[0])
        if voice in VOICES:
            self.voice_var.set(voice)

        model = self.config_data.get("ollama_modelo", "llama3.1:latest")
        self.model_var.set(model)
        self.model_combo.configure(values=[model])

        video_cfg = self.config_data.get("video", {})
        self.music_var.set(bool(video_cfg.get("usar_musica", True)))
        self.intro_var.set(bool(video_cfg.get("usar_intro", True)))
        self.clean_temp_var.set(bool(video_cfg.get("limpar_temp", True)))

    def _collect_config_from_ui(self) -> dict[str, Any]:
        cfg = dict(self.config_data)
        cfg["clube_foco"] = self.club_var.get().strip() or "Club América"
        raw_kw = self.keywords_entry.get()
        cfg["palavras_chave"] = [k.strip() for k in raw_kw.split(",") if k.strip()]
        cfg["tts_voz"] = self.voice_var.get().strip() or VOICES[0]
        cfg["ollama_modelo"] = self.model_var.get().strip() or "llama3.1:latest"

        video_cfg = dict(cfg.get("video", {}))
        video_cfg["usar_musica"] = bool(self.music_var.get())
        video_cfg["usar_intro"] = bool(self.intro_var.get())
        video_cfg["limpar_temp"] = bool(self.clean_temp_var.get())
        cfg["video"] = video_cfg

        self.config_data = cfg
        try:
            save_config(cfg)
        except Exception as exc:
            self._append_log(f"Aviso: não foi possível salvar config.json ({exc}).")
        return cfg

    def _on_club_changed(self, selected_club: str) -> None:
        presets = self.config_data.get("presets_clubes", {})
        preset_kw = presets.get(selected_club)
        if preset_kw:
            self.keywords_entry.delete(0, "end")
            self.keywords_entry.insert(0, ", ".join(preset_kw))

    def _on_batch_changed(self, val: str) -> None:
        n = int(val)
        self.batch_value_label.configure(text=f"{n} vídeo" if n == 1 else f"{n} vídeos")

    def _refresh_ollama_models_async(self) -> None:
        self.refresh_models_btn.configure(state="disabled", text="...")

        def _task() -> None:
            ollama_url = self.config_data.get("ollama_url", "http://localhost:11434")
            try:
                models = list_ollama_models(ollama_url)
                self.after(0, lambda: self._on_models_loaded(models, None))
            except Exception as exc:
                self.after(0, lambda err=str(exc): self._on_models_loaded([], err))

        threading.Thread(target=_task, daemon=True).start()

    def _on_models_loaded(self, models: list[str], error: str | None) -> None:
        self.refresh_models_btn.configure(state="normal", text="Listar")
        if error:
            self._append_log(f"[Ollama] Não foi possível listar modelos ({error}). Verifique se 'ollama serve' está ativo.")
            return
        if not models:
            self._append_log("[Ollama] Conectado, mas nenhum modelo instalado. Execute ex.: 'ollama pull llama3.1:8b'.")
            return
        self.model_combo.configure(values=models)
        current = self.model_var.get().strip()
        if current not in models:
            self.model_var.set(models[0])
        self._append_log(f"[Ollama] {len(models)} modelo(s) disponível(is): {', '.join(models)}")

    def _append_log(self, message: str) -> None:
        ts = datetime.now().strftime("%H:%M:%S")
        line = f"[{ts}] {message}\n"
        self.log_box.configure(state="normal")
        self.log_box.insert("end", line)
        self.log_box.see("end")
        self.log_box.configure(state="disabled")

    def _clear_log(self) -> None:
        self.log_box.configure(state="normal")
        self.log_box.delete("1.0", "end")
        self.log_box.configure(state="disabled")

    def _update_progress_ui(self, step: int, total_steps: int, stage_label: str, ratio: float) -> None:
        self.status_label.configure(text=stage_label)
        self.progress_bar.set(ratio)
        self.percent_label.configure(text=f"{int(round(ratio * 100))}%")

        for idx, badge in enumerate(self.step_badges, start=1):
            if idx < step or (ratio >= 0.999 and idx <= step):
                badge.configure(fg_color="#14532D", text_color=ACCENT_GREEN)
            elif idx == step:
                badge.configure(fg_color=ACCENT_CYAN_DARK, text_color="#071018")
            else:
                badge.configure(fg_color=BG_INPUT, text_color=TEXT_SECONDARY)

    def _update_preview_ui(self, article: NewsArticle | None, script: ScriptPackage | None) -> None:
        if article is None:
            return
        self.preview_meta_label.configure(text=f"Fonte: {article.source}")
        lines = [f"PAUTA: {article.title}", f"URL: {article.url}", ""]
        if script is not None:
            self.hook_preview_label.configure(
                text=f"Gancho em tela (3s): {script.gancho_tela}  |  {script.word_count} palavras"
            )
            lines.append("ROTEIRO GERADO:")
            lines.append(script.roteiro)
            lines.append("")
            lines.append(f"DESCRIÇÃO TIKTOK: {script.descricao_tiktok}")
            lines.append(f"HASHTAGS: {' '.join(script.hashtags)}")
        else:
            self.hook_preview_label.configure(text="Gancho em tela: gerando roteiro no Ollama...")
            snippet = (article.full_text or article.summary or "")[:450]
            lines.append("CONTEXTO DA MATÉRIA:")
            lines.append(snippet + ("..." if len(article.full_text) > 450 else ""))

        self.preview_box.configure(state="normal")
        self.preview_box.delete("1.0", "end")
        self.preview_box.insert("1.0", "\n".join(lines))
        self.preview_box.configure(state="disabled")

    def _set_controls_state(self, running: bool) -> None:
        self.is_running = running
        state = "disabled" if running else "normal"
        for widget in (
            self.club_menu,
            self.keywords_entry,
            self.voice_menu,
            self.model_combo,
            self.refresh_models_btn,
            self.batch_seg,
            self.music_switch,
            self.intro_switch,
            self.clean_temp_switch,
        ):
            widget.configure(state=state)

        if running:
            self.generate_btn.configure(
                text="CANCELAR GERAÇÃO",
                fg_color=ACCENT_DANGER,
                hover_color=ACCENT_DANGER_HOVER,
                text_color="#FFFFFF",
            )
        else:
            self.generate_btn.configure(
                text="GERAR VÍDEOS",
                fg_color=ACCENT_YELLOW,
                hover_color=ACCENT_YELLOW_HOVER,
                text_color="#090D16",
            )

    def _on_generate_clicked(self) -> None:
        if self.is_running:
            self.stop_event.set()
            self._append_log("Solicitação de cancelamento enviada. Aguardando encerramento seguro...")
            self.generate_btn.configure(state="disabled", text="CANCELANDO...")
            return

        cfg = self._collect_config_from_ui()
        batch_count = int(self.batch_seg.get())
        self.stop_event.clear()
        self._set_controls_state(True)
        self._update_progress_ui(1, 5, "Iniciando pipeline...", 0.02)

        def _worker() -> None:
            pipeline = FabricaPipeline(
                config=cfg,
                on_log=lambda m: self.after(0, lambda msg=m: self._append_log(msg)),
                on_progress=lambda st, tot, lbl, r: self.after(
                    0, lambda s=st, t=tot, l=lbl, p=r: self._update_progress_ui(s, t, l, p)
                ),
                on_preview=lambda art, scr: self.after(
                    0, lambda a=art, s=scr: self._update_preview_ui(a, s)
                ),
                stop_event=self.stop_event,
            )
            results = pipeline.run_batch(count=batch_count)
            self.after(0, lambda res=results: self._on_batch_finished(res))

        self.worker_thread = threading.Thread(target=_worker, daemon=True)
        self.worker_thread.start()

    def _on_batch_finished(self, results: list[Any]) -> None:
        self._set_controls_state(False)
        self.generate_btn.configure(state="normal")
        ok = sum(1 for r in results if r.success)
        total = len(results)
        if self.stop_event.is_set():
            self.status_label.configure(text=f"Interrompido ({ok}/{total} concluídos)")
        elif ok == total and total > 0:
            self.status_label.configure(text=f"Finalizado com sucesso! {ok}/{total} vídeo(s) em output/")
        else:
            self.status_label.configure(text=f"Finalizado com alertas: {ok}/{total} vídeo(s) gerados")

    def _open_output_folder(self) -> None:
        OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
        try:
            if sys.platform.startswith("win"):
                os.startfile(str(OUTPUT_DIR))  # type: ignore[attr-defined]
            elif sys.platform == "darwin":
                subprocess.Popen(["open", str(OUTPUT_DIR)])
            else:
                subprocess.Popen(["xdg-open", str(OUTPUT_DIR)])
        except Exception as exc:
            self._append_log(f"Não foi possível abrir a pasta output: {exc}")


def main() -> None:
    app = FabricaMXApp()
    app.mainloop()


if __name__ == "__main__":
    main()
