#!/usr/bin/env python
"""
Dubbing Tab (UI)
================
A self-contained Tk tab that dubs a chosen video's ORIGINAL dialogue into a
target language.  Deliberately kept OUT of ``complete_automation_gui.py`` so
the main file stays small and the Our Script tab is never touched.

Wiring (two lines in the main file):

    from dubbing_tab import DubbingTabMixin
    class VideoAutomationGUI(DubbingTabMixin, ...):   # add the mixin
        ...
        self.create_dubbing_tab()                     # after the other tabs

The mixin reuses the host GUI's ``self.settings``, ``self.update_setting``,
``self.notebook``, plus ``AppStyles`` / ``ModernButton`` imported here.  It
runs the dub on a worker thread and streams progress into its own log box.
"""

from __future__ import annotations

import os
import threading
import traceback
from pathlib import Path

import tkinter as tk
from tkinter import ttk, filedialog, messagebox, colorchooser

import dubbing_engine


# Populated by create_dubbing_tab() via _styles(); declared here so the bare
# ``AppStyles`` / ``ModernButton`` references in the helper methods resolve as
# module globals rather than raising NameError at import time.
AppStyles = None
ModernButton = None


# ── Non-Gemini dubbing voices ────────────────────────────────────────────
# Per-speaker dropdowns can voice a speaker with Edge / Kokoro / Piper too,
# not just Gemini.  Keys are stored as "<engine>:<id>" and routed by
# dubbing_engine._resolve_voice_settings().  These lists are CURATED (not the
# full catalogs) so the dropdown stays usable; the 🗣 TTS tab still exposes
# every voice for single-speaker dubbing.
_DUB_EDGE_VOICES = [
    # id (alias into TTSGenerator.VOICES)   gender
    ('aria', 'Female'), ('jenny', 'Female'), ('michelle', 'Female'),
    ('emma', 'Female'), ('ana', 'Female'),
    ('guy', 'Male'), ('eric', 'Male'), ('christopher', 'Male'),
    ('andrew', 'Male'), ('brian', 'Male'), ('roger', 'Male'),
    ('sonia', 'Female'), ('libby', 'Female'), ('ryan', 'Male'), ('thomas', 'Male'),
    ('natasha', 'Female'), ('william', 'Male'),
    ('neerja', 'Female'), ('prabhat', 'Male'),
    ('madhur', 'Male'), ('swara', 'Female'),
    ('asad', 'Male'), ('uzma', 'Female'),
    ('katja', 'Female'), ('conrad', 'Male'),
    ('sunhi', 'Female'), ('injoon', 'Male'),
    ('zariyah', 'Female'), ('hamed', 'Male'),
]

# Fallback Kokoro list, used only if the host GUI hasn't built self.kokoro_voices
# yet.  Format matches that list ("id - Gender (desc)").
_DUB_KOKORO_FALLBACK = [
    'af_bella - Female (American, Warm)', 'af_heart - Female (American, Heartfelt)',
    'af_nicole - Female (American, Soft)', 'am_adam - Male (American, Professional)',
    'am_michael - Male (American, Energetic)', 'am_onyx - Male (American, Rich)',
    'bf_emma - Female (British, Elegant)', 'bm_george - Male (British, Distinguished)',
    'hf_alpha - Female (Hindi)',
]


def _styles():
    """Lazy import of the host GUI's shared UI classes.

    Imported inside functions (not at module top) so that
    ``complete_automation_gui`` can ``from dubbing_tab import DubbingTabMixin``
    at class-definition time without a circular-import deadlock — by the time
    any of these run, the main module is fully loaded.
    """
    from complete_automation_gui import AppStyles, ModernButton
    return AppStyles, ModernButton


# Language menu — label shown to the user is what gets sent to the translator.
DUB_LANGUAGES = [
    'English', 'Urdu', 'Hindi', 'Arabic', 'Spanish', 'French', 'German', 'Italian',
    'Portuguese', 'Indonesian', 'Malay', 'Turkish', 'Russian', 'Persian',
    'Bengali', 'Punjabi', 'Tamil', 'Telugu', 'Japanese', 'Korean',
    'Chinese', 'Vietnamese', 'Thai',
]

# Source language options — "Auto-detect" lets whisper figure it out.
SOURCE_LANGUAGES = ['Auto-detect', 'English', 'Urdu', 'Hindi', 'Arabic',
    'Spanish', 'French', 'German', 'Italian', 'Portuguese', 'Indonesian',
    'Malay', 'Turkish', 'Russian', 'Persian', 'Bengali', 'Punjabi', 'Tamil',
    'Telugu', 'Japanese', 'Korean', 'Chinese', 'Vietnamese', 'Thai']


class DubbingTabMixin:
    """Adds a 🎙️ Dubbing tab to the host VideoAutomationGUI."""

    # ── Tab construction ────────────────────────────────────────────────
    def create_dubbing_tab(self):
        global AppStyles, ModernButton
        AppStyles, ModernButton = _styles()
        tab = tk.Frame(self.notebook, bg=AppStyles.BG_CARD)
        self.notebook.add(tab, text='🎙️ Dubbing')

        # Header
        header = tk.Frame(tab, bg=AppStyles.BG_CARD)
        header.pack(fill='x', padx=20, pady=(14, 4))
        tk.Label(header, text='🎙️ Dub Original Dialogue',
                 bg=AppStyles.BG_CARD, fg=AppStyles.TEXT_DARK,
                 font=('Segoe UI', 14, 'bold')).pack(anchor='w')
        tk.Label(header,
                 text='Pick any video → its spoken dialogue is transcribed, '
                      'translated, re-voiced with your TTS engine, and muxed '
                      'back over the (ducked) original audio. No Excel or '
                      'script needed.',
                 bg=AppStyles.BG_CARD, fg=AppStyles.TEXT_MEDIUM,
                 font=('Segoe UI', 9), justify='left', wraplength=520).pack(
                     anchor='w', pady=(2, 0))

        body = tk.Frame(tab, bg=AppStyles.BG_CARD)
        body.pack(fill='both', expand=True, padx=20, pady=(10, 0))

        # Two responsive columns: controls on the LEFT (scrollable, grows to
        # fill), log panel on the RIGHT (narrow, fixed-ish).  Using pack so the
        # left column takes all leftover width at any screen size.
        left_col = tk.Frame(body, bg=AppStyles.BG_CARD)
        left_col.pack(side='left', fill='both', expand=True)

        right_col = tk.Frame(body, bg=AppStyles.BG_CARD, width=300)
        right_col.pack(side='right', fill='y', padx=(12, 0))
        right_col.pack_propagate(False)   # keep the log panel at its set width

        # Scrollable canvas for the input cards (so they don't compete with log)
        canvas = tk.Canvas(left_col, bg=AppStyles.BG_CARD, highlightthickness=0)
        scrollbar = ttk.Scrollbar(left_col, orient='vertical', command=canvas.yview)
        scrollable = tk.Frame(canvas, bg=AppStyles.BG_CARD)
        scrollable.bind('<Configure>',
                        lambda e: canvas.configure(scrollregion=canvas.bbox('all')))
        _win = canvas.create_window((0, 0), window=scrollable, anchor='nw')

        # Cards are laid out in a 3-column grid (parallel, EQUAL width) so the
        # user doesn't have to scroll down a single tall stack. Each card packs
        # fill='x' into one of these columns. Columns are forced to EQUAL width
        # via a grid with `uniform` weight — plain pack(fill='both', expand=True)
        # only equalizes the EXTRA space, so wide cards made narrower columns.
        # The scrollbar still works if the columns exceed the visible height.
        # NOTE: grid_frame is packed at the END of create_dubbing_tab (after the
        # run row + status line) so those full-width controls stay on top.
        self._dub_grid = {}
        self._dub_grid_cols = []
        self._dub_grid_idx = 0
        self._dub_grid_frame = tk.Frame(scrollable, bg=AppStyles.BG_CARD)
        for _ci in range(3):
            self._dub_grid_frame.columnconfigure(_ci, weight=1, uniform='dubcol')
        self._dub_grid_frame.rowconfigure(0, weight=1)
        for _ci in range(3):
            _col = tk.Frame(self._dub_grid_frame, bg=AppStyles.BG_CARD)
            _col.grid(row=0, column=_ci, sticky='nsew', padx=4)
            self._dub_grid_cols.append(_col)
        # Bind the inner frame's width to the canvas width so cards reflow to the
        # available space instead of keeping their natural width and OVERLAPPING.
        canvas.bind('<Configure>', lambda e: canvas.itemconfigure(_win, width=e.width))
        canvas.configure(yscrollcommand=scrollbar.set)
        scrollbar.pack(side='right', fill='y')
        canvas.pack(side='left', fill='both', expand=True)

        # Mousewheel scrolling
        def _on_mousewheel(event):
            canvas.yview_scroll(int(-1 * (event.delta / 120)), 'units')
        canvas.bind_all('<MouseWheel>', _on_mousewheel)
        tab.bind('<Destroy>', lambda e: canvas.unbind_all('<MouseWheel>'))

        # ── 1) Source video ────────────────────────────────────────────
        vid_card = self._dub_card(scrollable, '🎬 Source Video')
        row = tk.Frame(vid_card, bg=AppStyles.BG_CARD)
        row.pack(fill='x', padx=8, pady=6)
        self._dub_video_var = tk.StringVar(
            value=self.settings.get('dub_last_video', ''))
        tk.Entry(row, textvariable=self._dub_video_var, bg=AppStyles.BG_INPUT,
                 fg=AppStyles.TEXT_DARK, font=('Segoe UI', 9), relief='flat').pack(
                     side='left', fill='x', expand=True, padx=(0, 6), ipady=3)

        def _browse_video():
            f = filedialog.askopenfilename(
                title='Select a video to dub',
                filetypes=[('Video files', '*.mp4 *.mov *.mkv *.avi *.webm'),
                           ('All files', '*.*')])
            if f:
                self._dub_video_var.set(f)
                self.update_setting('dub_last_video', f)
        ModernButton(row, text='📁 Browse', bg_color=AppStyles.ACCENT_INFO,
                     font=('Segoe UI', 9, 'bold'), padx=10, pady=3,
                     command=_browse_video).pack(side='left')

        # ── 1a) Batch folder (optional) ─────────────────────────────────
        # If set, EVERY video in this folder is dubbed one-by-one and the
        # single "Source Video" above is ignored.
        frow = tk.Frame(vid_card, bg=AppStyles.BG_CARD)
        frow.pack(fill='x', padx=8, pady=(0, 4))
        tk.Label(frow, text='or folder:', bg=AppStyles.BG_CARD,
                 fg=AppStyles.TEXT_MEDIUM, font=('Segoe UI', 8),
                 width=8, anchor='w').pack(side='left')
        self._dub_folder_var = tk.StringVar(
            value=self.settings.get('dub_batch_folder', ''))
        tk.Entry(frow, textvariable=self._dub_folder_var, bg=AppStyles.BG_INPUT,
                 fg=AppStyles.TEXT_DARK, font=('Segoe UI', 9), relief='flat').pack(
                     side='left', fill='x', expand=True, padx=(0, 6), ipady=3)

        def _browse_folder():
            d = filedialog.askdirectory(
                title='Select a folder of videos to dub (batch)')
            if d:
                self._dub_folder_var.set(d)
                self.update_setting('dub_batch_folder', d)
                self._dub_refresh_batch_count()

        def _clear_folder():
            self._dub_folder_var.set('')
            self.update_setting('dub_batch_folder', '')
            self._dub_refresh_batch_count()
        ModernButton(frow, text='📂 Folder', bg_color=AppStyles.ACCENT_INFO,
                     font=('Segoe UI', 9, 'bold'), padx=10, pady=3,
                     command=_browse_folder).pack(side='left')
        ModernButton(frow, text='✖', bg_color=AppStyles.TEXT_MEDIUM,
                     font=('Segoe UI', 9, 'bold'), padx=8, pady=3,
                     command=_clear_folder).pack(side='left', padx=(4, 0))

        # Recurse into subfolders?
        self._dub_batch_recursive_var = tk.BooleanVar(
            value=bool(self.settings.get('dub_batch_recursive', False)))
        # Skip videos whose output already exists (resume-friendly)?
        self._dub_batch_skip_done_var = tk.BooleanVar(
            value=bool(self.settings.get('dub_batch_skip_done', True)))
        crow2 = tk.Frame(vid_card, bg=AppStyles.BG_CARD)
        crow2.pack(fill='x', padx=8, pady=(0, 2))
        tk.Checkbutton(crow2, text='include subfolders',
                       variable=self._dub_batch_recursive_var,
                       bg=AppStyles.BG_CARD, fg=AppStyles.TEXT_DARK,
                       activebackground=AppStyles.BG_CARD,
                       selectcolor=AppStyles.BG_INPUT, font=('Segoe UI', 8),
                       command=lambda: (self.update_setting(
                           'dub_batch_recursive',
                           self._dub_batch_recursive_var.get()),
                           self._dub_refresh_batch_count())).pack(side='left')
        tk.Checkbutton(crow2, text='skip already-dubbed',
                       variable=self._dub_batch_skip_done_var,
                       bg=AppStyles.BG_CARD, fg=AppStyles.TEXT_DARK,
                       activebackground=AppStyles.BG_CARD,
                       selectcolor=AppStyles.BG_INPUT, font=('Segoe UI', 8),
                       command=lambda: self.update_setting(
                           'dub_batch_skip_done',
                           self._dub_batch_skip_done_var.get())).pack(
                               side='left', padx=(12, 0))
        self._dub_batch_count_var = tk.StringVar(value='')
        tk.Label(vid_card, textvariable=self._dub_batch_count_var,
                 bg=AppStyles.BG_CARD, fg=AppStyles.ACCENT_PRIMARY,
                 font=('Segoe UI', 8, 'italic')).pack(anchor='w', padx=8)
        tk.Label(vid_card,
                 text='   Batch mode: every video in the folder is dubbed into '
                      'the target language one-by-one. Multi-speaker voice '
                      'assignments are per-video, so unmapped speakers use your '
                      'default TTS voice.',
                 bg=AppStyles.BG_CARD, fg=AppStyles.TEXT_MEDIUM,
                 font=('Segoe UI', 8, 'italic'), justify='left',
                 wraplength=500).pack(anchor='w', padx=8, pady=(0, 4))
        self._dub_refresh_batch_count()

        # ── 1b) Source language ────────────────────────────────────────
        src_card = self._dub_card(scrollable, '🔊 Source Language')
        srow = tk.Frame(src_card, bg=AppStyles.BG_CARD)
        srow.pack(fill='x', padx=8, pady=6)
        tk.Label(srow, text='Video is in:', bg=AppStyles.BG_CARD,
                 fg=AppStyles.TEXT_DARK, font=('Segoe UI', 9)).pack(side='left')
        self._dub_src_lang_var = tk.StringVar(
            value=self.settings.get('dub_source_language', 'Auto-detect'))
        src_combo = ttk.Combobox(srow, textvariable=self._dub_src_lang_var,
                                 values=SOURCE_LANGUAGES, width=20)
        src_combo.pack(side='left', padx=(6, 0))
        src_combo.bind('<<ComboboxSelected>>', lambda e: self.update_setting(
            'dub_source_language', self._dub_src_lang_var.get()))
        src_combo.bind('<FocusOut>', lambda e: self.update_setting(
            'dub_source_language', self._dub_src_lang_var.get()))
        tk.Label(srow,
                 text='  "Auto-detect" lets whisper figure it out.',
                 bg=AppStyles.BG_CARD, fg=AppStyles.TEXT_MEDIUM,
                 font=('Segoe UI', 8, 'italic')).pack(side='left', padx=(8, 0))

        # Whisper model size — lets low-VRAM GPUs (e.g. Quadro M1200, 4GB)
        # drop to 'base'/'small' while big GPUs use 'medium'/'large-v3'.
        # 'distil-large-v3' ≈ large-v3 accuracy at ~6× the speed (English).
        mrow = tk.Frame(src_card, bg=AppStyles.BG_CARD)
        mrow.pack(fill='x', padx=8, pady=(0, 6))
        tk.Label(mrow, text='Whisper model:', bg=AppStyles.BG_CARD,
                 fg=AppStyles.TEXT_DARK, font=('Segoe UI', 9)).pack(side='left')
        self._dub_whisper_model_var = tk.StringVar(
            value=self.settings.get('dub_whisper_model', 'medium'))
        self._dub_model_combo = ttk.Combobox(
            mrow, textvariable=self._dub_whisper_model_var,
            values=['tiny', 'base', 'small', 'medium', 'large-v3',
                    'distil-large-v3'],
            state='readonly', width=18)
        self._dub_model_combo.pack(side='left', padx=(6, 0))
        self._dub_model_combo.bind(
            '<<ComboboxSelected>>',
            lambda e: (self.update_setting('dub_whisper_model',
                                           self._dub_whisper_model_var.get()),
                       self._dub_refresh_whisper_status()))
        tk.Label(mrow,
                 text='  Bigger = more accurate but needs more VRAM/time. '
                      'Use "base"/"small" on old or 4GB GPUs.',
                 bg=AppStyles.BG_CARD, fg=AppStyles.TEXT_MEDIUM,
                 font=('Segoe UI', 8, 'italic')).pack(side='left', padx=(8, 0))
        # Path-status label: tells you whether the selected model is already
        # on disk (and where) or will trigger a hub download on first use.
        self._dub_whisper_status = tk.Label(
            src_card, bg=AppStyles.BG_CARD, fg=AppStyles.TEXT_MEDIUM,
            font=('Segoe UI', 8), justify='left', anchor='w')
        self._dub_whisper_status.pack(fill='x', padx=8, pady=(0, 6))
        self._dub_refresh_whisper_status()

        # ── 3) Target language ─────────────────────────────────────────
        lang_card = self._dub_card(scrollable, '🌐 Target Language')
        lrow = tk.Frame(lang_card, bg=AppStyles.BG_CARD)
        lrow.pack(fill='x', padx=8, pady=6)
        tk.Label(lrow, text='Dub into:', bg=AppStyles.BG_CARD,
                 fg=AppStyles.TEXT_DARK, font=('Segoe UI', 9)).pack(side='left')
        self._dub_lang_var = tk.StringVar(
            value=self.settings.get('dub_target_language', 'Urdu'))
        lang_combo = ttk.Combobox(lrow, textvariable=self._dub_lang_var,
                                  values=DUB_LANGUAGES, width=20)
        lang_combo.pack(side='left', padx=(6, 0))
        lang_combo.bind('<<ComboboxSelected>>', lambda e: self.update_setting(
            'dub_target_language', self._dub_lang_var.get()))
        lang_combo.bind('<FocusOut>', lambda e: self.update_setting(
            'dub_target_language', self._dub_lang_var.get()))
        tk.Label(lrow,
                 text='  ⚠ Make sure the 🗣 TTS tab has a voice that speaks '
                      'this language.',
                 bg=AppStyles.BG_CARD, fg=AppStyles.TEXT_MEDIUM,
                 font=('Segoe UI', 8, 'italic')).pack(side='left', padx=(8, 0))

        # ── 2b) Speaker voices (multi-speaker dubbing) ──────────────────
        spk_card = self._dub_card(scrollable, '🎭 Speaker Voices')

        # Master toggle
        self._dub_multi_var = tk.BooleanVar(
            value=bool(self.settings.get('dub_multispeaker', False)))
        tk.Checkbutton(spk_card,
                       text='Multi-speaker dubbing (detect & assign a voice per speaker)',
                       variable=self._dub_multi_var,
                       bg=AppStyles.BG_CARD, fg=AppStyles.TEXT_DARK,
                       activebackground=AppStyles.BG_CARD,
                       selectcolor=AppStyles.BG_INPUT,
                       font=('Segoe UI', 9),
                       command=lambda: self.update_setting(
                           'dub_multispeaker',
                           self._dub_multi_var.get())).pack(
                               anchor='w', padx=8, pady=(2, 2))
        tk.Label(spk_card,
                 text='   Each speaker can use a Gemini, Edge, Kokoro or Piper '
                      'voice (prefixed in the dropdown). Kokoro/Piper are '
                      'offline; Piper lists only downloaded voices.',
                 bg=AppStyles.BG_CARD, fg=AppStyles.TEXT_MEDIUM,
                 font=('Segoe UI', 8, 'italic'), justify='left',
                 wraplength=520).pack(anchor='w', padx=8, pady=(0, 2))

        # HF token (needed only if the local pyannote bundle is missing)
        hrow = tk.Frame(spk_card, bg=AppStyles.BG_CARD)
        hrow.pack(fill='x', padx=8, pady=(2, 2))
        tk.Label(hrow, text='HF Token:', bg=AppStyles.BG_CARD,
                 fg=AppStyles.TEXT_DARK, font=('Segoe UI', 9),
                 width=10, anchor='w').pack(side='left')
        self._dub_hf_var = tk.StringVar(value=self.settings.get('hf_token', ''))
        hf_entry = tk.Entry(hrow, textvariable=self._dub_hf_var, show='•',
                            bg=AppStyles.BG_INPUT, fg=AppStyles.TEXT_DARK,
                            font=('Segoe UI', 9), relief='flat')
        hf_entry.pack(side='left', fill='x', expand=True, padx=(0, 6), ipady=3)
        hf_entry.bind('<FocusOut>', lambda e: self.update_setting(
            'hf_token', self._dub_hf_var.get().strip()))
        tk.Label(spk_card,
                 text='   Optional — only needed if the bundled pyannote models '
                      'are missing (see MULTISPEAKER_DUBBING_PLAN.md §2).',
                 bg=AppStyles.BG_CARD, fg=AppStyles.TEXT_MEDIUM,
                 font=('Segoe UI', 8, 'italic'), justify='left',
                 wraplength=500).pack(anchor='w', padx=8)

        # Exact speaker count — forces pyannote instead of letting it guess
        # (auto-detection often over-splits one voice into several).
        crow = tk.Frame(spk_card, bg=AppStyles.BG_CARD)
        crow.pack(fill='x', padx=8, pady=(4, 2))
        tk.Label(crow, text='Speakers in video:', bg=AppStyles.BG_CARD,
                 fg=AppStyles.TEXT_DARK, font=('Segoe UI', 9),
                 width=16, anchor='w').pack(side='left')
        self._dub_nspk_var = tk.StringVar(
            value=str(self.settings.get('dub_num_speakers') or 'Auto'))
        nspk = ttk.Combobox(
            crow, textvariable=self._dub_nspk_var, width=8, state='readonly',
            values=['Auto', '1', '2', '3', '4', '5', '6', '7', '8'])
        nspk.pack(side='left', padx=(6, 0))
        nspk.bind('<<ComboboxSelected>>', lambda e: self._dub_save_num_speakers())
        tk.Label(crow, text='  (set the exact count for best accuracy)',
                 bg=AppStyles.BG_CARD, fg=AppStyles.TEXT_MEDIUM,
                 font=('Segoe UI', 8, 'italic')).pack(side='left')

        # Detect button + rows container
        drow = tk.Frame(spk_card, bg=AppStyles.BG_CARD)
        drow.pack(fill='x', padx=8, pady=(6, 2))
        self._dub_detect_btn = ModernButton(
            drow, text='🔍 Detect Speakers', bg_color=AppStyles.ACCENT_INFO,
            font=('Segoe UI', 9, 'bold'), padx=10, pady=3,
            command=self._dub_detect_speakers)
        self._dub_detect_btn.pack(side='left')
        self._dub_detect_status = tk.StringVar(value='')
        tk.Label(drow, textvariable=self._dub_detect_status,
                 bg=AppStyles.BG_CARD, fg=AppStyles.ACCENT_PRIMARY,
                 font=('Segoe UI', 8, 'italic')).pack(side='left', padx=(10, 0))

        # Container that per-speaker rows get added to (rebuilt on each detect)
        self._dub_speaker_rows = tk.Frame(spk_card, bg=AppStyles.BG_CARD)
        self._dub_speaker_rows.pack(fill='x', padx=8, pady=(2, 6))
        self._dub_speaker_vars = {}   # speaker label → StringVar(voice key)

        # Rebuild rows from any previously-saved mapping
        _saved_map = self.settings.get('dub_speaker_voices') or {}
        if _saved_map:
            self._dub_build_speaker_rows(sorted(_saved_map.keys()))

        # ── 3) Output + mix controls ───────────────────────────────────
        opt_card = self._dub_card(scrollable, '⚙️ Options')

        orow = tk.Frame(opt_card, bg=AppStyles.BG_CARD)
        orow.pack(fill='x', padx=8, pady=(6, 2))
        tk.Label(orow, text='Output folder:', bg=AppStyles.BG_CARD,
                 fg=AppStyles.TEXT_DARK, font=('Segoe UI', 9)).pack(side='left')
        self._dub_out_var = tk.StringVar(
            value=self.settings.get('dub_output_folder', ''))
        tk.Entry(orow, textvariable=self._dub_out_var, bg=AppStyles.BG_INPUT,
                 fg=AppStyles.TEXT_DARK, font=('Segoe UI', 9), relief='flat').pack(
                     side='left', fill='x', expand=True, padx=(6, 6), ipady=3)

        def _browse_out():
            d = filedialog.askdirectory(title='Select output folder')
            if d:
                self._dub_out_var.set(d)
                self.update_setting('dub_output_folder', d)
        ModernButton(orow, text='📁', bg_color=AppStyles.ACCENT_INFO,
                     font=('Segoe UI', 9, 'bold'), padx=8, pady=3,
                     command=_browse_out).pack(side='left')
        tk.Label(opt_card,
                 text='   Leave blank: writes into '
                      '<parent-folder>_<lang>/ with original filename',
                 bg=AppStyles.BG_CARD, fg=AppStyles.TEXT_MEDIUM,
                 font=('Segoe UI', 8, 'italic')).pack(anchor='w', padx=8)

        # Duck sliders
        mrow = tk.Frame(opt_card, bg=AppStyles.BG_CARD)
        mrow.pack(fill='x', padx=8, pady=(8, 4))
        self._dub_duck_var = tk.DoubleVar(
            value=float(self.settings.get('dub_original_duck', 0.12)))
        self._dub_bg_var = tk.DoubleVar(
            value=float(self.settings.get('dub_original_bg', 0.55)))
        self._dub_slider(mrow, 'Orig. vol. during dub:', self._dub_duck_var,
                         'dub_original_duck')
        self._dub_slider(mrow, 'Orig. vol. (music/gaps):', self._dub_bg_var,
                         'dub_original_bg')
        tk.Label(opt_card,
                 text='   During dub = how loud the original stays UNDER the '
                      'dubbed voice.  Music/gaps = its volume where nobody is '
                      'being dubbed (background music, pauses).',
                 bg=AppStyles.BG_CARD, fg=AppStyles.TEXT_MEDIUM,
                 font=('Segoe UI', 8, 'italic'), justify='left',
                 wraplength=500).pack(anchor='w', padx=8)

        # Max dubbing speed — how much a long translated line may be sped up
        # (pitch-preserving) to stay on the video timeline.  1.0 = never speed
        # up (may drift/overlap); higher = tighter sync, more compressed voice.
        srow = tk.Frame(opt_card, bg=AppStyles.BG_CARD)
        srow.pack(fill='x', padx=8, pady=(2, 4))
        self._dub_speed_var = tk.DoubleVar(
            value=float(self.settings.get('dub_max_speed', 1.6)))
        tk.Label(srow, text='Max voice speed-up:', bg=AppStyles.BG_CARD,
                 fg=AppStyles.TEXT_DARK, font=('Segoe UI', 8),
                 width=18, anchor='w').pack(side='left')
        _spd_lbl = tk.Label(srow, text=f'{self._dub_speed_var.get():.2f}×',
                            bg=AppStyles.BG_CARD, fg=AppStyles.ACCENT_PRIMARY,
                            font=('Segoe UI', 8), width=5)
        _spd_lbl.pack(side='right')

        def _on_speed(v):
            fv = float(v)
            _spd_lbl.config(text=f'{fv:.2f}×')
            self.update_setting('dub_max_speed', round(fv, 2))
        ttk.Scale(srow, from_=1.0, to=2.5, variable=self._dub_speed_var,
                  orient='horizontal', command=_on_speed).pack(
                      side='left', fill='x', expand=True, padx=6)
        tk.Label(opt_card,
                 text='   1.0 = natural (may lag behind scene) · higher = '
                      'tighter sync, more compressed. ~1.6 recommended for '
                      'Urdu/Hindi.',
                 bg=AppStyles.BG_CARD, fg=AppStyles.TEXT_MEDIUM,
                 font=('Segoe UI', 8, 'italic'), justify='left',
                 wraplength=500).pack(anchor='w', padx=8)

        # Keep original music & SFX (Demucs vocal removal) ---------------
        self._dub_keep_music_var = tk.BooleanVar(
            value=bool(self.settings.get('dub_keep_music', False)))
        tk.Checkbutton(opt_card,
                       text='Keep original music & sound effects (remove only voices)',
                       variable=self._dub_keep_music_var,
                       bg=AppStyles.BG_CARD, fg=AppStyles.TEXT_DARK,
                       activebackground=AppStyles.BG_CARD,
                       selectcolor=AppStyles.BG_INPUT,
                       font=('Segoe UI', 8),
                       command=lambda: self.update_setting(
                           'dub_keep_music',
                           self._dub_keep_music_var.get())).pack(
                               anchor='w', padx=8, pady=(6, 0))
        tk.Label(opt_card,
                 text='   Uses AI (Demucs) to strip the actors’ speech while '
                      'keeping the score, ambience & effects at full quality — '
                      'the dub then sits on a clean music/SFX bed. Adds a short '
                      'GPU pass per video; falls back to ducking if unavailable.',
                 bg=AppStyles.BG_CARD, fg=AppStyles.TEXT_MEDIUM,
                 font=('Segoe UI', 8, 'italic'), justify='left',
                 wraplength=500).pack(anchor='w', padx=8, pady=(0, 2))

        self._dub_keep_audio_var = tk.BooleanVar(
            value=self.settings.get('dub_keep_audio_file', False))
        tk.Checkbutton(opt_card,
                       text='Also keep the dubbed audio as a separate .mp3',
                       variable=self._dub_keep_audio_var,
                       bg=AppStyles.BG_CARD, fg=AppStyles.TEXT_DARK,
                       activebackground=AppStyles.BG_CARD,
                       selectcolor=AppStyles.BG_INPUT,
                       font=('Segoe UI', 8),
                       command=lambda: self.update_setting(
                           'dub_keep_audio_file',
                           self._dub_keep_audio_var.get())).pack(
                               anchor='w', padx=8, pady=(2, 0))

        # Burn translated captions onto the dubbed video ------------------
        self._dub_burn_captions_var = tk.BooleanVar(
            value=bool(self.settings.get('dub_burn_captions', True)))
        tk.Checkbutton(opt_card,
                       text='Burn translated captions on the dubbed video',
                       variable=self._dub_burn_captions_var,
                       bg=AppStyles.BG_CARD, fg=AppStyles.TEXT_DARK,
                       activebackground=AppStyles.BG_CARD,
                       selectcolor=AppStyles.BG_INPUT,
                       font=('Segoe UI', 8),
                       command=lambda: self.update_setting(
                           'dub_burn_captions',
                           self._dub_burn_captions_var.get())).pack(
                               anchor='w', padx=8, pady=(2, 0))
        tk.Label(opt_card,
                 text='■ Shows the translated text (dubbed language) synced to '
                      'the new voice, styled from your caption settings. Re-encodes '
                      'the video, so the output becomes H.264.',
                 bg=AppStyles.BG_CARD, fg=AppStyles.TEXT_MEDIUM,
                 font=('Segoe UI', 8, 'italic'), justify='left',
                 wraplength=500).pack(anchor='w', padx=8, pady=(0, 6))

        # ── Caption style (the ASS face the burned captions use) ──────────
        # Shares the SAME clipper preset + animation keys the 💬 Captions tab
        # writes, so the dub burn honors "my selected caption ASS" — and you
        # can pick it right here.
        self._dub_caption_preset_var, self._dub_caption_anim_var = \
            self._dub_build_caption_style_card(scrollable)

        # Alight Motion template status (set in Quick Process tab) ----------
        am_card = self._dub_card(scrollable,
                                 'Alight Motion Template (set in Quick Process)')
        am_body = tk.Frame(am_card, bg=AppStyles.BG_CARD)
        am_body.pack(fill='x', padx=8, pady=(2, 8))

        self._dub_am_status_var = tk.StringVar()
        self._dub_refresh_am_status()
        tk.Label(am_body, textvariable=self._dub_am_status_var,
                 bg=AppStyles.BG_CARD, fg=AppStyles.ACCENT_PRIMARY,
                 font=('Segoe UI', 9, 'bold')).pack(anchor='w')
        tk.Label(am_body,
                 text='   The Alight Motion look is selected in the Quick '
                      'Process > Alight Motion Look Builder card.',
                 bg=AppStyles.BG_CARD, fg=AppStyles.TEXT_MEDIUM,
                 font=('Segoe UI', 8, 'italic'), wraplength=520,
                 justify='left').pack(anchor='w', pady=(2, 0))

        self._dub_apply_am_var = tk.BooleanVar(
            value=bool(self.settings.get('dub_apply_am', False)))
        tk.Checkbutton(am_body,
                       text='Apply the Alight Motion template to the dubbed video',
                       variable=self._dub_apply_am_var,
                       bg=AppStyles.BG_CARD, fg=AppStyles.TEXT_DARK,
                       activebackground=AppStyles.BG_CARD,
                       selectcolor=AppStyles.BG_INPUT,
                       font=('Segoe UI', 8),
                       command=lambda: self.update_setting(
                           'dub_apply_am',
                           self._dub_apply_am_var.get())).pack(
                               anchor='w', pady=(4, 0))
        tk.Label(am_body,
                 text='   Re-renders the video with the AM look applied on top '
                      'of the dub (output becomes H.264).',
                 bg=AppStyles.BG_CARD, fg=AppStyles.TEXT_MEDIUM,
                 font=('Segoe UI', 8, 'italic'), wraplength=520,
                 justify='left').pack(anchor='w')

        # Include transitions from Transitions tab --------------------------
        trans_card = self._dub_card(scrollable,
                                    'Include Transitions from Transitions Tab')
        trans_body = tk.Frame(trans_card, bg=AppStyles.BG_CARD)
        trans_body.pack(fill='x', padx=8, pady=(2, 8))

        self._dub_include_transitions_var = tk.BooleanVar(
            value=bool(self.settings.get('dub_include_transitions', False)))
        tk.Checkbutton(trans_body,
                       text='Apply enabled transitions to the dubbed video',
                       variable=self._dub_include_transitions_var,
                       bg=AppStyles.BG_CARD, fg=AppStyles.TEXT_DARK,
                       activebackground=AppStyles.BG_CARD,
                       selectcolor=AppStyles.BG_INPUT,
                       font=('Segoe UI', 8),
                       command=lambda: self.update_setting(
                           'dub_include_transitions',
                           self._dub_include_transitions_var.get())).pack(
                               anchor='w', pady=(2, 0))

        self._dub_trans_summary_var = tk.StringVar()
        self._dub_refresh_trans_summary()
        tk.Label(trans_body, textvariable=self._dub_trans_summary_var,
                 bg=AppStyles.BG_CARD, fg=AppStyles.TEXT_MEDIUM,
                 font=('Segoe UI', 8, 'italic'), wraplength=520,
                 justify='left').pack(anchor='w', pady=(4, 0))
        tk.Button(trans_body, text='Refresh transition list',
                  command=self._dub_refresh_trans_summary,
                  bg=AppStyles.BG_INPUT, fg=AppStyles.TEXT_DARK,
                  font=('Segoe UI', 8), padx=10, pady=2).pack(
                      anchor='w', pady=(6, 0))

        # Our-Script visual overlays (mirror the Our Script tab cards) -------
        # Our-Script visual overlays (mirror the Our Script tab cards) -------
        # Each card below shares the EXACT same settings keys as the matching
        # Our Script tab card, so the dubbed render honors precisely what you
        # set there (region blur + border, custom blur regions, title, CTA).
        self._dub_build_region_blur_card(scrollable)
        self._dub_build_custom_blur_card(scrollable)
        self._dub_build_title_card(scrollable)
        self._dub_build_bottom_text_card(scrollable)
        # ── 4) Run button + progress ───────────────────────────────────
        run_row = tk.Frame(scrollable, bg=AppStyles.BG_CARD)
        run_row.pack(fill='x', pady=(4, 2))
        self._dub_run_btn = ModernButton(
            run_row, text='▶  Dub Video', bg_color=AppStyles.ACCENT_SUCCESS,
            hover_color='#059669', font=('Segoe UI', 11, 'bold'),
            padx=18, pady=8, command=self._dub_start)
        self._dub_run_btn.pack(side='left')
        self._dub_preview_btn = ModernButton(
            run_row, text='🖼  Preview overlays', bg_color=AppStyles.ACCENT_INFO,
            hover_color='#0891b2', font=('Segoe UI', 10, 'bold'),
            padx=14, pady=6, command=self._dub_preview_overlays)
        self._dub_preview_btn.pack(side='left', padx=(8, 0))
        self._dub_live_preview_btn = ModernButton(
            run_row, text='🔍  Live Preview', bg_color=AppStyles.ACCENT_WARNING,
            hover_color='#d97706', font=('Segoe UI', 10, 'bold'),
            padx=14, pady=6, command=self._dub_open_live_preview)
        self._dub_live_preview_btn.pack(side='left', padx=(8, 0))

        self._dub_progress_var = tk.DoubleVar(value=0)
        ttk.Progressbar(run_row, mode='determinate',
                        variable=self._dub_progress_var,
                        style='Modern.Horizontal.TProgressbar').pack(
                            side='left', fill='x', expand=True, padx=(12, 0))

        self._dub_status_var = tk.StringVar(value='Ready.')
        tk.Label(scrollable, textvariable=self._dub_status_var,
                 bg=AppStyles.BG_CARD, fg=AppStyles.ACCENT_PRIMARY,
                 font=('Segoe UI', 9)).pack(anchor='w', pady=(0, 4))

        # Pack the 3-column card grid LAST so it flows below the run row/status.
        # Constrain it to the scrollable width (pack_propagate off) so the equal
        # columns shrink to fit the left area instead of spilling under the log.
        if getattr(self, '_dub_grid_frame', None) is not None:
            _gf = self._dub_grid_frame
            _gf.pack_propagate(False)
            _gf.pack(fill='x', side='top')
            # Constrain both the scrollable window and grid_frame to the canvas
            # width so the equal columns fit the left area, not the log. This
            # REPLACES the earlier canvas <Configure> binding (Tk bind is not
            # additive), so it must also reflow the scrollable window.
            try:
                canvas.bind(
                    '<Configure>',
                    lambda e: (canvas.itemconfigure(_win, width=e.width),
                               _gf.config(width=max(e.width - 12, 200))))
            except Exception:
                pass

        # ── 5) Log box — small panel on the RIGHT, fills its column height ──
        # Mirrors the Our Script tab's log: toolbar (auto-scroll/clear/save/
        # line-count) + timestamped colored lines.
        log_card = tk.Frame(right_col, bg=AppStyles.BG_CARD,
                            highlightbackground='#30363d', highlightthickness=1)
        log_card.pack(fill='both', expand=True)
        tk.Label(log_card, text='📋 Dub Log', bg=AppStyles.BG_CARD,
                 fg=AppStyles.ACCENT_PRIMARY,
                 font=('Segoe UI', 10, 'bold')).pack(anchor='w', padx=8, pady=(6, 0))
        # Toolbar: auto-scroll + clear + save + line count
        _log_toolbar = tk.Frame(log_card, bg=AppStyles.BG_CARD)
        _log_toolbar.pack(fill='x', padx=6, pady=(2, 1))
        self._dub_log_autoscroll_var = tk.BooleanVar(value=True)
        tk.Checkbutton(
            _log_toolbar, text='Auto-scroll',
            variable=self._dub_log_autoscroll_var,
            bg=AppStyles.BG_CARD, fg=AppStyles.TEXT_DARK,
            selectcolor=AppStyles.BG_INPUT,
            activebackground=AppStyles.BG_CARD,
            font=('Segoe UI', 8)).pack(side='left')
        ModernButton(_log_toolbar, text='🗑 Clear', bg_color=AppStyles.BG_INPUT,
                     font=('Segoe UI', 8, 'bold'), padx=4, pady=1,
                     command=self._dub_log_clear).pack(side='left', padx=(6, 0))
        ModernButton(_log_toolbar, text='💾 Save', bg_color=AppStyles.BG_INPUT,
                     font=('Segoe UI', 8, 'bold'), padx=4, pady=1,
                     command=self._dub_log_save).pack(side='left', padx=(4, 0))
        self._dub_log_lines_var = tk.StringVar(value='0 lines')
        tk.Label(_log_toolbar, textvariable=self._dub_log_lines_var,
                 bg=AppStyles.BG_CARD, fg=AppStyles.TEXT_MEDIUM,
                 font=('Segoe UI', 8, 'italic')).pack(side='right')

        _log_wrap = tk.Frame(log_card, bg=AppStyles.BG_CARD)
        _log_wrap.pack(fill='both', expand=True, padx=6, pady=6)
        _log_scroll = ttk.Scrollbar(_log_wrap, orient='vertical')
        _log_scroll.pack(side='right', fill='y')
        self._dub_log_widget = tk.Text(
            _log_wrap, width=1, wrap='word', bg=AppStyles.BG_INPUT,
            fg=AppStyles.TEXT_DARK, font=('Consolas', 8), relief='flat', bd=4,
            yscrollcommand=_log_scroll.set)
        self._dub_log_widget.pack(side='left', fill='both', expand=True)
        _log_scroll.config(command=self._dub_log_widget.yview)
        # Level colors — mirror the Our Script log tags.
        _lt = self._dub_log_widget
        _lt.tag_configure('ts', foreground='#5b6572')
        _lt.tag_configure('info', foreground='#e2e8f0')
        _lt.tag_configure('ok', foreground='#4ade80')
        _lt.tag_configure('warn', foreground='#fbbf24')
        _lt.tag_configure('error', foreground='#f87171')
        _lt.tag_configure('path', foreground='#38bdf8')
        _lt.tag_configure('header', foreground='#a78bfa', font=('Consolas', 8, 'bold'))
        self._dub_running = False

    # ── Log toolbar helpers (mirror Our Script's _os_log_clear/_os_log_save) ──
    def _dub_log_clear(self):
        if not hasattr(self, '_dub_log_widget'):
            return
        try:
            self._dub_log_widget.configure(state='normal')
            self._dub_log_widget.delete('1.0', 'end')
            self._dub_log_widget.configure(state='disabled')
            if hasattr(self, '_dub_log_lines_var'):
                self._dub_log_lines_var.set('0 lines')
        except Exception:
            pass

    def _dub_log_save(self):
        if not hasattr(self, '_dub_log_widget'):
            return
        try:
            from tkinter import filedialog as _fd
            from datetime import datetime as _dt
            ts = _dt.now().strftime('%Y%m%d_%H%M%S')
            path = _fd.asksaveasfilename(
                title='Save dub log', defaultextension='.txt',
                initialfile=f'dub_log_{ts}.txt',
                filetypes=[('Text files', '*.txt'), ('All files', '*.*')])
            if not path:
                return
            with open(path, 'w', encoding='utf-8') as f:
                f.write(self._dub_log_widget.get('1.0', 'end-1c'))
        except Exception:
            pass

    # ── Batch-folder helpers ────────────────────────────────────────────
    VIDEO_EXTS = ('.mp4', '.mov', '.mkv', '.avi', '.webm', '.m4v', '.flv',
                  '.wmv', '.mpg', '.mpeg', '.ts')

    def _dub_scan_folder(self, folder: str, recursive: bool):
        """Return a sorted list of video Paths in *folder*.

        Skips our own outputs so a re-run over the same folder never dubs
        a dub — both legacy ``*_dubbed_*.mp4`` files and the new subfolder
        layout (``<parent>_<lang>/original_name.mp4``) are excluded.
        """
        from pathlib import Path as _P
        base = _P(folder)
        if not base.is_dir():
            return []
        # Compute current output-subfolder suffix (e.g. "_english") so
        # files sitting inside a previously-dubbed output folder are skipped.
        # NB: _dub_scan_folder can run during tab-build (via
        # _dub_refresh_batch_count) BEFORE _dub_lang_var is created, so guard it.
        _lang = ''
        if hasattr(self, '_dub_lang_var'):
            _lang = (self._dub_lang_var.get() or '').strip()
        _safe_lang = _lang.lower().replace(' ', '_') if _lang else ''
        it = base.rglob('*') if recursive else base.glob('*')
        vids = []
        for p in it:
            if not p.is_file():
                continue
            if p.suffix.lower() not in self.VIDEO_EXTS:
                continue
            if '_dubbed_' in p.stem.lower():
                continue
            # Skip files inside a previous dub-output subfolder
            if _safe_lang and p.parent.name.endswith(f'_{_safe_lang}'):
                continue
            vids.append(p)
        return sorted(vids, key=lambda p: str(p).lower())

    def _dub_batch_out_path(self, src, lang: str):
        """Where the dub for *src* is written.

        Creates a subfolder named ``<parent-folder>_<lang>`` next to the
        source video and keeps the **original filename** (no ``_dubbed_``
        suffix).  If the user set a custom output-folder override in the UI
        that folder is used as the base instead of the source's parent.

        Example:
            ``videos/MyChannel/video.mp4`` dubbed to ``english`` →
            ``videos/MyChannel/MyChannel_english/video.mp4``
        """
        from pathlib import Path as _P
        src = _P(src)
        out_folder = (self._dub_out_var.get() or '').strip()
        safe_lang = lang.lower().replace(' ', '_')
        # The "channel name" is the source video's parent folder name.
        parent_name = src.parent.name
        subfolder_name = f'{parent_name}_{safe_lang}'
        base_dir = _P(out_folder) if out_folder else src.parent
        output_dir = base_dir / subfolder_name
        try:
            output_dir.mkdir(parents=True, exist_ok=True)
        except Exception:
            pass  # will fail downstream if the dir is unwritable
        return output_dir / src.name  # keep original filename

    def _dub_whisper_model_paths(self, model_size):
        """Candidate locations for a whisper model, mirroring the lookup in
        ``_whisper_word_timestamps.py::_resolve_local_model`` so the status
        label agrees with what the subprocess will actually find.

        Returns (bundled_flat, bundled_hf, user_hf): the three places a model
        can live, each a Path (may not exist).
        """
        here = Path(__file__).resolve().parent
        short = model_size
        if short.startswith('distil-'):
            short = short[len('distil-'):]
        # Flat bundled folders travel with the portable app.
        bundled_flat = here / 'models' / 'whisper' / f'faster-whisper-{model_size}'
        # HF-cache layout: the `models--` prefix + snapshots/<hash>/.
        bundled_hf = (here / 'models' / 'whisper'
                      / f'models--Systran--faster-whisper-{model_size}')
        user_hf = (Path.home() / '.cache' / 'huggingface' / 'hub'
                   / f'models--Systran--faster-whisper-{model_size}')
        # distil-large-v3 lives under the distil repo name in HF-cache form.
        if short != model_size:
            bundled_hf = (here / 'models' / 'whisper'
                          / f'models--Systran--faster-distil-whisper-{short}')
            user_hf = (Path.home() / '.cache' / 'huggingface' / 'hub'
                       / f'models--Systran--faster-distil-whisper-{short}')
        return bundled_flat, bundled_hf, user_hf

    def _dub_find_whisper_model(self, model_size):
        """Return the on-disk folder for ``model_size`` if found, else None.
        Checks flat bundle, app-level HF cache, then the user HF cache."""
        for root in self._dub_whisper_model_paths(model_size):
            try:
                if (root / 'model.bin').is_file() and (root / 'config.json').is_file():
                    return root
                for snap in root.glob('snapshots/*'):
                    if (snap / 'model.bin').is_file() and (snap / 'config.json').is_file():
                        return snap
            except OSError:
                continue
        return None

    def _dub_refresh_whisper_status(self):
        """Refresh the whisper-model status label: found (where) vs will-download."""
        if not hasattr(self, '_dub_whisper_status'):
            return
        model = self._dub_whisper_model_var.get() if hasattr(
            self, '_dub_whisper_model_var') else 'medium'
        found = self._dub_find_whisper_model(model)
        if found is not None:
            self._dub_whisper_status.config(
                text=f'✅ {model} installed at:\n   {found}',
                fg=AppStyles.ACCENT_PRIMARY)
        else:
            self._dub_whisper_status.config(
                text=(f'⚠️ {model} NOT downloaded — will auto-download on '
                      f'first use (needs internet, can take a while).'),
                fg='#e3b341')

    def _dub_refresh_batch_count(self):
        """Update the '(N videos found)' hint next to the folder picker."""
        try:
            folder = (self._dub_folder_var.get() or '').strip()
        except Exception:
            return
        if not folder:
            self._dub_batch_count_var.set('')
            return
        vids = self._dub_scan_folder(
            folder, bool(self._dub_batch_recursive_var.get()))
        n = len(vids)
        if n == 0:
            self._dub_batch_count_var.set('   ⚠ no videos found in that folder')
        else:
            self._dub_batch_count_var.set(
                f'   📂 batch mode ON — {n} video(s) queued')

    # ── Small UI helpers ────────────────────────────────────────────────
    def _dub_card(self, parent, title):
        # Distribute cards across the 3-column grid (equal width, parallel).
        # Fall back to packing directly onto *parent* if the grid isn't built
        # (e.g. isolated harnesses that only call a single *_card builder).
        cols = getattr(self, '_dub_grid_cols', None)
        if cols:
            target = cols[self._dub_grid_idx % len(cols)]
            self._dub_grid_idx += 1
        else:
            target = parent
        card = tk.Frame(target, bg=AppStyles.BG_CARD,
                        highlightbackground='#30363d', highlightthickness=1)
        card.pack(fill='x', pady=3)
        tk.Label(card, text=title, bg=AppStyles.BG_CARD,
                 fg=AppStyles.ACCENT_PRIMARY,
                 font=('Segoe UI', 9, 'bold')).pack(anchor='w', padx=6, pady=(4, 1))
        card._body = None
        return card

    def _dub_touch_preview(self, *_):
        """Refresh the dub live preview if it is open; else do nothing.

        Used as the overlay slider callback so tuning the controls re-renders
        the open scrub preview without popping an error when no video loaded.
        """
        if getattr(self, '_dub_live_preview_refresh', None) is None:
            return
        try:
            # Only re-render if a live-preview window is actually open.
            if getattr(self, '_dub_lp_canvas', None) is None:
                return
            self._dub_live_preview_refresh()
        except Exception:
            pass

    def _dub_build_caption_style_card(self, scrollable):
        """Caption style picker for the burned captions (shared clipper keys)."""
        card = self._dub_card(scrollable, '🎨 Caption Style (burned ASS)')
        body = tk.Frame(card, bg=AppStyles.BG_CARD)
        body.pack(fill='x', padx=8, pady=(2, 8))
        try:
            import clipper_captions as _cc
            _data = _cc.get_presets_for_gui()
        except Exception:
            _data = []
        _labels = [p['label'] for p in _data]
        _map = {p['label']: p['id'] for p in _data}
        if not _map:
            _map = {'Bold White': 'bold_white'}
            _labels = ['Bold White']
        _saved = self.settings.get('clipper_caption_preset', 'bold_white')
        _saved_label = next((l for l, i in _map.items() if i == _saved), _labels[0])

        row1 = tk.Frame(body, bg=AppStyles.BG_CARD)
        row1.pack(fill='x', pady=(0, 3))
        tk.Label(row1, text='Style preset:', bg=AppStyles.BG_CARD,
                 fg=AppStyles.TEXT_DARK, font=('Segoe UI', 8)).pack(side='left')
        preset_var = tk.StringVar(value=_saved_label)
        cb = ttk.Combobox(row1, textvariable=preset_var, values=_labels,
                          state='readonly', width=24, font=('Segoe UI', 8))
        cb.pack(side='left', padx=(6, 0))
        cb.bind('<<ComboboxSelected>>', lambda e: self.update_setting(
            'clipper_caption_preset',
            _map.get(preset_var.get(), 'bold_white')))

        row2 = tk.Frame(body, bg=AppStyles.BG_CARD)
        row2.pack(fill='x', pady=(0, 2))
        tk.Label(row2, text='Animation:', bg=AppStyles.BG_CARD,
                 fg=AppStyles.TEXT_DARK, font=('Segoe UI', 8)).pack(side='left')
        anim_var = tk.StringVar(value=self.settings.get(
            'clipper_caption_animation', 'none'))
        an = ttk.Combobox(row2, textvariable=anim_var,
                          values=['none', 'highlight', 'word_reveal',
                                  'one_word', 'karaoke'],
                          state='readonly', width=24, font=('Segoe UI', 8))
        an.pack(side='left', padx=(6, 0))
        an.bind('<<ComboboxSelected>>', lambda e: self.update_setting(
            'clipper_caption_animation', anim_var.get()))

        tk.Label(body,
                 text='   Shared with the 💬 Captions tab — the dub burns '
                      'captions in exactly this style. Overrides there '
                      '(colors / font / stroke / bg) apply too.',
                 bg=AppStyles.BG_CARD, fg=AppStyles.TEXT_MEDIUM,
                 font=('Segoe UI', 8, 'italic'), wraplength=520,
                 justify='left').pack(anchor='w', pady=(2, 0))
        return preset_var, anim_var

    def _dub_cb_caption_dialog(self, status_label):
        """'Add Custom Blur Region' dialog for the DUB custom-blur card."""
        root = self.root
        dg = tk.Toplevel(root)
        dg.title('Add Custom Blur Region')
        dg.geometry('420x340')
        dg.configure(bg=AppStyles.BG_CARD)
        dg.transient(root)
        dg.grab_set()
        fr = tk.Frame(dg, bg=AppStyles.BG_CARD, padx=16, pady=12)
        fr.pack(fill='both', expand=True)

        def _scaled_row(label, lo, hi, var):
            tk.Label(fr, text=label, bg=AppStyles.BG_CARD,
                     fg=AppStyles.TEXT_DARK, font=('Segoe UI', 9)).pack(anchor='w')
            tk.Scale(fr, from_=lo, to=hi, resolution=1, orient='horizontal',
                     variable=var, bg=AppStyles.BG_CARD, fg=AppStyles.TEXT_DARK,
                     troughcolor=AppStyles.BG_INPUT, length=300).pack(fill='x')

        x_v = tk.DoubleVar(value=0)
        _scaled_row('X offset (%, 0=left edge):', 0, 100, x_v)
        y_v = tk.DoubleVar(value=0)
        _scaled_row('Y offset (%, 0=top edge):', 0, 100, y_v)
        w_v = tk.DoubleVar(value=20)
        _scaled_row('Width (%, of video):', 1, 100, w_v)
        h_v = tk.DoubleVar(value=15)
        _scaled_row('Height (%, of video):', 1, 100, h_v)

        def _save():
            regions = self.settings.get('custom_blur_regions', []) or []
            idx = len(regions) + 1
            regions.append({
                'label': f'Region {idx}', 'enabled': True,
                'x': int(x_v.get()), 'y': int(y_v.get()),
                'width': int(w_v.get()), 'height': int(h_v.get()),
            })
            self.update_setting('custom_blur_regions', regions)
            dg.destroy()
            self._os_bb_rebuild_cb_list(status_label, status_label,
                                        '_dub_cb', self._dub_touch_preview)
        btn = tk.Frame(fr, bg=AppStyles.BG_CARD)
        btn.pack(pady=12)
        ModernButton(btn, text='💾 Save', bg_color=AppStyles.ACCENT_SUCCESS,
                     font=('Segoe UI', 9, 'bold'), padx=16, pady=4,
                     command=_save).pack(side='left', padx=6)
        ModernButton(btn, text='Cancel', bg_color='#6c757d',
                     font=('Segoe UI', 9), padx=16, pady=4,
                     command=dg.destroy).pack(side='left', padx=6)

    # ── Our-Script overlay mirrors ──────────────────────────────────────────
    # Exactly the same style controls as the OurScript tab's cards, gated by
    # the per-feature dubbing checkboxes. Every control writes the SAME shared
    # settings key the OurScript tab uses, so the dub renders IDENTICAL.
    # (_dub_* attribute names keep each tab's widget handles independent.)
    def _dub_build_region_blur_card(self, scrollable):
        """Region Blur + Border — mirror of the OurScript card (same keys)."""
        card = self._dub_card(scrollable, '🌻 Region Blur + Border')
        b = tk.Frame(card, bg=AppStyles.BG_CARD)
        b.pack(fill='x', padx=8, pady=(2, 8))

        self._dub_region_blur_var = tk.BooleanVar(
            value=bool(self.settings.get('dub_region_blur', False)))
        tk.Checkbutton(b, text='Apply Region Blur to the dubbed video',
                       variable=self._dub_region_blur_var,
                       bg=AppStyles.BG_CARD, fg=AppStyles.TEXT_DARK,
                       activebackground=AppStyles.BG_CARD,
                       selectcolor=AppStyles.BG_INPUT, font=('Segoe UI', 8, 'bold'),
                       command=lambda: self.update_setting(
                           'dub_region_blur', self._dub_region_blur_var.get())
                       ).pack(anchor='w', pady=(0, 2))

        def _slider(pr, label, key, lo, hi, default, fmt):
            sr = tk.Frame(pr, bg=AppStyles.BG_CARD)
            sr.pack(fill='x', padx=0, pady=0)
            tk.Label(sr, text=label, bg=AppStyles.BG_CARD,
                     fg=AppStyles.TEXT_MEDIUM, font=('Segoe UI', 7)).pack(side='left')
            var = tk.DoubleVar(value=self.settings.get(key, default))
            vl = tk.Label(sr, text=fmt(var.get()), bg=AppStyles.BG_CARD,
                          fg=AppStyles.ACCENT_PRIMARY, font=('Segoe UI', 7, 'bold'),
                          width=4, anchor='e')
            vl.pack(side='right')
            tk.Scale(sr, from_=lo, to=hi, orient='horizontal', variable=var,
                     bg=AppStyles.BG_CARD, fg=AppStyles.TEXT_DARK,
                     troughcolor=AppStyles.BG_INPUT, showvalue=False,
                     sliderlength=14, highlightthickness=0, length=120,
                     command=lambda v, k=key, lb=vl: (
                         self.update_setting(k, int(float(v))),
                         lb.config(text=fmt(int(float(v)))),
                         self._dub_touch_preview())
                     ).pack(side='left', fill='x', expand=True, padx=(4, 4))
            return var

        # Mode: blur / cover.
        mode_r = tk.Frame(b, bg=AppStyles.BG_CARD)
        mode_r.pack(fill='x', pady=(2, 0))
        tk.Label(mode_r, text='Mode:', bg=AppStyles.BG_CARD,
                 fg=AppStyles.TEXT_DARK, font=('Segoe UI', 8)).pack(side='left')
        self._dub_rb_mode_var = tk.StringVar(
            value=self.settings.get('region_blur_mode', 'blur'))
        mode_cb = ttk.Combobox(mode_r, textvariable=self._dub_rb_mode_var,
                               values=['blur', 'cover'], state='readonly', width=8)
        mode_cb.pack(side='left', padx=(4, 0))
        self._dub_rb_blur_frame = tk.Frame(b, bg=AppStyles.BG_CARD)
        self._dub_rb_cover_frame = tk.Frame(b, bg=AppStyles.BG_CARD)
        self._dub_rb_blur_frame.pack(fill='x', pady=(2, 0))

        def _mode_changed(*_):
            self.update_setting('region_blur_mode', self._dub_rb_mode_var.get())
            self._dub_rb_blur_frame.pack_forget()
            self._dub_rb_cover_frame.pack_forget()
            if self._dub_rb_mode_var.get() == 'cover':
                self._dub_rb_cover_frame.pack(fill='x', pady=(2, 0))
            else:
                self._dub_rb_blur_frame.pack(fill='x', pady=(2, 0))
            self._dub_touch_preview()
        mode_cb.bind('<<ComboboxSelected>>', lambda e: _mode_changed())

        # Per-side toggles + sliders (2x2).
        def _side(col_label, size_key, enable_key, lo, hi, default):
            col = tk.Frame(self._dub_rb_blur_frame, bg=AppStyles.BG_CARD)
            col.pack(side='left', fill='x', expand=True, padx=2)
            en = tk.BooleanVar(value=self.settings.get(enable_key, False))
            tk.Checkbutton(col, text=col_label, variable=en,
                           bg=AppStyles.BG_CARD, fg=AppStyles.TEXT_DARK,
                           activebackground=AppStyles.BG_CARD,
                           selectcolor=AppStyles.BG_INPUT, font=('Segoe UI', 7),
                           command=lambda: (
                               self.update_setting(enable_key, en.get()),
                               self._dub_touch_preview())
                           ).pack(anchor='w')
            sr = tk.Frame(col, bg=AppStyles.BG_CARD)
            sr.pack(fill='x')
            sv = tk.DoubleVar(value=self.settings.get(size_key, default))
            sl = tk.Label(sr, text=f'{int(sv.get())}%', bg=AppStyles.BG_CARD,
                          fg=AppStyles.ACCENT_PRIMARY, font=('Segoe UI', 7, 'bold'))
            sl.pack()
            tk.Scale(sr, from_=lo, to=hi, orient='horizontal', variable=sv,
                     bg=AppStyles.BG_CARD, fg=AppStyles.TEXT_DARK,
                     troughcolor=AppStyles.BG_INPUT, showvalue=False,
                     sliderlength=14, highlightthickness=0, length=40,
                     command=lambda v, k=size_key, lb=sl: (
                         self.update_setting(k, int(float(v))),
                         lb.config(text=f'{int(float(v))}%'),
                         self._dub_touch_preview())
                     ).pack(fill='x')
        s1 = tk.Frame(self._dub_rb_blur_frame, bg=AppStyles.BG_CARD)
        s1.pack(fill='x', pady=(2, 0))
        _side('▴ Top', 'blur_top_size', 'blur_enable_top', 5, 100, 20)
        _side('▾ Bot', 'blur_bottom_size', 'blur_enable_bottom', 5, 100, 20)
        s2 = tk.Frame(self._dub_rb_blur_frame, bg=AppStyles.BG_CARD)
        s2.pack(fill='x')
        _side('◂ Left', 'blur_left_size', 'blur_enable_left', 5, 100, 20)
        _side('▸ Right', 'blur_right_size', 'blur_enable_right', 5, 100, 20)

        # Crop spinboxes.
        crop_r = tk.Frame(b, bg=AppStyles.BG_CARD)
        crop_r.pack(fill='x', padx=0, pady=(2, 0))
        tk.Label(crop_r, text='Crop:', bg=AppStyles.BG_CARD,
                 fg=AppStyles.TEXT_DARK, font=('Segoe UI', 7, 'bold')).pack(side='left')
        for clabel, ckey, cdefault in [('T:', 'blur_crop_top', 0),
                                       ('B:', 'blur_crop_bottom', 30),
                                       ('L:', 'blur_crop_left', 0),
                                       ('R:', 'blur_crop_right', 0)]:
            sf = tk.Frame(crop_r, bg=AppStyles.BG_CARD)
            sf.pack(side='left', padx=(1, 0))
            tk.Label(sf, text=clabel, bg=AppStyles.BG_CARD,
                     fg=AppStyles.TEXT_MEDIUM, font=('Segoe UI', 6)).pack(side='left')
            sv = tk.IntVar(value=self.settings.get(ckey, cdefault))
            tk.Spinbox(sf, from_=0, to=50, width=2, textvariable=sv,
                       bg=AppStyles.BG_INPUT, fg=AppStyles.TEXT_DARK,
                       font=('Segoe UI', 7), relief='flat', bd=0,
                       command=lambda k=ckey, v=sv: (
                           self.update_setting(k, v.get()),
                           self._dub_touch_preview())).pack(side='left')
            sv.trace_add('write', lambda *_, k=ckey, v=sv: (
                self.update_setting(k, v.get()), self._dub_touch_preview()))

        # Blur mode controls: intensity, tint+opacity, feather.
        _slider(self._dub_rb_blur_frame, 'Intensity:', 'blur_intensity',
                1, 100, 15, lambda v: f'{int(v)}')
        tint_r = tk.Frame(self._dub_rb_blur_frame, bg=AppStyles.BG_CARD)
        tint_r.pack(fill='x', padx=0, pady=(2, 0))
        tv = tk.BooleanVar(value=self.settings.get('blur_color_tint_enabled', False))
        tk.Checkbutton(tint_r, text='Tint', variable=tv, bg=AppStyles.BG_CARD,
                       fg=AppStyles.TEXT_DARK, activebackground=AppStyles.BG_CARD,
                       selectcolor=AppStyles.BG_INPUT, font=('Segoe UI', 7),
                       command=lambda: (
                           self.update_setting('blur_color_tint_enabled', tv.get()),
                           self._dub_touch_preview())).pack(side='left')
        self._dub_rb_tint_var = tk.StringVar(
            value=self.settings.get('blur_tint_color', '#000000'))
        _sw = tk.Label(tint_r, text='  ', bg=self._dub_rb_tint_var.get(),
                       relief='solid', borderwidth=1)
        _sw.pack(side='left', padx=(2, 1))
        tk.Entry(tint_r, textvariable=self._dub_rb_tint_var, width=5,
                 bg=AppStyles.BG_INPUT, fg=AppStyles.TEXT_DARK,
                 font=('Segoe UI', 7), relief='flat').pack(side='left')

        def _pk_tint():
            c = colorchooser.askcolor(title='Blur Tint',
                                      initialcolor=self._dub_rb_tint_var.get())
            if c and c[1]:
                self._dub_rb_tint_var.set(c[1])
                _sw.config(bg=c[1])
                self.update_setting('blur_tint_color', c[1])
                self._dub_touch_preview()
        ModernButton(tint_r, text='🎨', bg_color=AppStyles.ACCENT_INFO,
                     font=('Segoe UI', 6), padx=2, pady=0,
                     command=_pk_tint).pack(side='left', padx=1)
        self._dub_rb_tint_var.trace_add('write', lambda *_: (
            self.update_setting('blur_tint_color', self._dub_rb_tint_var.get()),
            _sw.config(bg=self._dub_rb_tint_var.get()),
            self._dub_touch_preview()))
        _slider(tint_r, 'Op:', 'blur_tint_opacity', 0, 100, 50,
                lambda v: f'{int(v)}%')
        fe = tk.BooleanVar(value=self.settings.get('blur_feather_edge', True))
        tk.Checkbutton(self._dub_rb_blur_frame, text='Feather Edge', variable=fe,
                       bg=AppStyles.BG_CARD, fg=AppStyles.TEXT_DARK,
                       activebackground=AppStyles.BG_CARD,
                       selectcolor=AppStyles.BG_INPUT, font=('Segoe UI', 7),
                       command=lambda: (
                           self.update_setting('blur_feather_edge', fe.get()),
                           self._dub_touch_preview())).pack(anchor='w', pady=(2, 0))

        # Cover mode controls: color, opacity, round.
        cc_r = tk.Frame(self._dub_rb_cover_frame, bg=AppStyles.BG_CARD)
        cc_r.pack(fill='x', padx=0, pady=(2, 0))
        tk.Label(cc_r, text='Cover Color:', bg=AppStyles.BG_CARD,
                 fg=AppStyles.TEXT_DARK, font=('Segoe UI', 7, 'bold')).pack(side='left')
        self._dub_rb_cover_color_var = tk.StringVar(
            value=self.settings.get('cover_color', '#000000'))
        _cc_sw = tk.Label(cc_r, text='  ', bg=self._dub_rb_cover_color_var.get(),
                          relief='solid', borderwidth=1)
        _cc_sw.pack(side='left', padx=(2, 1))
        tk.Entry(cc_r, textvariable=self._dub_rb_cover_color_var, width=6,
                 bg=AppStyles.BG_INPUT, fg=AppStyles.TEXT_DARK,
                 font=('Segoe UI', 7), relief='flat').pack(side='left')

        def _pk_cc():
            c = colorchooser.askcolor(title='Cover Color',
                                      initialcolor=self._dub_rb_cover_color_var.get())
            if c and c[1]:
                self._dub_rb_cover_color_var.set(c[1])
                _cc_sw.config(bg=c[1])
                self.update_setting('cover_color', c[1])
                self._dub_touch_preview()
        ModernButton(cc_r, text='🎨', bg_color=AppStyles.ACCENT_INFO,
                     font=('Segoe UI', 6), padx=2, pady=0,
                     command=_pk_cc).pack(side='left', padx=1)
        self._dub_rb_cover_color_var.trace_add('write', lambda *_: (
            self.update_setting('cover_color', self._dub_rb_cover_color_var.get()),
            _cc_sw.config(bg=self._dub_rb_cover_color_var.get()),
            self._dub_touch_preview()))
        _slider(self._dub_rb_cover_frame, 'Opacity:', 'cover_opacity',
                0, 100, 85, lambda v: f'{int(v)}%')
        _slider(self._dub_rb_cover_frame, 'Round:', 'cover_radius',
                0, 30, 8, lambda v: f'{int(v)}px')

        # ── Border ──
        tk.Label(b, text='── Border ──', bg=AppStyles.BG_CARD,
                 fg=AppStyles.TEXT_MEDIUM, font=('Segoe UI', 7, 'bold')
                 ).pack(anchor='w', padx=0, pady=(2, 0))
        self._dub_rb_border_enabled_var = tk.BooleanVar(
            value=self.settings.get('cleanup_border_enabled', False))
        tk.Checkbutton(b, text='Add Border', variable=self._dub_rb_border_enabled_var,
                       bg=AppStyles.BG_CARD, fg=AppStyles.TEXT_DARK,
                       activebackground=AppStyles.BG_CARD, selectcolor=AppStyles.BG_INPUT,
                       font=('Segoe UI', 8, 'bold'),
                       command=lambda: (
                           self.update_setting('cleanup_border_enabled',
                                               self._dub_rb_border_enabled_var.get()),
                           self._dub_touch_preview())).pack(anchor='w', padx=0, pady=(1, 0))
        bc_r = tk.Frame(b, bg=AppStyles.BG_CARD)
        bc_r.pack(fill='x', padx=0, pady=(2, 0))
        tk.Label(bc_r, text='Color:', bg=AppStyles.BG_CARD,
                 fg=AppStyles.TEXT_DARK, font=('Segoe UI', 7)).pack(side='left')
        self._dub_rb_border_color_var = tk.StringVar(
            value=self.settings.get('cleanup_border_color', '#FFFFFF'))
        tk.Entry(bc_r, textvariable=self._dub_rb_border_color_var,
                 bg=AppStyles.BG_INPUT, fg=AppStyles.TEXT_DARK,
                 font=('Segoe UI', 7), width=6, relief='flat').pack(side='left', padx=(3, 1))
        self._dub_rb_border_swatch = tk.Label(
            bc_r, text='    ', bg=self._dub_rb_border_color_var.get(),
            relief='solid', borderwidth=1)
        self._dub_rb_border_swatch.pack(side='left', padx=(0, 2))

        def _pk_br():
            c = colorchooser.askcolor(title='Border Color',
                                      initialcolor=self._dub_rb_border_color_var.get())
            if c and c[1]:
                self._dub_rb_border_color_var.set(c[1])
                self._dub_rb_border_swatch.config(bg=c[1])
                self.update_setting('cleanup_border_color', c[1])
                self._dub_touch_preview()
        ModernButton(bc_r, text='🎨', bg_color=AppStyles.ACCENT_INFO,
                     font=('Segoe UI', 6), padx=2, pady=0,
                     command=_pk_br).pack(side='left')
        self._dub_rb_border_color_var.trace_add('write', lambda *_: (
            self.update_setting('cleanup_border_color',
                                self._dub_rb_border_color_var.get()),
            self._dub_rb_border_swatch.config(bg=self._dub_rb_border_color_var.get()),
            self._dub_touch_preview()))
        _slider(b, 'Size:', 'cleanup_border_size', 1, 60, 4,
                lambda v: f'{int(v)}px')

    def _dub_build_custom_blur_card(self, scrollable):
        """Custom Blur Regions — mirror of the OurScript card (same keys)."""
        card = self._dub_card(scrollable, '🎯 Custom Blur Regions (Hide Logos)')
        b = tk.Frame(card, bg=AppStyles.BG_CARD)
        b.pack(fill='x', padx=8, pady=(2, 8))

        self._dub_custom_blur_var = tk.BooleanVar(
            value=bool(self.settings.get('dub_custom_blur', False)))
        tk.Checkbutton(b, text='Apply Custom Blur Regions to the dubbed video',
                       variable=self._dub_custom_blur_var,
                       bg=AppStyles.BG_CARD, fg=AppStyles.TEXT_DARK,
                       activebackground=AppStyles.BG_CARD,
                       selectcolor=AppStyles.BG_INPUT, font=('Segoe UI', 8, 'bold'),
                       command=lambda: self.update_setting(
                           'dub_custom_blur', self._dub_custom_blur_var.get())
                       ).pack(anchor='w', pady=(0, 2))

        custom = self.settings.get('custom_blur_regions', []) or []
        self._dub_cb_stat = tk.Label(
            b, text='', bg=AppStyles.BG_CARD, fg=AppStyles.TEXT_MEDIUM,
            font=('Segoe UI', 8))
        self._dub_cb_stat.pack(anchor='w')
        self._dub_cb_list_frame = tk.Frame(b, bg=AppStyles.BG_CARD)
        self._dub_cb_list_frame.pack(fill='x', padx=2)

        btns = tk.Frame(b, bg=AppStyles.BG_CARD)
        btns.pack(fill='x', pady=(2, 0))
        ModernButton(btns, text='+ Add', bg_color=AppStyles.BG_INPUT,
                     font=('Segoe UI', 8, 'bold'), padx=4, pady=1,
                     command=lambda: self._dub_cb_caption_dialog(
                         self._dub_cb_stat)).pack(side='left', padx=(0, 2))
        ModernButton(btns, text='Blur Mid', bg_color='#d69e2e',
                     font=('Segoe UI', 8, 'bold'), padx=4, pady=1,
                     command=lambda: self._os_bb_add_middle_region(
                         self._dub_cb_stat, self._dub_cb_stat,
                         '_dub_cb', self._dub_touch_preview)).pack(side='left', padx=(0, 2))
        ModernButton(btns, text='Inpaint ▼', bg_color='#22c55e',
                     font=('Segoe UI', 8, 'bold'), padx=4, pady=1,
                     command=lambda: self._os_bb_add_inpaint_region(
                         self._dub_cb_stat, self._dub_cb_stat,
                         '_dub_cb', self._dub_touch_preview)).pack(side='left', padx=(0, 2))
        ModernButton(btns, text='Ref', bg_color=AppStyles.ACCENT_INFO,
                     font=('Segoe UI', 7), padx=4, pady=1,
                     command=lambda: self._os_bb_rebuild_cb_list(
                         self._dub_cb_stat, self._dub_cb_stat,
                         '_dub_cb', self._dub_touch_preview)).pack(side='left')

        # X/Y/W/H sliders.
        self._dub_cb_x_var = tk.IntVar(value=5)
        self._dub_cb_y_var = tk.IntVar(value=35)
        self._dub_cb_w_var = tk.IntVar(value=90)
        self._dub_cb_h_var = tk.IntVar(value=22)
        for row_data in [
                [('→ X', 'x', self._dub_cb_x_var, 0, 95),
                 ('⬇ Y', 'y', self._dub_cb_y_var, 0, 75)],
                [('↔ W', 'width', self._dub_cb_w_var, 20, 100),
                 ('≡ H', 'height', self._dub_cb_h_var, 5, 40)]]:
            r = tk.Frame(b, bg=AppStyles.BG_CARD)
            r.pack(fill='x', padx=1, pady=0)
            for label, key, var, lo, hi in row_data:
                sr = tk.Frame(r, bg=AppStyles.BG_CARD)
                sr.pack(side='left', fill='x', expand=True, padx=0)
                tk.Label(sr, text=label, bg=AppStyles.BG_CARD,
                         fg=AppStyles.TEXT_DARK, font=('Segoe UI', 6, 'bold')).pack(anchor='w')
                tk.Scale(sr, from_=lo, to=hi, resolution=1, orient='horizontal',
                         variable=var, bg=AppStyles.BG_CARD, fg=AppStyles.TEXT_DARK,
                         troughcolor=AppStyles.BG_INPUT, length=35, sliderlength=8,
                         font=('Segoe UI', 5)).pack(fill='x')
                var.trace_add('write', lambda *_, k=key, v=var: (
                    self._os_bb_update_middle_region(
                        k, v.get(), self._dub_touch_preview)))

        # Mode: blur / inpaint.
        mode_r = tk.Frame(b, bg=AppStyles.BG_CARD)
        mode_r.pack(fill='x', padx=2, pady=(2, 0))
        tk.Label(mode_r, text='Mode:', bg=AppStyles.BG_CARD,
                 fg=AppStyles.TEXT_DARK, font=('Segoe UI', 8, 'bold')).pack(side='left')
        self._dub_cb_mode_var = tk.StringVar(value='blur')
        mc = ttk.Combobox(mode_r, textvariable=self._dub_cb_mode_var,
                          values=['blur', 'inpaint'], state='readonly', width=8)
        mc.pack(side='left', padx=(4, 0))
        mc.bind('<<ComboboxSelected>>', lambda e: (
            self._os_bb_update_middle_region(
                'mode', self._dub_cb_mode_var.get(), self._dub_touch_preview)))
        tk.Label(mode_r, text='Blur=hide  Inpaint=AI remove',
                 bg=AppStyles.BG_CARD, fg=AppStyles.TEXT_MEDIUM,
                 font=('Segoe UI', 5, 'italic')).pack(side='right')

        # Fill color.
        fc_r = tk.Frame(b, bg=AppStyles.BG_CARD)
        fc_r.pack(fill='x', padx=2, pady=(2, 0))
        tk.Label(fc_r, text='Fill:', bg=AppStyles.BG_CARD,
                 fg=AppStyles.TEXT_DARK, font=('Segoe UI', 8, 'bold')).pack(side='left')
        self._dub_cb_fill_color_var = tk.StringVar(value='#000000')
        _fc_sw = tk.Label(fc_r, text='  ', bg=self._dub_cb_fill_color_var.get(),
                          relief='solid', borderwidth=1, width=2)
        _fc_sw.pack(side='left', padx=(2, 1))
        tk.Entry(fc_r, textvariable=self._dub_cb_fill_color_var, width=6,
                 bg=AppStyles.BG_INPUT, fg=AppStyles.TEXT_DARK,
                 font=('Segoe UI', 7), relief='flat').pack(side='left')

        def _pk_fc():
            c = colorchooser.askcolor(title='Fill Color',
                                      initialcolor=self._dub_cb_fill_color_var.get())
            if c and c[1]:
                self._dub_cb_fill_color_var.set(c[1])
                _fc_sw.config(bg=c[1])
                self._os_bb_update_middle_color(c[1], self._dub_touch_preview)
        ModernButton(fc_r, text='🎨', bg_color=AppStyles.ACCENT_INFO,
                     font=('Segoe UI', 6), padx=2, pady=0,
                     command=_pk_fc).pack(side='left', padx=1)
        self._dub_cb_fill_color_var.trace_add('write', lambda *_: (
            self._os_bb_update_middle_color(
                self._dub_cb_fill_color_var.get(), self._dub_touch_preview),
            _fc_sw.config(bg=self._dub_cb_fill_color_var.get())))

        # Fill opacity.
        fo_r = tk.Frame(b, bg=AppStyles.BG_CARD)
        fo_r.pack(fill='x', padx=2, pady=(0, 1))
        tk.Label(fo_r, text='Opacity:', bg=AppStyles.BG_CARD,
                 fg=AppStyles.TEXT_DARK, font=('Segoe UI', 8, 'bold')).pack(side='left')
        self._dub_cb_fill_opacity_var = tk.IntVar(value=0)
        fo_v = tk.Label(fo_r, text='80%', bg=AppStyles.BG_CARD,
                        fg=AppStyles.ACCENT_PRIMARY, font=('Segoe UI', 7, 'bold'), width=3)
        fo_v.pack(side='right')
        tk.Scale(fo_r, from_=0, to=100, orient='horizontal',
                 variable=self._dub_cb_fill_opacity_var,
                 bg=AppStyles.BG_CARD, fg=AppStyles.TEXT_DARK,
                 highlightthickness=0, troughcolor=AppStyles.BG_INPUT,
                 showvalue=False, sliderlength=8, length=35,
                 command=lambda v, lb=fo_v: (
                     self._os_bb_update_middle_region(
                         'fill_opacity', int(float(v)), self._dub_touch_preview),
                     lb.config(text=f'{int(float(v))}%')
                 )).pack(side='left', fill='x', expand=True, padx=2)

        # Pill (cover) toggle + round slider.
        cov_r = tk.Frame(b, bg=AppStyles.BG_CARD)
        cov_r.pack(fill='x', padx=2)
        self._dub_cb_cover_mode_var = tk.BooleanVar(value=False)
        tk.Checkbutton(cov_r, text='Pill (cover)', variable=self._dub_cb_cover_mode_var,
                       bg=AppStyles.BG_CARD, fg=AppStyles.TEXT_DARK,
                       activebackground=AppStyles.BG_CARD,
                       selectcolor=AppStyles.BG_INPUT, font=('Segoe UI', 7),
                       command=lambda: self._os_bb_update_middle_region(
                           'cover_mode', self._dub_cb_cover_mode_var.get(),
                           self._dub_touch_preview)).pack(side='left')
        cr_r = tk.Frame(b, bg=AppStyles.BG_CARD)
        cr_r.pack(fill='x', padx=2)
        tk.Label(cr_r, text='Round:', bg=AppStyles.BG_CARD,
                 fg=AppStyles.TEXT_DARK, font=('Segoe UI', 8, 'bold')).pack(side='left')
        self._dub_cb_cover_radius_var = tk.IntVar(value=8)
        cr_v = tk.Label(cr_r, text='8 px', bg=AppStyles.BG_CARD,
                        fg=AppStyles.ACCENT_PRIMARY, font=('Segoe UI', 7, 'bold'), width=3)
        cr_v.pack(side='right')
        tk.Scale(cr_r, from_=0, to=30, orient='horizontal',
                 variable=self._dub_cb_cover_radius_var,
                 bg=AppStyles.BG_CARD, fg=AppStyles.TEXT_DARK,
                 highlightthickness=0, troughcolor=AppStyles.BG_INPUT,
                 showvalue=False, sliderlength=8, length=35,
                 command=lambda v, lb=cr_v: (
                     self._os_bb_update_middle_region(
                         'cover_radius', int(float(v)), self._dub_touch_preview),
                     lb.config(text=f'{int(float(v))} px')
                 )).pack(side='left', fill='x', expand=True, padx=2)

        # Build the region list + count.
        self._os_bb_rebuild_cb_list(self._dub_cb_stat, self._dub_cb_stat,
                                    '_dub_cb', self._dub_touch_preview)

    def _dub_build_title_card(self, scrollable):
        """Title Text — mirror of the OurScript card (same keys)."""
        card = self._dub_card(scrollable, '🅣 Title Text')
        b = tk.Frame(card, bg=AppStyles.BG_CARD)
        b.pack(fill='x', padx=8, pady=(2, 8))

        self._dub_title_text_var = tk.BooleanVar(
            value=bool(self.settings.get('dub_title_text', False)))
        tk.Checkbutton(b, text='Apply Title Text to the dubbed video',
                       variable=self._dub_title_text_var,
                       bg=AppStyles.BG_CARD, fg=AppStyles.TEXT_DARK,
                       activebackground=AppStyles.BG_CARD,
                       selectcolor=AppStyles.BG_INPUT, font=('Segoe UI', 8, 'bold'),
                       command=lambda: self.update_setting(
                           'dub_title_text', self._dub_title_text_var.get())
                       ).pack(anchor='w', pady=(0, 2))

        self._dub_title_text_widget = tk.Text(
            b, height=2, wrap='word', bg=AppStyles.BG_INPUT, fg=AppStyles.TEXT_DARK,
            font=('Segoe UI', 9), relief='flat')
        self._dub_title_text_widget.pack(fill='x', pady=(0, 3))
        _t = self.settings.get('our_script_title_text', '') or ''
        if _t:
            self._dub_title_text_widget.insert('1.0', _t)
        self._dub_title_text_widget.bind(
            '<KeyRelease>',
            lambda e: self.update_setting(
                'our_script_title_text',
                self._dub_title_text_widget.get('1.0', 'end-1c')))

        grid = tk.Frame(b, bg=AppStyles.BG_CARD)
        grid.pack(fill='x')
        grid.columnconfigure(0, weight=1, uniform='dub_tt')
        grid.columnconfigure(1, weight=1, uniform='dub_tt')
        lcol = tk.Frame(grid, bg=AppStyles.BG_CARD)
        lcol.grid(row=0, column=0, sticky='nsew', padx=(0, 3))
        rcol = tk.Frame(grid, bg=AppStyles.BG_CARD)
        rcol.grid(row=0, column=1, sticky='nsew', padx=(3, 0))

        def _color_row(parent, label, key, default, pick_title):
            row = tk.Frame(parent, bg=AppStyles.BG_CARD)
            row.pack(fill='x', pady=1)
            tk.Label(row, text=label, bg=AppStyles.BG_CARD,
                     fg=AppStyles.TEXT_DARK, font=('Segoe UI', 8),
                     width=7, anchor='w').pack(side='left')
            var = tk.StringVar(value=self.settings.get(key, default))
            sw = tk.Label(row, text='     ', bg=var.get(), relief='solid', borderwidth=1)
            sw.pack(side='left', padx=(2, 1))
            tk.Entry(row, textvariable=var, bg=AppStyles.BG_INPUT,
                     fg=AppStyles.TEXT_DARK, font=('Segoe UI', 8),
                     width=7, relief='flat').pack(side='left', padx=(2, 1))

            def _pick():
                c = colorchooser.askcolor(title=pick_title, initialcolor=var.get())
                if c and c[1]:
                    var.set(c[1])
                    sw.config(bg=c[1])
                    self.update_setting(key, c[1])
            ModernButton(row, text='🎨', bg_color=AppStyles.ACCENT_INFO,
                         font=('Segoe UI', 8), padx=4, pady=1,
                         command=_pick).pack(side='left')
            return var, sw
        tc_var, tc_sw = _color_row(lcol, 'Text:', 'our_script_title_text_color',
                                   '#FFFFFF', 'Choose Title Text Color')
        bg_var, bg_sw = _color_row(lcol, 'BG:', 'our_script_title_bg_color',
                                   '#000000', 'Choose Title Background Color')

        # Position.
        pos_r = tk.Frame(lcol, bg=AppStyles.BG_CARD)
        pos_r.pack(fill='x', pady=1)
        tk.Label(pos_r, text='Position:', bg=AppStyles.BG_CARD,
                 fg=AppStyles.TEXT_DARK, font=('Segoe UI', 8),
                 width=7, anchor='w').pack(side='left')
        self._dub_title_pos_var = tk.StringVar(
            value=self.settings.get('our_script_title_position', 'top'))
        pc = ttk.Combobox(pos_r, textvariable=self._dub_title_pos_var,
                          values=['top', 'center', 'bottom'], state='readonly', width=8)
        pc.pack(side='left', padx=(2, 0))
        pc.bind('<<ComboboxSelected>>', lambda e: self.update_setting(
            'our_script_title_position', self._dub_title_pos_var.get()))

        # V-Offset.
        voff_r = tk.Frame(lcol, bg=AppStyles.BG_CARD)
        voff_r.pack(fill='x', pady=1)
        tk.Label(voff_r, text='V-Offset:', bg=AppStyles.BG_CARD,
                 fg=AppStyles.TEXT_DARK, font=('Segoe UI', 8),
                 width=7, anchor='w').pack(side='left')
        self._dub_title_vo_var = tk.IntVar(
            value=int(self.settings.get('vertical_offset', 0)))
        voff_l = tk.Label(voff_r, text='0px', bg=AppStyles.BG_CARD,
                          fg=AppStyles.ACCENT_PRIMARY, font=('Segoe UI', 8, 'bold'),
                          width=4, anchor='e')
        voff_l.pack(side='right')
        tk.Scale(voff_r, from_=-200, to=200, orient='horizontal',
                 variable=self._dub_title_vo_var, length=55,
                 bg=AppStyles.BG_CARD, fg=AppStyles.TEXT_DARK,
                 troughcolor=AppStyles.BG_INPUT, highlightthickness=0,
                 command=lambda v, lb=voff_l: (
                     self.update_setting('vertical_offset', int(v)),
                     lb.config(text=f"{'+' if int(v) >= 0 else ''}{int(v)}px"),
                     self._dub_touch_preview())
                 ).pack(side='left', fill='x', expand=True, padx=(0, 2))

        # Size.
        fs_r = tk.Frame(rcol, bg=AppStyles.BG_CARD)
        fs_r.pack(fill='x', pady=1)
        tk.Label(fs_r, text='Size:', bg=AppStyles.BG_CARD,
                 fg=AppStyles.TEXT_DARK, font=('Segoe UI', 8),
                 width=7, anchor='w').pack(side='left')
        self._dub_title_fs_var = tk.IntVar(
            value=int(self.settings.get('our_script_title_font_size', 70)))
        fs_l = tk.Label(fs_r, text=str(self._dub_title_fs_var.get()),
                        bg=AppStyles.BG_CARD, fg=AppStyles.TEXT_DARK,
                        font=('Segoe UI', 8, 'bold'))
        fs_l.pack(side='right', padx=(4, 0))
        tk.Scale(fs_r, from_=12, to=200, orient='horizontal',
                 variable=self._dub_title_fs_var, length=55,
                 bg=AppStyles.BG_CARD, fg=AppStyles.TEXT_DARK,
                 troughcolor=AppStyles.BG_INPUT, highlightthickness=0,
                 command=lambda v, lb=fs_l: (
                     self.update_setting('our_script_title_font_size', int(v)),
                     lb.config(text=str(int(v))),
                     self._dub_touch_preview())
                 ).pack(side='left', padx=(2, 0))

        # BG opacity.
        op_r = tk.Frame(rcol, bg=AppStyles.BG_CARD)
        op_r.pack(fill='x', pady=1)
        tk.Label(op_r, text='BG op:', bg=AppStyles.BG_CARD,
                 fg=AppStyles.TEXT_DARK, font=('Segoe UI', 8),
                 width=7, anchor='w').pack(side='left')
        self._dub_title_op_var = tk.IntVar(
            value=int(self.settings.get('our_script_title_bg_opacity', 80)))
        op_l = tk.Label(op_r, text=f'{self._dub_title_op_var.get()}%',
                        bg=AppStyles.BG_CARD, fg=AppStyles.TEXT_DARK,
                        font=('Segoe UI', 8, 'bold'))
        op_l.pack(side='right', padx=(4, 0))
        tk.Scale(op_r, from_=0, to=100, orient='horizontal',
                 variable=self._dub_title_op_var, length=55,
                 bg=AppStyles.BG_CARD, fg=AppStyles.TEXT_DARK,
                 troughcolor=AppStyles.BG_INPUT, highlightthickness=0,
                 command=lambda v, lb=op_l: (
                     self.update_setting('our_script_title_bg_opacity', int(v)),
                     lb.config(text=f'{int(v)}%'),
                     self._dub_touch_preview())
                 ).pack(side='left', padx=(2, 0))

        # Round corners.
        rd_r = tk.Frame(rcol, bg=AppStyles.BG_CARD)
        rd_r.pack(fill='x', pady=1)
        tk.Label(rd_r, text='Round:', bg=AppStyles.BG_CARD,
                 fg=AppStyles.TEXT_DARK, font=('Segoe UI', 8),
                 width=7, anchor='w').pack(side='left')
        self._dub_title_radius_var = tk.IntVar(
            value=int(self.settings.get('our_script_title_bg_radius', 12)))
        rd_l = tk.Label(rd_r, text=f'{self._dub_title_radius_var.get()}px',
                        bg=AppStyles.BG_CARD, fg=AppStyles.TEXT_DARK,
                        font=('Segoe UI', 8, 'bold'))
        rd_l.pack(side='right', padx=(4, 0))
        tk.Scale(rd_r, from_=0, to=50, orient='horizontal',
                 variable=self._dub_title_radius_var, length=55,
                 bg=AppStyles.BG_CARD, fg=AppStyles.TEXT_DARK,
                 troughcolor=AppStyles.BG_INPUT, highlightthickness=0,
                 command=lambda v, lb=rd_l: (
                     self.update_setting('our_script_title_bg_radius', int(v)),
                     lb.config(text=f'{int(v)}px'),
                     self._dub_touch_preview())
                 ).pack(side='left', padx=(2, 0))

        tk.Label(b, text='   Same shared settings as the OurScript > Title Text card.',
                 bg=AppStyles.BG_CARD, fg=AppStyles.TEXT_MEDIUM,
                 font=('Segoe UI', 8, 'italic'), wraplength=520,
                 justify='left').pack(anchor='w', pady=(2, 0))

    def _dub_build_bottom_text_card(self, scrollable):
        """Bottom Text CTA — mirror of the OurScript card (same keys)."""
        card = self._dub_card(scrollable, '📄 Bottom Text (CTA at bottom)')
        b = tk.Frame(card, bg=AppStyles.BG_CARD)
        b.pack(fill='x', padx=8, pady=(2, 8))

        self._dub_bottom_text_var = tk.BooleanVar(
            value=bool(self.settings.get('dub_bottom_text', False)))
        tk.Checkbutton(b, text='Apply Bottom Text CTA to the dubbed video',
                       variable=self._dub_bottom_text_var,
                       bg=AppStyles.BG_CARD, fg=AppStyles.TEXT_DARK,
                       activebackground=AppStyles.BG_CARD,
                       selectcolor=AppStyles.BG_INPUT, font=('Segoe UI', 8, 'bold'),
                       command=lambda: self.update_setting(
                           'dub_bottom_text', self._dub_bottom_text_var.get())
                       ).pack(anchor='w', pady=(0, 2))

        self._dub_bottom_text_widget = tk.Text(
            b, height=2, wrap='word', bg=AppStyles.BG_INPUT, fg=AppStyles.TEXT_DARK,
            font=('Segoe UI', 9), relief='flat')
        self._dub_bottom_text_widget.pack(fill='x', pady=(0, 3))
        _bt = self.settings.get('bottom_text_content', '') or ''
        if _bt:
            self._dub_bottom_text_widget.insert('1.0', _bt)
        self._dub_bottom_text_widget.bind(
            '<KeyRelease>',
            lambda e: self.update_setting(
                'bottom_text_content',
                self._dub_bottom_text_widget.get('1.0', 'end-1c')))

        row = tk.Frame(b, bg=AppStyles.BG_CARD)
        row.pack(fill='x', pady=(2, 0))

        # Font family.
        tk.Label(row, text='Font:', bg=AppStyles.BG_CARD,
                 fg=AppStyles.TEXT_DARK, font=('Segoe UI', 8)).pack(side='left')
        self._dub_bt_font_var = tk.StringVar(
            value=self.settings.get('bottom_text_font_family', 'Arial'))
        fonts = self.get_system_fonts('bottom_text') if hasattr(
            self, 'get_system_fonts') else ['Arial']
        fc = ttk.Combobox(row, textvariable=self._dub_bt_font_var,
                          values=fonts, state='readonly', width=12)
        fc.pack(side='left', padx=(2, 6))
        fc.bind('<<ComboboxSelected>>', lambda e: self.update_setting(
            'bottom_text_font_family', self._dub_bt_font_var.get()))

        # Size.
        tk.Label(row, text='Size:', bg=AppStyles.BG_CARD,
                 fg=AppStyles.TEXT_DARK, font=('Segoe UI', 8)).pack(side='left')
        self._dub_bt_fs_var = tk.IntVar(
            value=int(self.settings.get('bottom_text_font_size', 45)))
        fs_l = tk.Label(row, text=str(self._dub_bt_fs_var.get()),
                        bg=AppStyles.BG_CARD, fg=AppStyles.ACCENT_PRIMARY,
                        font=('Segoe UI', 8, 'bold'))
        fs_l.pack(side='left', padx=(2, 0))
        tk.Scale(row, from_=12, to=200, orient='horizontal',
                 variable=self._dub_bt_fs_var, length=45,
                 bg=AppStyles.BG_CARD, fg=AppStyles.TEXT_DARK,
                 troughcolor=AppStyles.BG_INPUT, highlightthickness=0,
                 command=lambda v, lb=fs_l: (
                     self.update_setting('bottom_text_font_size', int(v)),
                     lb.config(text=str(int(v))),
                     self._dub_touch_preview())
                 ).pack(side='left', padx=(2, 4))

        # Text color.
        tk.Label(row, text='Color:', bg=AppStyles.BG_CARD,
                 fg=AppStyles.TEXT_DARK, font=('Segoe UI', 8)).pack(side='left')
        self._dub_bt_tc_var = tk.StringVar(
            value=self.settings.get('bottom_text_text_color', '#FFFFFF'))
        self._dub_bt_tc_swatch = tk.Frame(row, bg=self._dub_bt_tc_var.get(),
                                          width=16, height=14, relief='solid',
                                          borderwidth=1)
        self._dub_bt_tc_swatch.pack(side='left', padx=(2, 2))

        def _pick_tc():
            c = colorchooser.askcolor(title='Bottom Text Color',
                                      initialcolor=self._dub_bt_tc_var.get())
            if c and c[1]:
                self._dub_bt_tc_var.set(c[1])
                self._dub_bt_tc_swatch.config(bg=c[1])
                self.update_setting('bottom_text_text_color', c[1])
        ModernButton(row, text='🎨', bg_color=AppStyles.ACCENT_INFO,
                     font=('Segoe UI', 8), padx=4, pady=0,
                     command=_pick_tc).pack(side='left', padx=(0, 6))

        # BG color.
        tk.Label(row, text='BG:', bg=AppStyles.BG_CARD,
                 fg=AppStyles.TEXT_DARK, font=('Segoe UI', 8)).pack(side='left')
        self._dub_bt_bgc_var = tk.StringVar(
            value=self.settings.get('bottom_text_bg_color', '#000000'))
        self._dub_bt_bgc_swatch = tk.Frame(row, bg=self._dub_bt_bgc_var.get(),
                                           width=16, height=14, relief='solid',
                                           borderwidth=1)
        self._dub_bt_bgc_swatch.pack(side='left', padx=(2, 2))

        def _pick_bgc():
            c = colorchooser.askcolor(title='Bottom Text BG Color',
                                      initialcolor=self._dub_bt_bgc_var.get())
            if c and c[1]:
                self._dub_bt_bgc_var.set(c[1])
                self._dub_bt_bgc_swatch.config(bg=c[1])
                self.update_setting('bottom_text_bg_color', c[1])
        ModernButton(row, text='🎨', bg_color=AppStyles.ACCENT_INFO,
                     font=('Segoe UI', 8), padx=4, pady=0,
                     command=_pick_bgc).pack(side='left', padx=(0, 6))

        # BG opacity.
        tk.Label(row, text='Opacity:', bg=AppStyles.BG_CARD,
                 fg=AppStyles.TEXT_DARK, font=('Segoe UI', 8)).pack(side='left')
        self._dub_bt_op_var = tk.IntVar(
            value=int(self.settings.get('bottom_text_bg_opacity', 80)))
        op_l = tk.Label(row, text=f'{self._dub_bt_op_var.get()}%',
                        bg=AppStyles.BG_CARD, fg=AppStyles.ACCENT_PRIMARY,
                        font=('Segoe UI', 8, 'bold'))
        op_l.pack(side='left', padx=(2, 0))
        tk.Scale(row, from_=0, to=100, orient='horizontal',
                 variable=self._dub_bt_op_var, length=40,
                 bg=AppStyles.BG_CARD, fg=AppStyles.TEXT_DARK,
                 troughcolor=AppStyles.BG_INPUT, highlightthickness=0,
                 command=lambda v, lb=op_l: (
                     self.update_setting('bottom_text_bg_opacity', int(v)),
                     lb.config(text=f'{int(v)}%'),
                     self._dub_touch_preview())
                 ).pack(side='left', padx=(2, 0))

        # V-Offset.
        voff_r = tk.Frame(b, bg=AppStyles.BG_CARD)
        voff_r.pack(fill='x', pady=(2, 0))
        tk.Label(voff_r, text='V-Offset:', bg=AppStyles.BG_CARD,
                 fg=AppStyles.TEXT_DARK, font=('Segoe UI', 8),
                 width=7, anchor='w').pack(side='left')
        self._dub_bt_vo_var = tk.IntVar(
            value=int(self.settings.get('bottom_text_vertical_offset', 0)))
        voff_l = tk.Label(voff_r, text='0px', bg=AppStyles.BG_CARD,
                          fg=AppStyles.ACCENT_PRIMARY, font=('Segoe UI', 8, 'bold'),
                          width=4, anchor='e')
        voff_l.pack(side='right')
        tk.Scale(voff_r, from_=-200, to=200, orient='horizontal',
                 variable=self._dub_bt_vo_var, length=55,
                 bg=AppStyles.BG_CARD, fg=AppStyles.TEXT_DARK,
                 troughcolor=AppStyles.BG_INPUT, highlightthickness=0,
                 command=lambda v, lb=voff_l: (
                     self.update_setting('bottom_text_vertical_offset', int(v)),
                     lb.config(text=f"{'+' if int(v) >= 0 else ''}{int(v)}px"),
                     self._dub_touch_preview())
                 ).pack(side='left', fill='x', expand=True, padx=(0, 2))

        tk.Label(b, text='   Same shared settings as the OurScript > Bottom Text card.',
                 bg=AppStyles.BG_CARD, fg=AppStyles.TEXT_MEDIUM,
                 font=('Segoe UI', 8, 'italic'), wraplength=520,
                 justify='left').pack(anchor='w', pady=(2, 0))

    def _dub_refresh_am_status(self):
        """Show which Alight Motion template is currently selected (it is
        driven from the Quick Process > Alight Motion Look Builder card)."""
        if not hasattr(self, '_dub_am_status_var'):
            return
        template = self.settings.get('am_template', 'None')
        if not template or template == 'None':
            self._dub_am_status_var.set(
                'No Alight Motion template selected. Pick one in the Quick '
                'Process tab.')
        else:
            self._dub_am_status_var.set('Active template: ' + template)

    _DUB_TRANSITION_NAMES = [
        ('transition_fade_in', 'Fade In'),
        ('transition_fade_out', 'Fade Out'),
        ('transition_zoom_in', 'Zoom In'),
        ('transition_zoom_out', 'Zoom Out'),
        ('transition_blur_in', 'Blur In'),
        ('transition_blur_out', 'Blur Out'),
        ('transition_slide_in', 'Slide In'),
        ('transition_slide_out', 'Slide Out'),
        ('transition_wipe_in', 'Wipe In'),
        ('transition_wipe_out', 'Wipe Out'),
        ('transition_glitch_start', 'Glitch Start'),
        ('transition_glitch_end', 'Glitch End'),
        ('transition_cinematic_bars', 'Cinematic Bars'),
        ('lens_flare_enabled', 'Lens Flare'),
        ('light_leak_enabled', 'Light Leak'),
        ('film_burn_enabled', 'Film Burn'),
        ('transition_bounce', 'Bounce'),
        ('transition_mask', 'Mask Reveal'),
        ('transition_bounce_mask', 'Bounce+Mask'),
        ('transition_radial_wipe', 'Radial Wipe'),
        ('transition_color_dissolve', 'Color Dissolve'),
        ('transition_split_wipe', 'Split Wipe'),
        ('transition_luma_wipe', 'Luma Wipe'),
    ]

    def _dub_refresh_trans_summary(self):
        """List the transitions currently enabled in the Transitions tab."""
        if not hasattr(self, '_dub_trans_summary_var'):
            return
        enabled = [name for k, name in self._DUB_TRANSITION_NAMES
                   if self.settings.get(k, False)]
        if not enabled:
            self._dub_trans_summary_var.set(
                'No transitions are currently enabled in the Transitions tab.')
        else:
            self._dub_trans_summary_var.set(
                'Enabled transitions that will run during Dubbing: '
                + ' | '.join(enabled) + '.')

    def _dub_slider(self, parent, label, var, key):
        row = tk.Frame(parent, bg=AppStyles.BG_CARD)
        row.pack(fill='x', pady=1)
        tk.Label(row, text=label, bg=AppStyles.BG_CARD, fg=AppStyles.TEXT_DARK,
                 font=('Segoe UI', 8), width=18, anchor='w').pack(side='left')
        val_lbl = tk.Label(row, text=f'{var.get():.0%}', bg=AppStyles.BG_CARD,
                           fg=AppStyles.ACCENT_PRIMARY, font=('Segoe UI', 8),
                           width=5)
        val_lbl.pack(side='right')

        def _on_move(v):
            fv = float(v)
            val_lbl.config(text=f'{fv:.0%}')
            self.update_setting(key, round(fv, 3))
        ttk.Scale(row, from_=0.0, to=1.0, variable=var, orient='horizontal',
                  command=_on_move).pack(side='left', fill='x', expand=True,
                                         padx=6)

    # ── Multi-speaker: detect + voice mapping ───────────────────────────
    def _dub_extra_voices(self):
        """(key, label, gender) for the non-Gemini engines: Edge, Kokoro, Piper.

        Keys are 'edge:<id>' / 'kokoro:<id>' / 'piper:<id>' so
        dubbing_engine._resolve_voice_settings() can route each to the right
        TTS engine.  Curated (see the module-level lists) to keep the
        per-speaker dropdown usable.  Memoized on first call.
        """
        if getattr(self, '_dub_extra_cache', None) is not None:
            return self._dub_extra_cache
        out = []
        # Edge (edge-tts, online) — curated multilingual set.
        for vid, g in _DUB_EDGE_VOICES:
            out.append((f'edge:{vid}', f'Edge · {vid} ({g})', g))
        # Kokoro (local, offline) — reuse the 🗣 TTS tab's list when built.
        kv = getattr(self, 'kokoro_voices', None) or _DUB_KOKORO_FALLBACK
        for disp in kv:
            vid = disp.split(' - ')[0].strip()
            g = 'Female' if 'Female' in disp else ('Male' if 'Male' in disp else '')
            out.append((f'kokoro:{vid}', f'Kokoro · {disp}', g))
        # Piper (local) — only voices already downloaded (an undownloaded one
        # would fail mid-dub).  Gender unknown → left blank.
        try:
            import piper_tts_helper
            for vid in piper_tts_helper.get_downloaded_voices():
                out.append((f'piper:{vid}', f'Piper · {vid}', ''))
        except Exception:
            pass
        self._dub_extra_cache = out
        return out

    def _dub_extra_meta(self, key):
        """(label, gender) for an extra-engine key, or None if it isn't one."""
        for k, lbl, g in self._dub_extra_voices():
            if k == key:
                return lbl, g
        return None

    def _dub_voice_gender(self, key):
        """'Male' / 'Female' / '' for any voice key (all engines)."""
        meta = self._dub_extra_meta(key)
        if meta:
            return meta[1]
        if str(key).startswith('Child '):
            return ''
        try:
            from gemini_api_tts_helper import get_voice_gender
            return get_voice_gender(key) or ''
        except Exception:
            return ''

    def _dub_voice_keys(self):
        """Voice keys for the speaker dropdowns: Gemini + child presets + the
        non-Gemini engines (Edge / Kokoro / Piper).

        Gemini keys stay bare (backward-compatible with saved mappings); the
        child-voice presets let a speaker be a boy/girl; the extra engines are
        appended so a speaker can be voiced by Edge, Kokoro or Piper too.
        """
        keys = None
        try:
            from gemini_api_tts_helper import get_voice_keys
            keys = get_voice_keys()
        except Exception:
            keys = None
        if not keys:
            keys = ['Zephyr', 'Puck', 'Charon', 'Kore', 'Fenrir', 'Aoede']
        try:
            from dubbing_engine import CHILD_VOICE_PRESETS
            child = list(CHILD_VOICE_PRESETS.keys())
        except Exception:
            child = ['Child girl (Gemini)', 'Child boy (Gemini)',
                     'Child girl (Edge)', 'Child boy (Edge)']
        extra = [k for (k, _lbl, _g) in self._dub_extra_voices()]
        return list(keys) + child + extra

    def _dub_voice_label(self, key):
        """'Puck' → 'Puck (Male) — upbeat · ads/reactions' for display.

        Appends gender + a best-use hint so voices aren't picked blind.  Falls
        back to bare key if unknown.  Child presets already read naturally
        (e.g. 'Child girl (Gemini)'), so they are shown as-is.  Extra-engine
        keys (Edge/Kokoro/Piper) carry their own pre-built label.
        """
        meta = self._dub_extra_meta(key)
        if meta:
            return meta[0]
        if str(key).startswith('Child '):
            return str(key)
        try:
            from gemini_api_tts_helper import get_voice_gender, get_voice_use
            g = get_voice_gender(key)
            use = get_voice_use(key)
            base = f'{key} ({g})' if g else str(key)
            return f'{base} — {use}' if use else base
        except Exception:
            return key

    def _dub_build_speaker_rows(self, speakers, genders=None):
        """Render one label + voice Combobox per detected speaker.

        ``genders`` maps speaker→'Male'/'Female' (from pitch analysis).  The
        default voice is picked to MATCH that gender when known, cycling within
        the matching-gender voices so two same-gender speakers still differ.
        Any voice already saved for a speaker is preserved.  Every change
        persists the whole mapping to ``settings['dub_speaker_voices']``.
        """
        genders = genders or {}
        # Clear previous rows
        for child in list(self._dub_speaker_rows.winfo_children()):
            child.destroy()
        self._dub_speaker_vars = {}

        voice_keys = self._dub_voice_keys()
        saved = self.settings.get('dub_speaker_voices') or {}
        self._dub_speaker_genders = genders

        # Group voice keys by gender so defaults can be gender-matched.
        by_gender = {'Male': [], 'Female': []}
        for k in voice_keys:
            g = self._dub_voice_gender(k)
            if g in by_gender:
                by_gender[g].append(k)

        # Display labels carry the gender, e.g. "Puck (Male)"; map back to keys.
        display_values = [self._dub_voice_label(k) for k in voice_keys]
        self._dub_label_to_key = {self._dub_voice_label(k): k for k in voice_keys}

        _gender_counts = {'Male': 0, 'Female': 0}
        for idx, spk in enumerate(speakers):
            row = tk.Frame(self._dub_speaker_rows, bg=AppStyles.BG_CARD)
            row.pack(fill='x', pady=2)
            g = genders.get(spk, '')
            spk_label = f'{spk} ({g}):' if g else f'{spk}:'
            tk.Label(row, text=spk_label, bg=AppStyles.BG_CARD,
                     fg=AppStyles.TEXT_DARK, font=('Segoe UI', 9),
                     width=18, anchor='w').pack(side='left')
            # Pre-fill: saved choice, else a gender-matched voice (cycling within
            # that gender so two same-gender speakers get distinct voices), else
            # fall back to cycling the whole list.
            default = saved.get(spk)
            if not default:
                pool = by_gender.get(g) if g else None
                if pool:
                    default = pool[_gender_counts[g] % len(pool)]
                    _gender_counts[g] += 1
                elif voice_keys:
                    default = voice_keys[idx % len(voice_keys)]
            # The visible StringVar shows the labelled form; the bare key is
            # recovered in _dub_save_speaker_map via _dub_label_to_key.
            var = tk.StringVar(value=self._dub_voice_label(default) if default else '')
            combo = ttk.Combobox(row, textvariable=var, values=display_values,
                                 width=42, state='readonly')
            combo.pack(side='left', padx=(6, 0))
            combo.bind('<<ComboboxSelected>>',
                       lambda e: self._dub_save_speaker_map())
            self._dub_speaker_vars[spk] = var

            # Preview buttons: hear the ACTOR's real voice vs. the ASSIGNED
            # voice, so voices aren't assigned blind.
            ModernButton(row, text='🎬 Actor', bg_color=AppStyles.ACCENT_INFO,
                         font=('Segoe UI', 8), padx=6, pady=2,
                         command=lambda s=spk: self._dub_preview_actor(s)).pack(
                             side='left', padx=(6, 0))
            ModernButton(row, text='▶ Voice', bg_color=AppStyles.ACCENT_SUCCESS,
                         font=('Segoe UI', 8), padx=6, pady=2,
                         command=lambda s=spk: self._dub_preview_voice(s)).pack(
                             side='left', padx=(4, 0))

        # Persist the (possibly auto-suggested) mapping immediately
        self._dub_save_speaker_map()

    def _dub_save_num_speakers(self):
        """Persist the exact-speaker-count choice.

        'Auto' clears the hint (pyannote guesses); a number N pins BOTH the min
        and max so the diarizer returns exactly N speakers.
        """
        raw = (self._dub_nspk_var.get() or 'Auto').strip()
        if raw.isdigit():
            n = int(raw)
            self.update_setting('dub_num_speakers', n)
            self.update_setting('dub_min_speakers', n)
            self.update_setting('dub_max_speakers', n)
        else:
            self.update_setting('dub_num_speakers', 0)
            self.update_setting('dub_min_speakers', 0)
            self.update_setting('dub_max_speakers', 0)

    def _dub_save_speaker_map(self):
        """Write the current speaker→voice dropdown state to settings.

        The comboboxes show labelled forms like "Puck (Male)"; store the bare
        voice key so the TTS engine gets exactly what it expects.
        """
        label_to_key = getattr(self, '_dub_label_to_key', {})
        mapping = {}
        for spk, var in self._dub_speaker_vars.items():
            disp = var.get()
            if not disp:
                continue
            mapping[spk] = label_to_key.get(disp, disp)
        self.update_setting('dub_speaker_voices', mapping)

    # ── Speaker/voice preview ───────────────────────────────────────────
    def _dub_preview_actor(self, speaker):
        """Play ~10s of the ACTOR's real voice from the video, so the user can
        hear who SPEAKER_xx actually is (and their true gender)."""
        if getattr(self, '_dub_running', False):
            self._dub_log('warn', 'Busy — please wait.')
            return
        video = (self._dub_video_var.get() or '').strip()
        if not video or not Path(video).is_file():
            messagebox.showerror('Preview', 'Pick a valid video file first.')
            return
        segs = getattr(self, '_dub_detected_segments', None)
        if not segs:
            messagebox.showinfo('Preview',
                                'Run "Detect Speakers" first so I know each '
                                'actor\'s timing.')
            return
        self._dub_log('info', f'Preview: extracting {speaker} audio…')
        t = threading.Thread(
            target=self._dub_preview_actor_worker,
            args=(Path(video), list(segs), speaker), daemon=True)
        t.start()

    def _dub_preview_actor_worker(self, video, segs, speaker):
        try:
            import tempfile
            out = Path(tempfile.gettempdir()) / f'_dub_actor_{speaker}.wav'
            res = dubbing_engine.preview_actor_clip(
                video, segs, speaker, out, log=self._dub_log, max_dur=10.0)
            if res and Path(res).is_file():
                os.startfile(str(res))  # play in default audio player
            else:
                self._dub_log('warn', f'Preview: no audio for {speaker}')
        except Exception as e:
            self._dub_log('error', f'Preview actor failed: {e}')

    def _dub_preview_voice(self, speaker):
        """Render + play a short TTS sample of the voice currently assigned to
        this speaker — exactly as the dub will produce it."""
        if getattr(self, '_dub_running', False):
            self._dub_log('warn', 'Busy — please wait.')
            return
        var = self._dub_speaker_vars.get(speaker)
        disp = var.get() if var else ''
        label_to_key = getattr(self, '_dub_label_to_key', {})
        voice = label_to_key.get(disp, disp)
        if not voice:
            messagebox.showinfo('Preview', 'Pick a voice for this speaker first.')
            return
        self._dub_log('info', f'Preview: rendering "{voice}" for {speaker}…')
        t = threading.Thread(
            target=self._dub_preview_voice_worker,
            args=(speaker, voice), daemon=True)
        t.start()

    def _dub_preview_voice_worker(self, speaker, voice):
        try:
            import tempfile
            # A short line in the target language reads more naturally than
            # English when auditioning a dub voice.
            sample = self._dub_preview_sample_text()
            out = Path(tempfile.gettempdir()) / f'_dub_voice_{speaker}.mp3'
            res = dubbing_engine.preview_voice(
                sample, voice, self.settings, out, log=self._dub_log)
            if res and Path(res).is_file():
                os.startfile(str(res))
            else:
                self._dub_log('warn', f'Preview: could not render {voice}')
        except Exception as e:
            self._dub_log('error', f'Preview voice failed: {e}')

    def _dub_preview_sample_text(self):
        """A one-line audition sample; uses the target language when known."""
        tgt = (self._dub_lang_var.get() or '').strip().lower() if hasattr(
            self, '_dub_lang_var') else ''
        samples = {
            'urdu': 'السلام علیکم، یہ میری آواز کا نمونہ ہے۔',
            'hindi': 'नमस्ते, यह मेरी आवाज़ का नमूना है।',
            'arabic': 'مرحبا، هذه عينة من صوتي.',
        }
        for k, v in samples.items():
            if k in tgt:
                return v
        return 'Hello, this is a sample of my dubbing voice.'

    def _dub_detect_speakers(self):
        """Run diarized transcription on the chosen video → list speakers."""
        if getattr(self, '_dub_running', False):
            self._dub_log('warn', 'A dub is already running — please wait.')
            return
        video = (self._dub_video_var.get() or '').strip()
        if not video or not Path(video).is_file():
            messagebox.showerror('Detect Speakers',
                                 'Please pick a valid video file first.')
            return
        # Persist the toggle + token before detecting
        self.update_setting('dub_multispeaker', self._dub_multi_var.get())
        self.update_setting('hf_token', self._dub_hf_var.get().strip())

        self._dub_running = True
        self._dub_detect_btn.config(state='disabled')
        self._dub_detect_status.set('Detecting… (this can take a minute)')
        self._dub_log('header', 'Detecting speakers…')
        src_lang = (self._dub_src_lang_var.get() or '').strip()
        t = threading.Thread(
            target=self._dub_detect_worker,
            args=(Path(video),
                  src_lang if src_lang != 'Auto-detect' else None),
            daemon=True)
        t.start()

    def _dub_detect_worker(self, video: Path, src_lang):
        try:
            words = dubbing_engine.transcribe_video(
                video, self.settings, log=self._dub_log,
                source_language=src_lang, diarize=True,
                hf_token=self._dub_hf_var.get().strip() or None,
                min_spk=self.settings.get('dub_min_speakers') or None,
                max_spk=self.settings.get('dub_max_speakers') or None)
            segs = dubbing_engine.group_words_into_segments(words) if words else []
            speakers = dubbing_engine.distinct_speakers(segs) if segs else []
            if not speakers:
                self._dub_log('warn', 'No speakers detected — check the video/log.')
                self._dub_video_widget_after(
                    lambda: self._dub_detect_status.set('No speakers detected.'))
                return
            self._dub_log('ok', f'Detected {len(speakers)} speaker(s): {speakers}')
            # Cache the diarized segments + video so the per-speaker preview
            # buttons can extract each actor's ORIGINAL voice on demand.
            self._dub_detected_segments = segs
            self._dub_detected_video = video
            # Estimate each speaker's gender from voice pitch so the default
            # voice can be gender-matched instead of assigned by index.
            genders = {}
            try:
                genders = dubbing_engine.estimate_speaker_genders(
                    video, segs, log=self._dub_log)
                if genders:
                    self._dub_log('info', f'Dub: estimated genders {genders}')
            except Exception as e:
                self._dub_log('warn', f'Dub: gender estimate failed ({e})')
            self._dub_video_widget_after(
                lambda: (self._dub_build_speaker_rows(speakers, genders),
                         self._dub_detect_status.set(
                             f'{len(speakers)} speaker(s) — pick a voice each.')))
        except Exception as e:
            self._dub_log('error', f'Detect speakers failed: {e}')
            for ln in traceback.format_exc().splitlines():
                self._dub_log('error', f'  {ln}')
            self._dub_video_widget_after(
                lambda: self._dub_detect_status.set('❌ Failed. See log.'))
        finally:
            self._dub_running = False
            self._dub_video_widget_after(
                lambda: self._dub_detect_btn.config(state='normal'))

    # ── Logging (thread-safe via after) ─────────────────────────────────
    def _dub_log(self, level, msg, max_lines=2000):
        """Append a timestamped line to the dub log widget, mirroring the
        Our Script tab's ``_os_log`` format (timestamp + colored tag)."""
        if not hasattr(self, '_dub_log_widget'):
            return
        from datetime import datetime as _dt
        ts = _dt.now().strftime('%H:%M:%S')
        if level not in ('info', 'ok', 'warn', 'error', 'path', 'header'):
            level = 'info'

        def _append():
            try:
                w = self._dub_log_widget
                w.configure(state='normal')
                w.insert('end', f'[{ts}] ', 'ts')
                w.insert('end', msg + '\n', level)
                # Trim to max_lines to avoid runaway memory.
                try:
                    line_count = int(w.index('end-1c').split('.')[0])
                    if line_count > max_lines:
                        cut = line_count - int(max_lines * 0.75)
                        w.delete('1.0', f'{cut}.0')
                except Exception:
                    pass
                if (getattr(self, '_dub_log_autoscroll_var', None) is not None
                        and self._dub_log_autoscroll_var.get()):
                    w.see('end')
                w.configure(state='disabled')
                if hasattr(self, '_dub_log_lines_var'):
                    cur = w.index('end-1c').split('.')[0]
                    self._dub_log_lines_var.set(f'{cur} lines')
            except Exception:
                pass
        try:
            self._dub_log_widget.after(0, _append)
        except Exception:
            print(f'[{ts}] {msg}')

    def _dub_set_status(self, text):
        try:
            self._dub_status_var.set(text)
        except Exception:
            pass

    def _dub_set_progress(self, done, total, note=''):
        try:
            pct = (done / total * 100) if total else 0
            self._dub_progress_var.set(pct)
            if note:
                self._dub_set_status(note)
        except Exception:
            pass

    # ── Run handler ─────────────────────────────────────────────────────
    def _dub_start(self):
        if getattr(self, '_dub_running', False):
            self._dub_log('warn', 'A dub is already running — please wait.')
            return

        lang = (self._dub_lang_var.get() or '').strip()
        src_lang = (self._dub_src_lang_var.get() or '').strip()
        if not lang:
            messagebox.showerror('Dubbing', 'Please pick a target language.')
            return

        # ── Batch-folder mode takes priority when a folder is set ─────────
        folder = (self._dub_folder_var.get() or '').strip()
        if folder:
            if not Path(folder).is_dir():
                messagebox.showerror('Dubbing', 'The batch folder does not exist.')
                return
            recursive = bool(self._dub_batch_recursive_var.get())
            vids = self._dub_scan_folder(folder, recursive)
            if not vids:
                messagebox.showerror(
                    'Dubbing', 'No videos found in that folder.')
                return
            self.update_setting('dub_batch_folder', folder)
            self.update_setting('dub_target_language', lang)
            self.update_setting('dub_source_language', src_lang)

            self._dub_running = True
            self._dub_run_btn.config(state='disabled')
            self._dub_progress_var.set(0)
            self._dub_log('header',
                          f'Batch dub started: {len(vids)} video(s) → {lang}')
            self._dub_set_status(f'Batch: 0/{len(vids)}…')
            t = threading.Thread(
                target=self._dub_batch_worker,
                args=(vids, lang, src_lang), daemon=True)
            t.start()
            return

        # ── Single-video mode ─────────────────────────────────────────────
        video = (self._dub_video_var.get() or '').strip()
        if not video or not Path(video).is_file():
            messagebox.showerror(
                'Dubbing', 'Please pick a valid video file, or choose a '
                           'folder for batch mode.')
            return

        src = Path(video)
        out_video = self._dub_batch_out_path(src, lang)

        # Persist selections
        self.update_setting('dub_last_video', video)
        self.update_setting('dub_target_language', lang)
        self.update_setting('dub_source_language', src_lang)

        self._dub_running = True
        self._dub_run_btn.config(state='disabled')
        self._dub_progress_var.set(0)
        self._dub_log('header', f'Dub started: {src.name} → {lang}')
        self._dub_set_status('Starting…')

        t = threading.Thread(
            target=self._dub_worker,
            args=(src, out_video, lang, src_lang), daemon=True)
        t.start()

    def _dub_preview_source(self) -> Path | None:
        """Pick the video the overlay preview should be drawn on: the single
        Source Video, else the first video in the batch folder."""
        v = (self._dub_video_var.get() or '').strip()
        if v and Path(v).is_file():
            return Path(v)
        folder = (self._dub_folder_var.get() or '').strip()
        if folder and Path(folder).is_dir():
            recursive = bool(self._dub_batch_recursive_var.get())
            vids = self._dub_scan_folder(folder, recursive)
            if vids:
                self._dub_video_var.set(str(vids[0]))
                return vids[0]
        return None

    def _dub_preview_overlays(self):
        """Render a single-frame preview of the dub overlays (blur / title /
        bottom text / caption position) and open it, so the user can position
        elements before running the full (slow) dub."""
        if getattr(self, '_dub_running', False):
            self._dub_log('warn', 'A dub is running — please wait.')
            return
        src = self._dub_preview_source()
        if src is None:
            messagebox.showerror(
                'Dubbing', 'Pick a video (or set a batch folder) to preview on.')
            return
        self._dub_preview_btn.config(state='disabled')
        self._dub_set_status(f'Previewing overlays on {Path(src).name}…')
        self._dub_log('info', f'Preview: overlays on {Path(src).name} …')

        def _run():
            try:
                png = dubbing_engine.preview_dub_overlays(
                    src, self.settings, self._dub_log)
                self._dub_video_widget_after(
                    lambda: (os.startfile(str(png)),
                             self._dub_set_status(
                                 f'Preview ready → {Path(png).name}')))
            except Exception as e:
                self._dub_log('error', f'Preview failed: {e}')
                for ln in traceback.format_exc().splitlines():
                    self._dub_log('error', f'  {ln}')
                self._dub_video_widget_after(
                    lambda: self._dub_set_status('Preview failed.'))
            finally:
                self._dub_video_widget_after(
                    lambda: self._dub_preview_btn.config(state='normal'))

        t = threading.Thread(target=_run, daemon=True)
        t.start()

    # ── Live overlay preview (mirrors the Our Script tab's scrub preview) ──
    def _dub_open_live_preview(self, *_):
        """Open a scrubbable Toplevel showing the dub overlays LIVE.

        Mirrors the Our Script tab's ``_os_open_live_preview``: a portrait
        canvas, a timeline scrub slider, and a time label.  Dragging the
        slider re-renders ONE frame at that time with the SAME overlay view
        the dub effects render builds (region/custom blur, title text,
        bottom-text CTA) plus the caption text active at that moment — so the
        user can position every element before running the full (slow) dub.
        """
        if getattr(self, '_dub_running', False):
            self._dub_log('warn', 'A dub is running — please wait.')
            return
        src = self._dub_preview_source()
        if src is None:
            messagebox.showerror(
                'Dubbing', 'Pick a video (or set a batch folder) to preview on.')
            return

        if getattr(self, '_dub_lp_window', None) is not None:
            try:
                self._dub_lp_window.destroy()
            except Exception:
                pass
            self._dub_lp_window = None
        self._dub_lp_rendering = False

        from PIL import Image, ImageTk  # type: ignore

        win = tk.Toplevel(self.root)
        win.title(f'🔍 Dub Live Preview — {Path(src).name}')
        win.configure(bg=AppStyles.BG_CARD)
        win.transient(self.root)
        win.resizable(False, False)
        self._dub_lp_window = win

        # Scratch state for the preview.
        self._dub_lp_video = Path(src)
        self._dub_lp_img = None          # latest rendered PIL image
        self._dub_lp_photo = None        # keep a ref so Tk doesn't GC it
        self._dub_lp_preview_scale = 1.0

        top = tk.Frame(win, bg=AppStyles.BG_CARD)
        top.pack(fill='x', padx=10, pady=(10, 4))
        tk.Label(top, text=f'Scrub to position   •   {Path(src).name}',
                 bg=AppStyles.BG_CARD, fg=AppStyles.TEXT_DARK,
                 font=('Segoe UI', 9, 'bold')).pack(side='left')

        # Time vars (duration filled in after first frame).
        self._dub_lp_time_var = tk.DoubleVar(value=0.0)
        self._dub_lp_dur_var = tk.DoubleVar(value=0.0)

        # Canvas — portrait phone box like the Our Script preview.
        canvas_w, canvas_h = 270, 480
        self._dub_lp_canvas = tk.Canvas(
            win, width=canvas_w, height=canvas_h, bg='#000',
            highlightbackground='#30363d', highlightthickness=1)
        self._dub_lp_canvas.pack(padx=10, pady=(2, 4))

        # Scrub row: time label + slider.
        scrub_row = tk.Frame(win, bg=AppStyles.BG_CARD)
        scrub_row.pack(fill='x', padx=10, pady=(0, 2))
        self._dub_lp_time_label = tk.Label(
            scrub_row, text='0:00 / 0:00', bg=AppStyles.BG_CARD,
            fg=AppStyles.TEXT_MEDIUM, font=('Consolas', 8))
        self._dub_lp_time_label.pack(side='left', anchor='w')
        self._dub_lp_scrub = ttk.Scale(
            scrub_row, orient='horizontal', from_=0, to=1.0,
            variable=self._dub_lp_time_var,
            command=self._dub_live_preview_refresh, length=240)
        self._dub_lp_scrub.pack(side='right', fill='x', expand=True, padx=(8, 0))

        # Bottom row: overlay toggles + Refresh + Close.
        btn_row = tk.Frame(win, bg=AppStyles.BG_CARD)
        btn_row.pack(fill='x', padx=10, pady=(2, 10))

        # Left side: toggles
        toggles = tk.Frame(btn_row, bg=AppStyles.BG_CARD)
        toggles.pack(side='left', fill='x', expand=True)
        tk.Label(toggles, text='Draw in preview:',
                bg=AppStyles.BG_CARD, fg=AppStyles.TEXT_DARK,
                font=('Segoe UI', 8, 'bold')).pack(side='left', padx=(0, 6))

        self._dub_preview_show_blur_var = tk.BooleanVar(
            value=self.settings.get('dub_preview_show_blur', True))
        tk.Checkbutton(
            toggles, text='🌫 Blur',
            variable=self._dub_preview_show_blur_var,
            bg=AppStyles.BG_CARD, fg=AppStyles.TEXT_DARK,
            selectcolor=AppStyles.BG_INPUT,
            activebackground=AppStyles.BG_CARD,
            font=('Segoe UI', 8),
            command=lambda: (
                self.update_setting('dub_preview_show_blur',
                                    self._dub_preview_show_blur_var.get()),
                self._dub_live_preview_refresh())).pack(side='left', padx=2)

        self._dub_preview_show_title_var = tk.BooleanVar(
            value=self.settings.get('dub_preview_show_title', True))
        tk.Checkbutton(
            toggles, text='🅣 Title',
            variable=self._dub_preview_show_title_var,
            bg=AppStyles.BG_CARD, fg=AppStyles.TEXT_DARK,
            selectcolor=AppStyles.BG_INPUT,
            activebackground=AppStyles.BG_CARD,
            font=('Segoe UI', 8),
            command=lambda: (
                self.update_setting('dub_preview_show_title',
                                    self._dub_preview_show_title_var.get()),
                self._dub_live_preview_refresh())).pack(side='left', padx=2)

        self._dub_preview_show_caption_var = tk.BooleanVar(
            value=self.settings.get('dub_preview_show_caption', True))
        tk.Checkbutton(
            toggles, text='📝 Caption',
            variable=self._dub_preview_show_caption_var,
            bg=AppStyles.BG_CARD, fg=AppStyles.TEXT_DARK,
            selectcolor=AppStyles.BG_INPUT,
            activebackground=AppStyles.BG_CARD,
            font=('Segoe UI', 8),
            command=lambda: (
                self.update_setting('dub_preview_show_caption',
                                    self._dub_preview_show_caption_var.get()),
                self._dub_live_preview_refresh())).pack(side='left', padx=2)

        self._dub_preview_show_bottom_var = tk.BooleanVar(
            value=self.settings.get('dub_preview_show_bottom', True))
        tk.Checkbutton(
            toggles, text='📄 Bottom',
            variable=self._dub_preview_show_bottom_var,
            bg=AppStyles.BG_CARD, fg=AppStyles.TEXT_DARK,
            selectcolor=AppStyles.BG_INPUT,
            activebackground=AppStyles.BG_CARD,
            font=('Segoe UI', 8),
            command=lambda: (
                self.update_setting('dub_preview_show_bottom',
                                    self._dub_preview_show_bottom_var.get()),
                self._dub_live_preview_refresh())).pack(side='left', padx=2)

        # Right side: Refresh + Close
        btn_right = tk.Frame(btn_row, bg=AppStyles.BG_CARD)
        btn_right.pack(side='right')
        self._dub_lp_refresh_btn = ModernButton(
            btn_right, text='🔄 Refresh', bg_color=AppStyles.ACCENT_INFO,
            hover_color='#0891b2', font=('Segoe UI', 9, 'bold'),
            padx=10, pady=4, command=self._dub_live_preview_refresh)
        self._dub_lp_refresh_btn.pack(side='left', padx=(0, 4))
        ModernButton(
            btn_right, text='✖ Close', bg_color=AppStyles.ACCENT_DANGER,
            hover_color='#dc2626', font=('Segoe UI', 9, 'bold'),
            padx=10, pady=4, command=win.destroy).pack(side='left')
        win.protocol('WM_DELETE_WINDOW', self._dub_lp_close)

        # First render.
        self._dub_live_preview_refresh()
        self._dub_lp_canvas.after(0, self._dub_lp_canvas.focus_set)

    def _dub_lp_close(self):
        win = getattr(self, '_dub_lp_window', None)
        if win is not None:
            try:
                win.destroy()
            except Exception:
                pass
        self._dub_lp_window = None
        self._dub_lp_photo = None

    def _dub_caption_at(self, t: float) -> str:
        """Best placeholder caption text active at time *t*.

        Uses the cached diarized segments from "Detect Speakers" when they
        belong to the current preview video — the real dialogue at that
        moment — else a generic placeholder.  (Actual TRANSLATED lines only
        exist after a full dub run.)
        """
        src = getattr(self, '_dub_lp_video', None)
        segs = getattr(self, '_dub_detected_segments', None)
        det_video = getattr(self, '_dub_detected_video', None)
        if segs and src and det_video and Path(det_video) == src:
            for s in segs:
                st = float(s.get('start', 0) or 0)
                en = float(s.get('end', 0) or 0)
                txt = (s.get('text') or '').strip()
                if txt and st <= t < en:
                    return txt
        return 'Translated captions appear here'

    def _dub_live_preview_refresh(self, *_):
        """Re-render the preview frame at the scrub position and display it."""
        if not hasattr(self, '_dub_lp_canvas'):
            return
        # Debounce: a re-render already queued covers this slider tick.
        if getattr(self, '_dub_lp_rendering', False):
            return
        self._dub_lp_rendering = True

        def _render():
            try:
                t = max(0.0, float(self._dub_lp_time_var.get() or 0.0))
                # Probe duration once (cheap) so the scrub + label have a range.
                try:
                    import cv2
                    cap = cv2.VideoCapture(str(self._dub_lp_video))
                    fps = cap.get(cv2.CAP_PROP_FPS) or 25.0
                    nf = int(cap.get(cv2.CAP_PROP_FRAME_COUNT) or 0)
                    cap.release()
                    dur = (nf / fps) if fps > 0 and nf > 0 else 0.0
                except Exception:
                    dur = 0.0
                if dur > 0:
                    self._dub_video_widget_after(lambda: (
                        self._dub_lp_dur_var.set(dur),
                        self._dub_lp_scrub.configure(to=max(dur, 1.0))))
                cap_text = self._dub_caption_at(t)
                img = dubbing_engine.render_dub_overlay_frame(
                    self._dub_lp_video, self.settings, time_sec=t,
                    log=self._dub_log, caption_text=cap_text)
                self._dub_video_widget_after(
                    lambda: self._dub_lp_show(img, t))
            except Exception as e:
                self._dub_log('error', f'Live preview failed: {e}')
            finally:
                self._dub_lp_rendering = False

        threading.Thread(target=_render, daemon=True).start()

    def _dub_lp_show(self, img, time_sec: float):
        """Fit *img* onto the canvas, record the display scale, show it."""
        if not hasattr(self, '_dub_lp_canvas'):
            return
        try:
            from PIL import ImageTk  # type: ignore

            w, h = img.size
            cw = self._dub_lp_canvas.winfo_width() or 270
            ch = self._dub_lp_canvas.winfo_height() or 480
            if cw < 50:
                cw = 270
            if ch < 50:
                ch = 480
            scale = min(cw / w, ch / h, 1.0)
            nw = max(1, int(w * scale))
            nh = max(1, int(h * scale))
            self._dub_lp_preview_scale = float(nw) / float(w) if w else 1.0

            photo = ImageTk.PhotoImage(img.resize((nw, nh)))
            self._dub_lp_photo = photo  # keep a reference
            self._dub_lp_canvas.delete('all')
            self._dub_lp_canvas.create_image(
                cw // 2, ch // 2, image=photo, anchor='center')

            dur = float(self._dub_lp_dur_var.get() or 0.0)
            self._dub_lp_time_label.config(
                text=f'{int(time_sec // 60)}:{int(time_sec % 60):02d} / '
                     f'{int(dur // 60)}:{int(dur % 60):02d}')
        except Exception:
            # Window was destroyed while the render thread ran — ignore.
            pass

    def _dub_batch_worker(self, vids, lang: str, src_lang: str):
        """Dub every video in *vids* one-by-one on this worker thread.

        Each video is independent: a failure on one is logged and the batch
        continues with the next. The overall progress bar tracks video count;
        per-video engine progress is streamed to the log/status line.
        """
        total = len(vids)
        done = ok = skipped = failed = 0
        skip_done = bool(self._dub_batch_skip_done_var.get())
        try:
            for idx, src in enumerate(vids, 1):
                if not getattr(self, '_dub_running', False):
                    self._dub_log('warn', 'Batch cancelled.')
                    break
                out_video = self._dub_batch_out_path(src, lang)
                self._dub_set_status(
                    f'Batch {idx}/{total}: {src.name}')
                self._dub_set_progress(idx - 1, total,
                                       f'Batch {idx}/{total}: {src.name}')

                if skip_done and Path(out_video).is_file() \
                        and Path(out_video).stat().st_size > 0:
                    self._dub_log('info',
                                  f'[{idx}/{total}] ⏭ already dubbed — {src.name}')
                    skipped += 1
                    done += 1
                    continue

                self._dub_log('header', f'[{idx}/{total}] {src.name} → {lang}')
                try:
                    result = dubbing_engine.dub_video(
                        src, out_video, lang, self.settings,
                        log=self._dub_log,
                        progress=self._dub_set_progress,
                        keep_audio_file=bool(self._dub_keep_audio_var.get()),
                        source_language=src_lang
                        if src_lang != 'Auto-detect' else None)
                    if result is not None:
                        self._dub_log('ok',
                                      f'[{idx}/{total}] ✅ {Path(result).name}')
                        ok += 1
                    else:
                        self._dub_log('error',
                                      f'[{idx}/{total}] ❌ failed — {src.name}')
                        failed += 1
                except Exception as e:
                    self._dub_log('error',
                                  f'[{idx}/{total}] error on {src.name}: {e}')
                    for ln in traceback.format_exc().splitlines():
                        self._dub_log('error', f'  {ln}')
                    failed += 1
                done += 1
                self._dub_set_progress(done, total)

            summary = (f'Batch done: {ok} dubbed, {skipped} skipped, '
                       f'{failed} failed (of {total}).')
            self._dub_log('header', summary)
            self._dub_set_status(f'✅ {summary}')
            self._dub_video_widget_after(
                lambda: messagebox.showinfo('Batch dubbing complete', summary))
        finally:
            self._dub_running = False
            self._dub_video_widget_after(
                lambda: self._dub_run_btn.config(state='normal'))

    def _dub_worker(self, src: Path, out_video: Path, lang: str,
                    src_lang: str = 'Auto-detect'):
        try:
            result = dubbing_engine.dub_video(
                src, out_video, lang, self.settings,
                log=self._dub_log,
                progress=self._dub_set_progress,
                keep_audio_file=bool(self._dub_keep_audio_var.get()),
                source_language=src_lang if src_lang != 'Auto-detect' else None)
            if result is not None:
                self._dub_log('ok', f'Done → {result}')
                self._dub_set_status(f'✅ Done: {Path(result).name}')
                self._dub_video_widget_after(
                    lambda: messagebox.showinfo(
                        'Dubbing complete',
                        f'Dubbed video written to:\n{result}'))
            else:
                self._dub_log('error', 'Dub failed — see log above.')
                self._dub_set_status('❌ Failed. See log.')
        except Exception as e:
            self._dub_log('error', f'Unexpected error: {e}')
            for ln in traceback.format_exc().splitlines():
                self._dub_log('error', f'  {ln}')
            self._dub_set_status('❌ Error. See log.')
        finally:
            self._dub_running = False
            self._dub_video_widget_after(
                lambda: self._dub_run_btn.config(state='normal'))

    def _dub_video_widget_after(self, fn):
        """Run *fn* on the Tk main thread."""
        try:
            self._dub_log_widget.after(0, fn)
        except Exception:
            try:
                fn()
            except Exception:
                pass