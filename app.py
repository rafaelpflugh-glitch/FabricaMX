"""Fábrica TK — Interface Desktop Multi-Nicho (CustomTkinter)."""

from __future__ import annotations

import os
import subprocess
import sys
import threading
from datetime import datetime
from pathlib import Path
from typing import Any

import customtkinter as ctk

from src import (
    build_runtime_config,
    get_niche_paths,
    list_niches,
    load_config,
    load_niche_config,
    save_config,
    save_niche_config,
)
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

MODE_LABELS = {
    "top5": "Top 5 / Segredos Virais",
    "noticia": "Notícia / Rumor Viral",
}
LABEL_TO_MODE = {v: k for k, v in MODE_LABELS.items()}

PIPELINE_STEPS = [
    "1/5 Pauta",
    "2/5 Roteiro",
    "3/5 Cenas",
    "4/5 Voz/Ass",
    "5/5 FFmpeg",
]


class FabricaTKApp(ctk.CTk):
    """Aplicação Desktop Fábrica TK para produção automatizada multi-nicho 9:16."""

    def __init__(self) -> None:
        super().__init__()

        ctk.set_appearance_mode("dark")
        ctk.set_default_color_theme("dark-blue")

        self.title("Fábrica TK — Automação Multi-Nicho para TikTok")
        self.geometry("1120x700")
        self.minsize(1040, 660)
        self.configure(fg_color=BG_APP)

        self.global_cfg: dict[str, Any] = load_config()
        self.niches_list: list[dict[str, Any]] = list_niches()
        self.niche_name_to_id: dict[str, str] = {n["nome"]: n["id"] for n in self.niches_list}
        self.niche_id_to_name: dict[str, str] = {n["id"]: n["nome"] for n in self.niches_list}

        self.voice_label_to_meta: dict[str, dict[str, str]] = {}
        self.voice_id_to_label: dict[str, str] = {}

        self.active_niche_id: str = self.global_cfg.get("nicho_ativo", "gta_vi")
        if self.active_niche_id not in self.niche_id_to_name and self.niches_list:
            self.active_niche_id = self.niches_list[0]["id"]

        self.runtime_cfg: dict[str, Any] = build_runtime_config(self.global_cfg, self.active_niche_id)
        self.is_running = False
        self.stop_event = threading.Event()
        self.worker_thread: threading.Thread | None = None

        self._build_layout()
        self._apply_niche_to_ui(self.active_niche_id)
        self._append_log("Fábrica TK pronta! Escolha o nicho, o modelo Ollama e clique em GERAR VÍDEOS.")

        self.after(300, self._refresh_ollama_models_async)

    def _build_layout(self) -> None:
        self.grid_columnconfigure(0, weight=0, minsize=405)
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

        brand_row = ctk.CTkFrame(header, fg_color="transparent")
        brand_row.pack(fill="x")

        ctk.CTkLabel(
            brand_row,
            text="FÁBRICA",
            font=ctk.CTkFont(family="Segoe UI", size=22, weight="bold"),
            text_color=TEXT_PRIMARY,
        ).pack(side="left")
        ctk.CTkLabel(
            brand_row,
            text="TK",
            font=ctk.CTkFont(family="Segoe UI", size=22, weight="bold"),
            text_color=ACCENT_YELLOW,
        ).pack(side="left", padx=(4, 10))
        self.niche_badge = ctk.CTkLabel(
            brand_row,
            text="MULTI-NICHO • 9:16",
            font=ctk.CTkFont(family="Consolas", size=11, weight="bold"),
            text_color=ACCENT_CYAN,
            fg_color=BG_CARD,
            corner_radius=6,
            padx=8,
            pady=2,
        )
        self.niche_badge.pack(side="left")

        controls = ctk.CTkScrollableFrame(
            self.left_panel,
            fg_color="transparent",
            scrollbar_button_color=BORDER_SUBTLE,
            scrollbar_button_hover_color=ACCENT_CYAN_DARK,
        )
        controls.grid(row=1, column=0, sticky="nsew", padx=10, pady=2)
        controls.grid_columnconfigure(0, weight=1)

        # 0. Seletor de NICHO (pastas em nichos/)
        self._section_label(controls, "NICHO (PASTA ISOLADA EM nichos/)", row=0)
        niche_names = [n["nome"] for n in self.niches_list] or ["GTA VI"]
        self.niche_var = ctk.StringVar(value=self.niche_id_to_name.get(self.active_niche_id, niche_names[0]))
        self.niche_menu = ctk.CTkOptionMenu(
            controls,
            values=niche_names,
            variable=self.niche_var,
            command=self._on_niche_changed,
            fg_color=BG_INPUT,
            button_color=ACCENT_YELLOW,
            button_hover_color=ACCENT_YELLOW_HOVER,
            dropdown_fg_color=BG_CARD,
            dropdown_hover_color=BORDER_SUBTLE,
            text_color=TEXT_PRIMARY,
            height=35,
            font=ctk.CTkFont(size=13, weight="bold"),
        )
        self.niche_menu.grid(row=1, column=0, sticky="ew", padx=6, pady=(2, 8))

        # 1. Modo de Conteúdo
        self._section_label(controls, "MODO DE CONTEÚDO", row=2)
        self.mode_var = ctk.StringVar(value=MODE_LABELS["top5"])
        self.mode_menu = ctk.CTkOptionMenu(
            controls,
            values=list(MODE_LABELS.values()),
            variable=self.mode_var,
            fg_color=BG_INPUT,
            button_color=ACCENT_CYAN_DARK,
            button_hover_color=ACCENT_CYAN,
            dropdown_fg_color=BG_CARD,
            dropdown_hover_color=BORDER_SUBTLE,
            text_color=TEXT_PRIMARY,
            height=34,
        )
        self.mode_menu.grid(row=3, column=0, sticky="ew", padx=6, pady=(2, 8))

        # 2. Sub-foco / Preset do Nicho
        self._section_label(controls, "SUB-FOCO / PRESET", row=4)
        self.focus_var = ctk.StringVar(value="")
        self.focus_menu = ctk.CTkOptionMenu(
            controls,
            values=["Geral"],
            variable=self.focus_var,
            command=self._on_focus_changed,
            fg_color=BG_INPUT,
            button_color=ACCENT_CYAN_DARK,
            button_hover_color=ACCENT_CYAN,
            dropdown_fg_color=BG_CARD,
            dropdown_hover_color=BORDER_SUBTLE,
            text_color=TEXT_PRIMARY,
            height=34,
        )
        self.focus_menu.grid(row=5, column=0, sticky="ew", padx=6, pady=(2, 8))

        # Palavras-chave
        self._section_label(controls, "PALAVRAS-CHAVE (PARA MODO NOTÍCIA)", row=6)
        self.keywords_entry = ctk.CTkEntry(
            controls,
            placeholder_text="Separadas por vírgula",
            fg_color=BG_INPUT,
            border_color=BORDER_SUBTLE,
            text_color=TEXT_PRIMARY,
            height=34,
        )
        self.keywords_entry.grid(row=7, column=0, sticky="ew", padx=6, pady=(2, 10))

        # 3. Voz / Idioma Edge-TTS
        self._section_label(controls, "VOZ & IDIOMA (EDGE-TTS)", row=8)
        self.voice_var = ctk.StringVar(value="")
        self.voice_menu = ctk.CTkOptionMenu(
            controls,
            values=["PT-BR • Antonio (Masculino)"],
            variable=self.voice_var,
            fg_color=BG_INPUT,
            button_color=ACCENT_CYAN_DARK,
            button_hover_color=ACCENT_CYAN,
            dropdown_fg_color=BG_CARD,
            dropdown_hover_color=BORDER_SUBTLE,
            text_color=TEXT_PRIMARY,
            height=34,
        )
        self.voice_menu.grid(row=9, column=0, sticky="ew", padx=6, pady=(2, 10))

        # 4. Modelo Ollama + botão Listar Modelos
        self._section_label(controls, "MODELO OLLAMA (ALTERNE LIVREMENTE)", row=10)
        ollama_row = ctk.CTkFrame(controls, fg_color="transparent")
        ollama_row.grid(row=11, column=0, sticky="ew", padx=6, pady=(2, 10))
        ollama_row.grid_columnconfigure(0, weight=1)

        default_model = self.global_cfg.get("ollama_modelo", "qwen3:8b")
        self.model_var = ctk.StringVar(value=default_model)
        self.model_combo = ctk.CTkComboBox(
            ollama_row,
            values=[default_model],
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

        # 5. Quantidade de vídeos no lote (1 a 5)
        batch_header = ctk.CTkFrame(controls, fg_color="transparent")
        batch_header.grid(row=12, column=0, sticky="ew", padx=6, pady=(4, 2))
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
        self.batch_seg.grid(row=13, column=0, sticky="ew", padx=6, pady=(2, 12))

        # 6. Opções avançadas opcionais (100% automático se deixar em branco!)
        self._section_label(controls, "LINK YOUTUBE / PLACAR (OPCIONAL • VAZIO = AUTO)", row=14)
        self.custom_video_entry = ctk.CTkEntry(
            controls,
            placeholder_text="URL YouTube (ou deixe vazio p/ baixar sozinho)",
            fg_color=BG_INPUT,
            border_color=BORDER_SUBTLE,
            text_color=TEXT_PRIMARY,
            height=32,
        )
        self.custom_video_entry.grid(row=15, column=0, sticky="ew", padx=6, pady=(2, 6))

        self.placar_entry = ctk.CTkEntry(
            controls,
            placeholder_text="Placar opcional (ex: AMÉRICA 3 x 2 BARCELONA)",
            fg_color=BG_INPUT,
            border_color=BORDER_SUBTLE,
            text_color=TEXT_PRIMARY,
            height=32,
        )
        self.placar_entry.grid(row=16, column=0, sticky="ew", padx=6, pady=(0, 10))

        # 7. Switches
        switch_card = ctk.CTkFrame(
            controls,
            fg_color=BG_CARD,
            corner_radius=10,
            border_width=1,
            border_color=BORDER_SUBTLE,
        )
        switch_card.grid(row=17, column=0, sticky="ew", padx=6, pady=(2, 6))
        switch_card.grid_columnconfigure(0, weight=1)

        video_cfg = self.global_cfg.get("video", {})
        self.music_var = ctk.BooleanVar(value=bool(video_cfg.get("usar_musica", True)))
        self.music_switch = ctk.CTkSwitch(
            switch_card,
            text="Usar música de fundo (assets/music)",
            variable=self.music_var,
            progress_color=ACCENT_CYAN_DARK,
            button_color=ACCENT_CYAN,
            text_color=TEXT_PRIMARY,
            font=ctk.CTkFont(size=12),
        )
        self.music_switch.grid(row=0, column=0, sticky="w", padx=12, pady=(10, 5))

        self.broll_var = ctk.BooleanVar(value=bool(video_cfg.get("usar_broll", True)))
        self.broll_switch = ctk.CTkSwitch(
            switch_card,
            text="Vídeo real HD automático (yt-dlp / broll)",
            variable=self.broll_var,
            progress_color=ACCENT_CYAN_DARK,
            button_color=ACCENT_CYAN,
            text_color=TEXT_PRIMARY,
            font=ctk.CTkFont(size=12),
        )
        self.broll_switch.grid(row=1, column=0, sticky="w", padx=12, pady=5)

        self.intro_var = ctk.BooleanVar(value=bool(video_cfg.get("usar_intro", True)))
        self.intro_switch = ctk.CTkSwitch(
            switch_card,
            text="Usar intro curta (assets/intros)",
            variable=self.intro_var,
            progress_color=ACCENT_CYAN_DARK,
            button_color=ACCENT_CYAN,
            text_color=TEXT_PRIMARY,
            font=ctk.CTkFont(size=12),
        )
        self.intro_switch.grid(row=2, column=0, sticky="w", padx=12, pady=5)

        self.clean_temp_var = ctk.BooleanVar(value=bool(video_cfg.get("limpar_temp", True)))
        self.clean_temp_switch = ctk.CTkSwitch(
            switch_card,
            text="Limpar pasta temp ao finalizar",
            variable=self.clean_temp_var,
            progress_color=ACCENT_CYAN_DARK,
            button_color=ACCENT_CYAN,
            text_color=TEXT_PRIMARY,
            font=ctk.CTkFont(size=12),
        )
        self.clean_temp_switch.grid(row=3, column=0, sticky="w", padx=12, pady=(5, 10))

        # Rodapé de ações
        actions = ctk.CTkFrame(self.left_panel, fg_color="transparent")
        actions.grid(row=2, column=0, sticky="ew", padx=16, pady=(8, 14))
        actions.grid_columnconfigure(0, weight=1)
        actions.grid_columnconfigure(1, weight=1)

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
        self.generate_btn.grid(row=0, column=0, columnspan=2, sticky="ew", pady=(0, 8))

        self.open_output_btn = ctk.CTkButton(
            actions,
            text="Abrir Output",
            height=36,
            corner_radius=8,
            fg_color=BG_CARD,
            hover_color=BORDER_SUBTLE,
            border_width=1,
            border_color=ACCENT_CYAN_DARK,
            text_color=ACCENT_CYAN,
            font=ctk.CTkFont(size=12, weight="bold"),
            command=self._open_output_folder,
        )
        self.open_output_btn.grid(row=1, column=0, sticky="ew", padx=(0, 4))

        self.open_assets_btn = ctk.CTkButton(
            actions,
            text="Pasta B-Roll/Assets",
            height=36,
            corner_radius=8,
            fg_color=BG_CARD,
            hover_color=BORDER_SUBTLE,
            border_width=1,
            border_color=BORDER_SUBTLE,
            text_color=TEXT_PRIMARY,
            font=ctk.CTkFont(size=12, weight="bold"),
            command=self._open_assets_folder,
        )
        self.open_assets_btn.grid(row=1, column=1, sticky="ew", padx=(4, 0))

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
        self.right_panel.grid_rowconfigure(1, weight=3)
        self.right_panel.grid_rowconfigure(2, weight=4)

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

        # Card de Prévia
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
            text="Gancho: —",
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
        self.preview_box.insert(
            "1.0",
            "Dica: jogue clipes .mp4 (ex.: trailer de GTA VI ou gols da Liga MX) em 'Pasta B-Roll/Assets -> broll' "
            "para misturar vídeo real em movimento com as cenas automáticas!",
        )
        self.preview_box.configure(state="disabled")

        # Console de Logs
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

    def _apply_niche_to_ui(self, niche_id: str) -> None:
        self.active_niche_id = niche_id
        self.runtime_cfg = build_runtime_config(self.global_cfg, niche_id)

        self.niche_badge.configure(text=self.runtime_cfg.get("nicho_etiqueta", f"{niche_id.upper()} • 9:16"))
        modo = str(self.global_cfg.get("modo_conteudo", "top5")).strip().lower()
        self.mode_var.set(MODE_LABELS.get(modo, MODE_LABELS["top5"]))

        presets = self.runtime_cfg.get("presets_foco") or {}
        preset_keys = list(presets.keys()) or [self.runtime_cfg.get("nicho_nome", "Geral")]
        self.focus_menu.configure(values=preset_keys)
        foco = self.runtime_cfg.get("clube_foco") or preset_keys[0]
        if foco not in preset_keys:
            foco = preset_keys[0]
        self.focus_var.set(foco)

        keywords = self.runtime_cfg.get("palavras_chave") or presets.get(foco) or []
        self.keywords_entry.delete(0, "end")
        self.keywords_entry.insert(0, ", ".join(keywords))

        # Vozes específicas do nicho
        vozes = self.runtime_cfg.get("vozes") or [
            {"id": "pt-BR-AntonioNeural", "label": "PT-BR • Antonio", "idioma": "pt-BR"}
        ]
        self.voice_label_to_meta = {v["label"]: v for v in vozes}
        self.voice_id_to_label = {v["id"]: v["label"] for v in vozes}
        labels = [v["label"] for v in vozes]
        self.voice_menu.configure(values=labels)
        cur_voice_id = self.runtime_cfg.get("tts_voz", vozes[0]["id"])
        self.voice_var.set(self.voice_id_to_label.get(cur_voice_id, labels[0]))

    def _on_niche_changed(self, selected_name: str) -> None:
        niche_id = self.niche_name_to_id.get(selected_name)
        if not niche_id:
            return
        self._apply_niche_to_ui(niche_id)
        self._append_log(f"[Nicho] Alterado para '{selected_name}' (pasta: nichos/{niche_id}/)")

    def _on_focus_changed(self, selected_focus: str) -> None:
        presets = self.runtime_cfg.get("presets_foco") or {}
        preset_kw = presets.get(selected_focus)
        if preset_kw:
            self.keywords_entry.delete(0, "end")
            self.keywords_entry.insert(0, ", ".join(preset_kw))

    def _collect_runtime_config(self) -> dict[str, Any]:
        # Atualiza config global
        self.global_cfg["nicho_ativo"] = self.active_niche_id
        self.global_cfg["modo_conteudo"] = LABEL_TO_MODE.get(self.mode_var.get(), "top5")
        self.global_cfg["ollama_modelo"] = self.model_var.get().strip() or "qwen3:8b"

        video_cfg = dict(self.global_cfg.get("video", {}))
        video_cfg["usar_musica"] = bool(self.music_var.get())
        video_cfg["usar_broll"] = bool(self.broll_var.get())
        video_cfg["usar_intro"] = bool(self.intro_var.get())
        video_cfg["limpar_temp"] = bool(self.clean_temp_var.get())
        self.global_cfg["video"] = video_cfg

        # Persiste escolhas do nicho atual (voz, foco, palavras-chave)
        try:
            save_config(self.global_cfg)
            niche_data = load_niche_config(self.active_niche_id)
            voice_meta = self.voice_label_to_meta.get(self.voice_var.get())
            if voice_meta:
                niche_data["voz_padrao"] = voice_meta["id"]
                niche_data["idioma_padrao"] = voice_meta.get("idioma", niche_data.get("idioma_padrao", "pt-BR"))
            niche_data["foco_padrao"] = self.focus_var.get().strip() or niche_data.get("foco_padrao", "")
            raw_kw = self.keywords_entry.get()
            niche_data["palavras_chave"] = [k.strip() for k in raw_kw.split(",") if k.strip()]
            save_niche_config(self.active_niche_id, niche_data)
        except Exception as exc:
            self._append_log(f"Aviso ao salvar configurações: {exc}")

        self.runtime_cfg = build_runtime_config(self.global_cfg, self.active_niche_id)
        self.runtime_cfg["video_url_opcional"] = self.custom_video_entry.get().strip()
        self.runtime_cfg["placar_opcional"] = self.placar_entry.get().strip()
        return self.runtime_cfg

    def _on_batch_changed(self, val: str) -> None:
        n = int(val)
        self.batch_value_label.configure(text=f"{n} vídeo" if n == 1 else f"{n} vídeos")

    def _refresh_ollama_models_async(self) -> None:
        self.refresh_models_btn.configure(state="disabled", text="...")

        def _task() -> None:
            ollama_url = self.global_cfg.get("ollama_url", "http://localhost:11434")
            try:
                models = list_ollama_models(ollama_url)
                self.after(0, lambda: self._on_models_loaded(models, None))
            except Exception as exc:
                self.after(0, lambda err=str(exc): self._on_models_loaded([], err))

        threading.Thread(target=_task, daemon=True).start()

    def _on_models_loaded(self, models: list[str], error: str | None) -> None:
        self.refresh_models_btn.configure(state="normal", text="Listar")
        if error:
            self._append_log(f"[Ollama] Indisponível ({error}). Verifique se 'ollama serve' está rodando.")
            return
        if not models:
            self._append_log("[Ollama] Nenhum modelo instalado. Execute ex.: 'ollama pull qwen3:8b'.")
            return
        self.model_combo.configure(values=models)
        current = self.model_var.get().strip()
        if current not in models:
            self.model_var.set(models[0])
        self._append_log(f"[Ollama] {len(models)} modelo(s) disponíveis: {', '.join(models)}")

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
        lines = [f"PAUTA: {article.title}", f"REF: {article.url}", ""]
        if script is not None:
            self.hook_preview_label.configure(
                text=f"Gancho: {script.gancho_tela}  |  {script.word_count} palavras"
            )
            lines.append("ROTEIRO GERADO:")
            lines.append(script.roteiro)
            lines.append("")
            lines.append(f"DESCRIÇÃO TIKTOK: {script.descricao_tiktok}")
            lines.append(f"HASHTAGS: {' '.join(script.hashtags)}")
        else:
            self.hook_preview_label.configure(text="Gancho: gerando roteiro de alta retenção...")
            snippet = (article.full_text or article.summary or "")[:450]
            lines.append("BASE DA PAUTA:")
            lines.append(snippet + ("..." if len(article.full_text) > 450 else ""))

        self.preview_box.configure(state="normal")
        self.preview_box.delete("1.0", "end")
        self.preview_box.insert("1.0", "\n".join(lines))
        self.preview_box.configure(state="disabled")

    def _set_controls_state(self, running: bool) -> None:
        self.is_running = running
        state = "disabled" if running else "normal"
        for widget in (
            self.niche_menu,
            self.mode_menu,
            self.focus_menu,
            self.keywords_entry,
            self.voice_menu,
            self.model_combo,
            self.refresh_models_btn,
            self.batch_seg,
            self.custom_video_entry,
            self.placar_entry,
            self.music_switch,
            self.broll_switch,
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
            self._append_log("Solicitação de cancelamento enviada. Encerrando com segurança...")
            self.generate_btn.configure(state="disabled", text="CANCELANDO...")
            return

        cfg = self._collect_runtime_config()
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
            self.status_label.configure(
                text=f"Concluído! {ok}/{total} vídeo(s) em nichos/{self.active_niche_id}/output/"
            )
        else:
            self.status_label.configure(text=f"Finalizado com alertas: {ok}/{total} vídeo(s) gerados")

    def _open_folder(self, folder: Path) -> None:
        folder.mkdir(parents=True, exist_ok=True)
        try:
            if sys.platform.startswith("win"):
                os.startfile(str(folder))  # type: ignore[attr-defined]
            elif sys.platform == "darwin":
                subprocess.Popen(["open", str(folder)])
            else:
                subprocess.Popen(["xdg-open", str(folder)])
        except Exception as exc:
            self._append_log(f"Não foi possível abrir pasta ({exc}).")

    def _open_output_folder(self) -> None:
        paths = get_niche_paths(self.active_niche_id)
        self._open_folder(paths["output_dir"])

    def _open_assets_folder(self) -> None:
        paths = get_niche_paths(self.active_niche_id)
        self._open_folder(paths["assets_dir"])


def main() -> None:
    app = FabricaTKApp()
    app.mainloop()


if __name__ == "__main__":
    main()
