"""
==============================================================================
 sound-bt GUI (PySide6 / Qt)
 Author:  dkxce (c)  https://github.com/dkxce/Tidradio-TD-H9
 Version: v1.2.0

 English: Qt window:
            - pick an Input device (radio mic / "in") used for monitoring
            - pick an Output device used for TRANSMITTING audio INTO the radio
            - pick the RFCOMM (Bluetooth COM) port used for PTT detection
            - pick the "PTT redirect output" where gated radio audio goes
            - "Monitor radio": while on (and radio PTT held), whatever comes
              from the input is routed to the redirect output; the PTT lamp
              lights while the gate is open.
            - Playlist: add files/folders/.m3u (also via drag'n'drop), search
              with highlight, and play/pause/stop/prev/next with a position
              slider, elapsed/ETA and resume-from-position on restart.
 Russian: Qt-окно:
            - выбор Входа (микрофон рации, для мониторинга)
            - выбор Выхода (для ПЕРЕДАЧИ звука В рацию)
            - выбор RFCOMM (Bluetooth COM) порта для определения PTT
            - выбор "PTT redirect output" — куда уводить гейтованный звук рации
            - "Monitor радио": пока включён (и удерживается PTT рации), звук со
              входа уходит на redirect-выход; лампочка PTT горит, пока гейт
              открыт.
            - Плейлист: добавление файлов/папок/.m3u (в т.ч. drag'n'drop),
              поиск с подсветкой, кнопки play/pause/stop/prev/next, слайдер
              позиции, elapsed/ETA и возобновление с позиции при запуске.
==============================================================================
"""

from __future__ import annotations

import os
import sys
import time

from PySide6.QtCore import QRectF, Qt, QTimer, Signal
from PySide6.QtGui import QColor, QFont, QFontMetricsF, QPalette
from PySide6.QtWidgets import (
    QAbstractItemView, QApplication, QCheckBox, QComboBox, QFileDialog, QFrame,
    QHBoxLayout, QLabel, QLineEdit, QListWidget, QListWidgetItem, QMainWindow,
    QMenu, QMessageBox, QPushButton, QSlider, QStyledItemDelegate, QStyle,
    QVBoxLayout, QWidget,
)

import sound_bt.core as core


class HighlightDelegate(QStyledItemDelegate):
    """Draws list items with the current search query highlighted (green
    background + bold), matching case-insensitively."""

    def __init__(self, get_query, parent=None):
        """Create a delegate that highlights the current search query."""
        super().__init__(parent)
        # A callable returning the current query string (reads the live search box).
        self._get_query = get_query

    def paint(self, painter, option, index):
        """Custom painting entry point: read the display text and draw the row
        (background + possibly highlighted text)."""
        painter.save()
        # read the item's stored display string; it may be overridden by role
        text = index.data(Qt.ItemDataRole.DisplayRole) or ""
        self._draw_base(painter, option, index, text)
        painter.restore()

    def _draw_base(self, painter, option, index, text):
        """Draw the row background (selection/current highlight or normal)
        and then the text."""
        # fill the background for selection / current like default
        if option.state & QStyle.StateFlag.State_Selected:
            painter.fillRect(option.rect, option.palette.highlight())
        else:
            painter.fillRect(option.rect,
                             option.palette.base() if option.state &
                             QStyle.StateFlag.State_Enabled else option.palette.window())
        self._draw_text(painter, option, text)

    def _draw_text(self, painter, option, text):
        """Draw the track text. If a search query is active, the matched
        substring in the filename is painted on a green background in bold."""
        painter.setPen(option.palette.color(QPalette.ColorRole.Text))
        font = option.font
        painter.setFont(font)
        rect = option.rect.adjusted(4, 0, -4, 0)
        query = (self._get_query() or "").strip()
        if not query:
            painter.drawText(rect, int(Qt.AlignmentFlag.AlignLeft |
                                       Qt.AlignmentFlag.AlignVCenter), text)
            return
        q_low = query.lower()
        # find occurrence in the part after " - " (the filename)
        dash = text.find(" - ")
        base = text[dash + 3:] if dash >= 0 else text
        idx = base.lower().find(q_low)
        if idx < 0:
            painter.drawText(rect, int(Qt.AlignmentFlag.AlignLeft |
                                       Qt.AlignmentFlag.AlignVCenter), text)
            return
        # split into prefix + before-match, the match, and after-match segments
        prefix = text[:dash + 3] if dash >= 0 else ""
        before = base[:idx]
        hit = base[idx:idx + len(query)]
        after = base[idx + len(query):]
        x = rect.left()
        for seg, is_hit in ((prefix + before, False), (hit, True), (after, False)):
            if not seg and not is_hit:
                continue
            # measure the segment width so segments are laid out left to right
            fm = QFontMetricsF(font)
            w = fm.horizontalAdvance(seg)
            if is_hit:
                # highlight: green background, white bold text
                painter.fillRect(QRectF(x, rect.top(), w, rect.height()),
                                 QColor("#2db300"))
                painter.setPen(QColor("#ffffff"))
                bf = QFont(font)
                bf.setBold(True)
                painter.setFont(bf)
                painter.drawText(QRectF(x, rect.top(), w, rect.height()),
                                 int(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter),
                                 seg)
                painter.setFont(font)
                painter.setPen(option.palette.color(QPalette.ColorRole.Text))
            else:
                painter.drawText(QRectF(x, rect.top(), w, rect.height()),
                                 int(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter),
                                 seg)
            x += w


class PlaylistWidget(QListWidget):
    """A playlist view with drag'n'drop (files/folders) and a context menu
    (Add files/folder, Open playlist, Save playlist, Remove, Clear)."""

    def __init__(self, window: "SoundBtWindow"):
        super().__init__()
        self._win = window
        self.setSelectionMode(QListWidget.SelectionMode.ExtendedSelection)
        self.setAcceptDrops(True)
        self.setDragEnabled(True)
        self.setDefaultDropAction(Qt.DropAction.CopyAction)
        self.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.customContextMenuRequested.connect(self._show_menu)
        self.setMinimumHeight(140)
        self.setItemDelegate(HighlightDelegate(
            lambda: self._win.search_edit.text() if hasattr(self._win, "search_edit") else "",
            self))

    # -- drop support ---------------------------------------------------------
    # Allow external files/folders to be dragged onto the playlist. Native
    # drag events only reach us when they carry file URLs; everything else is
    # passed to the base class.
    def dragEnterEvent(self, e):
        if e.mimeData().hasUrls():
            e.acceptProposedAction()
        else:
            super().dragEnterEvent(e)

    def dragMoveEvent(self, e):
        if e.mimeData().hasUrls():
            e.acceptProposedAction()
        else:
            super().dragMoveEvent(e)

    def dropEvent(self, e):
        """Drop handler: extract local file paths from the dropped URLs and
        add them to the playlist model, then refresh the view."""
        if e.mimeData().hasUrls():
            paths = [u.toLocalFile() for u in e.mimeData().urls()]
            paths = [p for p in paths if p]
            if paths:
                added = self._win.playlist_add_paths(paths)
                if added > 0:
                    self._win.set_status(f"Added {added} item(s) to playlist")
            e.acceptProposedAction()
        else:
            super().dropEvent(e)

    # -- context menu ---------------------------------------------------------
    def _show_menu(self, pos):
        """Build and show the right-click context menu for the playlist."""
        menu = QMenu(self)
        menu.addAction("Add files…", self._win.playlist_add_files)
        menu.addAction("Add folder…", self._win.playlist_add_folder)
        menu.addAction("Open playlist (.m3u/.m3u8)…", self._win.playlist_open)
        menu.addAction("Save playlist…", self._win.playlist_save)
        menu.addSeparator()
        if self.selectedIndexes():
            menu.addAction("Remove selected", self._win.playlist_remove_selected)
        menu.addAction("Clear all", self._win.playlist_clear)
        menu.exec(self.viewport().mapToGlobal(pos))

    @staticmethod
    def _hashes(count: int) -> int:
        """Number of '#' used for the numbering width based on list size.
        <10 -> #, 10..99 -> ##, 100..999 -> ###, 1000..1999 -> ####. Max 2000."""
        if count < 10:
            return 1
        if count < 100:
            return 2
        if count < 1000:
            return 3
        return 4

    def refresh_items(self):
        """Repopulate the list from the model, numbering each row, keeping the
        current play row. Numbers are right-zero-padded to a width derived from
        the total count (#/##/###/####), capped at 2000 tracks."""
        self.apply_filter()

    def _format_track(self, i: int, name: str) -> str:
        """Build the display string for track number `i`: zero-padded index (to
        the width derived from the total count) + " - " + filename."""
        count = min(len(self._win.playlist.items), 2000)
        width = self._hashes(count)
        return f"{i:0{width}d} - {name}"

    def apply_filter(self):
        """Re-render the list. The number of tracks stays constant — search
        only highlights matches (via the delegate) and scrolls the first match
        into the visible area."""
        query = (self._win.search_edit.text() if hasattr(self._win, "search_edit")
                 else "").strip()
        q_low = query.lower()
        items = self._win.playlist.items[:2000]
        playing = self._win.playlist.current()
        self.blockSignals(True)
        self.clear()
        for i, item in enumerate(items, start=1):
            name = os.path.basename(item)
            self.addItem(QListWidgetItem(self._format_track(i, name)))
        # keep the playing track selected if visible
        if playing is not None and playing in items:
            self.setCurrentRow(items.index(playing))
        if q_low:
            # scroll to the first matching row (highlighting is drawn by
            # HighlightDelegate), so the user always sees a hit.
            for r in range(self.count()):
                if q_low in self.item(r).text().lower():
                    self.scrollToItem(
                        self.item(r),
                        QAbstractItemView.ScrollHint.PositionAtCenter)
                    break
        if hasattr(self._win, "set_status"):
            total = len(items)
            self._win.set_status(f"{total} track(s)")
        self.blockSignals(False)


class SoundBtWindow(QMainWindow):
    vad_changed = Signal(bool)  # PTT lamp (driven by monitor checkbox AND radio PTT)

    def __init__(self):
        """Build the main window: init state fields, construct the UI, refresh
        device lists, load the saved playlist, and start the playback-position
        refresh timer."""
        super().__init__()
        self.setWindowTitle("TD-H9 Sound Router — Bluetooth audio bridge by dxkce")
        self.resize(740, 380)

        # Core workers and playback state.
        self.router: core.StreamRouter | None = None
        self.rfcomm_listener: core.RfcommPttListener | None = None
        self._playing = False
        self._paused = False
        # Remembered resume state (track + position) restored on startup.
        self._resume_pos = 0.0
        self._resume_flag = False
        self._resume_index = -1

        self.vad_changed.connect(self._vad_set_color)
        self._all_devices: list[core.AudioDevice] = []
        self._build_ui()
        self.refresh_devices()
        self._load_playlist()
        self._play_timer = QTimer(self)
        self._play_timer.setInterval(200)
        self._play_timer.timeout.connect(self._timer_tick)
        self._play_timer.start()

    # ------------------------------------------------------------------ UI
    def _build_ui(self):
        """Build the whole widget tree: device group, monitor row with PTT
        lamp, playlist panel (search box, list, transport buttons, slider and
        time label), and the status bar."""
        central = QWidget()
        self.setCentralWidget(central)
        root = QVBoxLayout(central)
        root.setContentsMargins(12, 12, 12, 8)

        # -- device group --
        dev_frame = QFrame()
        dev_frame.setFrameShape(QFrame.Shape.StyledPanel)
        dv = QVBoxLayout(dev_frame)
        title = QLabel("Devices")
        title.setStyleSheet("font-weight: bold;")
        dv.addWidget(title)

        self._dev_labels: list[QLabel] = []

        def _dev_row(label: str, combo_attr: str, tooltip: str = ""):
            """Create one labelled QComboBox row. The label is kept for later
            alignment; the combo stretches to the right edge of the frame."""
            row = QHBoxLayout()
            lbl = QLabel(label)
            lbl.setToolTip(tooltip)
            row.addWidget(lbl)
            combo = QComboBox()
            setattr(self, combo_attr, combo)
            row.addWidget(combo, 1)     # combo stretches to the right edge
            dv.addLayout(row)
            self._dev_labels.append(lbl)

        _dev_row("Radio input (monitor)", "in_combo",
                 "Device that carries the radio's voice INTO the PC (radio mic / SCO).")
        _dev_row("Output (to radio)", "out_combo",
                 "Device used to play audio INTO the radio while transmitting.")
        _dev_row("RFCOMM (PTT port)", "rfcomm_combo",
                 "Bluetooth serial (COM) port carrying the radio's PTT commands "
                 "(+SPP=P on press, +SPP=R on release).")
        _dev_row("PTT redirect output", "redirect_combo",
                 "Where the gated radio audio is routed while PTT is held. "
                 "TD-H9 devices are excluded. Defaults to the system default output.")
        self.in_combo.activated.connect(self._in_changed)
        self.out_combo.activated.connect(self._out_changed)
        self.rfcomm_combo.activated.connect(self._rfcomm_changed)
        self.redirect_combo.activated.connect(self._redirect_changed)

        brow = QHBoxLayout()
        brow.addStretch(1)               # push the Refresh button to the right
        refresh_btn = QPushButton("Refresh")
        refresh_btn.clicked.connect(self.refresh_devices)
        brow.addWidget(refresh_btn)
        dv.addLayout(brow)
        root.addWidget(dev_frame)

        # -- monitor radio + VAD lamp --
        mon_row = QHBoxLayout()
        self.monitor_chk = QCheckBox(
            "Monitor: play radio input → Windows default output")
        self.monitor_chk.setStyleSheet("font-size: 15px; font-weight: bold;")
        self.monitor_chk.toggled.connect(self._monitor_toggled)
        mon_row.addWidget(self.monitor_chk)
        mon_row.addStretch(1)
        self.vad_lbl = QLabel("PTT")
        self.vad_lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.vad_lbl.setFixedSize(86, 30)
        self.vad_lbl.setStyleSheet(
            "QLabel { background-color: #888; color: white; font-weight: bold;"
            " border-radius: 5px; }")
        self.vad_lbl.setToolTip("PTT lamp: lights while Monitor is enabled AND "
                                "the radio's PTT is held (via the RFCOMM port).")
        mon_row.addWidget(self.vad_lbl)
        root.addLayout(mon_row)

        # -- playlist (transmit into radio) --
        self.playlist = core.Playlist()
        pl_frame = QFrame()
        pl_frame.setFrameShape(QFrame.Shape.StyledPanel)
        plv = QVBoxLayout(pl_frame)
        pl_title = QLabel("Playlist")
        pl_title.setStyleSheet("font-weight: bold;")
        plv.addWidget(pl_title)

        search_row = QHBoxLayout()
        search_lbl = QLabel("Search:")
        search_row.addWidget(search_lbl)
        self.search_edit = QLineEdit()
        self.search_edit.setPlaceholderText("type to filter & highlight tracks…")
        self.search_edit.setClearButtonEnabled(True)   # X button to clear
        self.search_edit.textChanged.connect(self._apply_search)
        search_row.addWidget(self.search_edit, 1)
        plv.addLayout(search_row)

        self.playlist_list = PlaylistWidget(self)
        self.playlist_list.itemDoubleClicked.connect(self._play_index)
        plv.addWidget(self.playlist_list)

        play_row = QHBoxLayout()
        self.play_btn = QPushButton("▶ Play")
        self.play_btn.clicked.connect(self.toggle_play)
        self.pause_btn = QPushButton("⏸ Pause")
        self.pause_btn.clicked.connect(self.toggle_pause)
        self.stop_btn = QPushButton("⏹ Stop")
        self.stop_btn.clicked.connect(self.stop_play)
        self.prev_btn = QPushButton("⏮ Prev")
        self.prev_btn.clicked.connect(self.play_prev)
        self.next_btn = QPushButton("⏭ Next")
        self.next_btn.clicked.connect(self.play_next)
        play_row.addWidget(self.play_btn)
        play_row.addWidget(self.pause_btn)
        play_row.addWidget(self.stop_btn)
        play_row.addWidget(self.prev_btn)
        play_row.addWidget(self.next_btn)
        plv.addLayout(play_row)

        self.play_state = QLabel("")
        plv.addWidget(self.play_state)

        time_row = QHBoxLayout()
        self.time_lbl = QLabel("")
        self.time_lbl.setMinimumWidth(150)
        time_row.addWidget(self.time_lbl)
        self.slider = QSlider(Qt.Orientation.Horizontal)
        self.slider.setEnabled(False)
        self.slider.sliderReleased.connect(self._on_slider_released)
        self.slider.valueChanged.connect(self._on_slider_value_changed)
        time_row.addWidget(self.slider, 1)
        plv.addLayout(time_row)
        root.addWidget(pl_frame)

        self.status = QLabel("Idle")
        self.status.setStyleSheet("color: #333;")
        root.addWidget(self.status)

    # ------------------------------------------------------------ devices
    def refresh_devices(self):
        """Re-query the audio device list and refill every device combo box."""
        try:
            devs = core.list_bt_devices()
        except Exception as e:
            QMessageBox.critical(self, "Device list error", str(e))
            return
        self._all_devices = devs
        self._fill_combo(self.in_combo, "in")
        self._fill_combo(self.out_combo, "out")
        self._fill_redirect()
        self._fill_rfcomm()
        self._align_device_combos()
        self.set_status("Ready")

    def _align_device_combos(self):
        """Align device rows: give every label the width of the widest one, so
        the combos all start at the same x. Each combo keeps its size and sits
        right after the label (not pushed to the far right)."""
        labels = self._dev_labels
        label_w = max((lbl.sizeHint().width() for lbl in labels), default=1)
        for lbl in labels:
            lbl.setFixedWidth(label_w)

    def _fill_combo(self, combo: QComboBox, want_kind: str):
        """Fill an in/out device combo from `self._all_devices`, applying the
        device-kind filter (TD-H9 / Bluetooth only, no '@' raw endpoints) and
        preserving the user's previous selection when possible."""
        prev_idx = combo.currentIndex()
        combo.blockSignals(True)
        combo.clear()
        for idx, d in enumerate(self._all_devices):
            has_kind = (d.max_in > 0) if want_kind == "in" else (d.max_out > 0)
            if not has_kind:
                continue
            if want_kind == "in":
                # Radio input: keep only TD-H9 devices that are Bluetooth
                # (in/BT) and have no '@' in the name.
                if not (d.is_bt and "td-h9" in d.name.lower()) or "@" in d.name:
                    continue
            else:
                # Output: keep only TD-H9 names that do NOT contain '@'
                # (the '@' ones are raw bthhfenum/sys HFP endpoints).
                if not ("td-h9" in d.name.lower()) or "@" in d.name:
                    continue
            combo.addItem(f"{d.kind():<8} | {d.name}", idx)
        prev = None
        if prev_idx >= 0 and prev_idx < combo.count():
            prev = combo.itemData(prev_idx)
        target = 0
        if combo.count():
            if want_kind == "in":
                # Prefer the TD-H9 HFP microphone for monitoring.
                hfp = next((i for i in range(combo.count())
                            if self._dev_by_combo(combo, i).is_bt), None)
                if hfp is not None:
                    target = hfp
            else:
                # For output prefer the default Windows output by default.
                tgt = next((i for i in range(combo.count())
                            if self._dev_by_combo(combo, i).default_out), None)
                if tgt is not None:
                    target = tgt
        if prev is not None and combo.itemData(target) != prev:
            for i in range(combo.count()):
                if combo.itemData(i) == prev:
                    target = i
                    break
        combo.setCurrentIndex(target)
        combo.blockSignals(False)
        if want_kind == "out":
            self._out_changed()
        else:
            self._in_changed()

    def _dev_by_combo(self, combo: QComboBox, row: int) -> core.AudioDevice:
        """Return the AudioDevice that backs the given row of a device combo
        (the combo stores the device index as item data)."""
        return self._all_devices[combo.itemData(row)]

    def _selected_dev(self, combo: QComboBox) -> core.AudioDevice | None:
        """Return the AudioDevice currently selected in `combo`, or None."""
        r = combo.currentIndex()
        if r < 0 or not self._all_devices:
            return None
        return self._dev_by_combo(combo, r)

    def _in_changed(self):
        """Called when the radio input selection changes: rebuild the router if
        monitoring is currently active."""
        if self.router is not None and self.monitor_chk.isChecked():
            self._set_router()

    def _out_changed(self):
        """Called when the transmit output selection changes. Output is used
        only for playback into the radio, so no router rebuild is needed."""
        pass

    def _fill_rfcomm(self):
        """Fill the RFCOMM (Bluetooth COM) port combo, preserving selection."""
        prev = self.rfcomm_combo.currentData()  # the selected port (or None)
        ports = core.list_rfcomm_ports()  # list[(port, device_name)]
        self.rfcomm_combo.blockSignals(True)
        self.rfcomm_combo.clear()
        self.rfcomm_combo.addItem("None (disabled)", None)
        for port, name in ports:
            label = port if not name else f"{port} ({name})"
            self.rfcomm_combo.addItem(label, port)
        idx = 0
        if prev:
            # Keep the user's explicit choice if the port is still present.
            i = self.rfcomm_combo.findData(prev)
            if i >= 0:
                idx = i
        else:
            # Default: pick the TD-H9 device (e.g. "COM11 (TD-H9-9223)") if found.
            for i in range(self.rfcomm_combo.count()):
                port = self.rfcomm_combo.itemData(i)
                if not port:
                    continue
                name = next((n for p, n in ports if p == port), "")
                if "td-h9" in name.lower():
                    idx = i
                    break
        self.rfcomm_combo.setCurrentIndex(idx)
        self.rfcomm_combo.blockSignals(False)
        self._rfcomm_changed()

    def _rfcomm_changed(self):
        """(Re)start the RFCOMM PTT listener for the selected COM port."""
        port = self.rfcomm_combo.currentData()
        if self.rfcomm_listener is not None:
            self.rfcomm_listener.stop()
            self.rfcomm_listener = None
            self._set_ptt(False)
        if port:
            self.rfcomm_listener = core.RfcommPttListener(
                port,
                on_ptt_down=lambda: self._set_ptt(True),
                on_ptt_up=lambda: self._set_ptt(False),
            )
            self.rfcomm_listener.start()

    def _fill_redirect(self):
        """Fill the PTT redirect output combo. Lists every output device except
        TD-H9, defaulting to the system's default output."""
        prev = self.redirect_combo.currentData()
        def_out_idx = core.default_output_index()
        self.redirect_combo.blockSignals(True)
        self.redirect_combo.clear()
        # "System default" pseudo-entry mapped to the actual default device.
        self.redirect_combo.addItem("System default", def_out_idx)
        for d in self._all_devices:
            if d.max_out <= 0:
                continue
            if core.is_td_h9_name(d.name) or d.is_hfp:
                continue
            self.redirect_combo.addItem(f"{d.kind():<8} | {d.name}", d.index)
        idx = 0
        if prev is not None:
            i = self.redirect_combo.findData(prev)
            if i >= 0:
                idx = i
        self.redirect_combo.setCurrentIndex(idx)
        self.redirect_combo.blockSignals(False)
        self._redirect_changed()

    def _redirect_changed(self):
        """Hot re-route the gated audio to the newly selected output device."""
        if self.router is not None:
            self.router.set_output(self.redirect_combo.currentData())

    # ------------------------------------------------------------- router
    def _set_router(self):
        """(Re)build the router: input -> Windows default output, with VAD."""
        was_on = self.router is not None and self.router.running
        if self.router is not None:
            self.router.stop()
            self.router = None
        if not was_on:
            return
        in_dev = self._selected_dev(self.in_combo)
        if in_dev is None or in_dev.max_in <= 0:
            self.set_status("Select a radio input device first")
            return
        out_idx = core.default_output_index()
        if out_idx is None:
            self.set_status("No Windows default output available")
            return
        self.router = core.StreamRouter(
            in_dev.index, out_idx,
            on_voice_on=self._vad_on,
            on_voice_off=self._vad_off,
        )
        self.router.set_monitor(self.monitor_chk.isChecked())
        self.router.set_output(self.redirect_combo.currentData())
        self.router.start()
        self.set_status(f"Monitoring: {in_dev.name[:30]} → redirect device")

    def _set_ptt(self, active: bool):
        """Forward a PTT state change from the RFCOMM listener into the router."""
        if self.router is not None:
            self.router.set_ptt(active)

    def _monitor_toggled(self, checked: bool):
        """Handle the Monitor checkbox toggle. On: validate the input and
        default output, build/start the router with the gate enabled, and show
        status. Off: disable the gate and stop the router."""
        if checked:
            in_dev = self._selected_dev(self.in_combo)
            if in_dev is None or in_dev.max_in <= 0:
                QMessageBox.warning(
                    self, "Monitor", "Select a radio input device first.")
                self.monitor_chk.blockSignals(True)
                self.monitor_chk.setChecked(False)
                self.monitor_chk.blockSignals(False)
                return
            out_idx = core.default_output_index()
            if out_idx is None:
                QMessageBox.warning(self, "Monitor",
                                    "No Windows default output available.")
                self.monitor_chk.blockSignals(True)
                self.monitor_chk.setChecked(False)
                self.monitor_chk.blockSignals(False)
                return
            if self.router is None:
                self.router = core.StreamRouter(
                    in_dev.index, out_idx,
                    on_voice_on=self._vad_on,
                    on_voice_off=self._vad_off,
                )
            self.router.set_monitor(self.monitor_chk.isChecked())
            self.router.set_output(self.redirect_combo.currentData())
            self.router.start()
            self.set_status("Monitoring radio input → redirect device")
        else:
            if self.router is not None:
                self.router.set_monitor(False)
                self.router.stop()
            self._vad_off()
            self.set_status("Monitor off")

    # -------------------------------------------------------------- VAD/PTT lamp
    def _vad_on(self):
        """Turn the PTT lamp on (called by the router when the gate opens)."""
        self.vad_changed.emit(True)

    def _vad_off(self):
        """Turn the PTT lamp off (gate closed)."""
        self.vad_changed.emit(False)

    def _vad_set_color(self, on: bool):
        """Update the PTT lamp label's color when the gate state changes."""
        if on:
            self.vad_lbl.setStyleSheet(
                "QLabel { background-color: #00c800; color: white; font-weight:"
                " bold; border-radius: 5px; }")
        else:
            self.vad_lbl.setStyleSheet(
                "QLabel { background-color: #888; color: white; font-weight:"
                " bold; border-radius: 5px; }")

    # ------------------------------------------------------ playlist ops
    def _out_for_play(self) -> core.AudioDevice | None:
        """Return the currently selected transmit-output device, showing a
        warning and returning None if none is usable."""
        out_dev = self._selected_dev(self.out_combo)
        if out_dev is None or out_dev.max_out <= 0:
            QMessageBox.warning(self, "No output",
                                "Select an output device to transmit into the radio")
            return None
        return out_dev

    def playlist_add_paths(self, paths: list[str]) -> int:
        """Add files/folders/.m3u files to the playlist model and refresh."""
        added = self.playlist.add_m3u(paths)
        if added:
            self.playlist_list.refresh_items()
        return added

    def playlist_add_files(self):
        """Open a file picker and add the chosen audio/.m3u files to the playlist."""
        paths, _ = QFileDialog.getOpenFileNames(
            self, "Add audio files", "",
            "Audio (*.mp3 *.wav *.ogg *.flac *.m4a *.aac *.opus *.wma);;"
            "Playlists (*.m3u *.m3u8);;All files (*)",
        )
        if paths:
            self.set_status(f"Added {self.playlist_add_paths(paths)} item(s)")

    def playlist_add_folder(self):
        """Open a folder picker and add all audio files within it recursively."""
        folder = QFileDialog.getExistingDirectory(self, "Add audio folder")
        if folder:
            added = self.playlist_add_paths([folder])
            self.set_status(f"Added {added} audio file(s) from folder")

    def playlist_open(self):
        """Replace the current playlist with the contents of a chosen .m3u file."""
        path, _ = QFileDialog.getOpenFileName(
            self, "Open playlist", "", "Playlists (*.m3u *.m3u8);;All files (*)")
        if path:
            self.playlist.clear()
            added = self.playlist.add_m3u([path])
            self.playlist_list.refresh_items()
            self.set_status(f"Loaded playlist: {added} item(s)")

    def playlist_save(self):
        """Save the current playlist to a user-chosen .m3u/.m3u8 file."""
        if not self.playlist.items:
            QMessageBox.information(self, "Save playlist", "Playlist is empty")
            return
        path, _ = QFileDialog.getSaveFileName(
            self, "Save playlist", "playlist.m3u",
            "M3U playlist (*.m3u);;M3U8 playlist (*.m3u8)")
        if path:
            try:
                self.playlist.save_m3u(path)
                self.set_status(f"Playlist saved: {os.path.basename(path)}")
            except OSError as e:
                QMessageBox.critical(self, "Save playlist", str(e))

    def playlist_remove_selected(self):
        """Remove the currently selected playlist rows (stopping playback first)."""
        rows = sorted({i.row() for i in self.playlist_list.selectedIndexes()},
                      reverse=True)
        if rows:
            self._stop_playback_cleanup()
            self.playlist.remove(rows)
            self.playlist_list.refresh_items()
            self.set_status(f"Removed {len(rows)} item(s)")

    def playlist_clear(self):
        """Clear the entire playlist (stopping playback first)."""
        self._stop_playback_cleanup()
        self.playlist.clear()
        self.playlist_list.refresh_items()
        self.set_status("Playlist cleared")

    def _apply_search(self, text: str):
        """Live-search filter: re-render the playlist, keeping matches visible
        and scrolled into the view."""
        self.playlist_list.apply_filter()

    # ------------------------------------------------------ transport
    def _start_item(self, index: int):
        """Start playback of the playlist item at `index` (into the radio output).
        Uses the single core.playback player, which joins any previous session
        first, so audio never overlaps (next/prev produce no double sound)."""
        if not (0 <= index < len(self.playlist.items)):
            return
        out_dev = self._out_for_play()
        if out_dev is None:
            return
        self.playlist.index = index
        self._playing = True
        self._paused = False
        path = self.playlist.current()
        if not path:
            return
        # resume from the remembered position, but only once for that track
        start = 0.0
        if self._resume_flag and index == getattr(self, "_resume_index", -1):
            start = self._resume_pos
        self._resume_flag = False
        core.playback.play(path, out_dev.index,
                           on_done=self._on_item_done, on_error=self._on_item_error,
                           start_seconds=start)
        current = self.playlist_list.currentRow()
        if current != index:
            self.playlist_list.setCurrentRow(index)
        self.slider.setValue(0)
        self._update_play_ui()

    def _on_item_done(self):
        """Called (worker thread) when a track finishes: auto-advance."""
        QTimer.singleShot(0, self._advance_and_play)

    def _on_item_error(self, exc):
        QTimer.singleShot(0, lambda: self.set_status(f"Play error: {exc}"))

    def _advance_and_play(self):
        """Move to the next track and start it; called when a track finishes.
        If the playlist ran out, reset the UI as finished."""
        if self._playing and not self._paused:
            self.playlist.advance()
            if self.playlist.current() is not None:
                self._start_item(self.playlist.index)
            else:
                self._reset_play_ui(status="Playback finished")

    def _play_index(self, item):
        """Start playback of the playlist item that was double-clicked."""
        self._stop_playback_cleanup()
        self._start_item(self.playlist_list.row(item))

    def toggle_play(self):
        """Play/pause/resume toggle button logic: if playing pause; if paused
        resume; otherwise start the current (or first) playlist track."""
        if self._playing and not self._paused:
            # Playing -> pause instead.
            self.toggle_pause()
            return
        if self._paused:
            # Resume from pause (Player keeps position).
            self._resume_play()
            return
        if not self.playlist.items:
            QMessageBox.information(self, "Playlist", "Playlist is empty")
            return
        row = self.playlist_list.currentRow()
        if row < 0:
            row = 0
            self.playlist_list.setCurrentRow(0)
        self._start_item(row)

    def toggle_pause(self):
        """Pause/resume toggle: if unpaused and playing, pause (keeping the
        exact position); otherwise resume from where it was paused."""
        if not self._playing and not self._paused:
            return
        if self._paused:
            self._resume_play()
        else:
            self._playing = False
            self._paused = True
            core.playback.pause()
            self._update_play_ui()

    def _resume_play(self):
        """Resume the paused playback from the saved position."""
        self._playing = True
        self._paused = False
        core.playback.resume()
        self._update_play_ui()

    def play_prev(self):
        """Stop the current playback and start the previous track (wrap-around)."""
        self._stop_playback_cleanup()
        if not self.playlist.items:
            return
        prev = (self.playlist.index - 1) % len(self.playlist.items) \
            if self.playlist.index > 0 else len(self.playlist.items) - 1
        self._start_item(prev)

    def play_next(self):
        """Stop the current playback and start the next track (wrap-around)."""
        self._stop_playback_cleanup()
        if not self.playlist.items:
            return
        nxt = self.playlist.next_index()
        if nxt < 0:
            return
        self._start_item(nxt)

    def stop_play(self):
        """Stop playback and reset the UI."""
        self._stop_playback_cleanup()
        self.slider.setValue(0)
        self._reset_play_ui(status="Playback stopped")

    def _stop_playback_cleanup(self):
        """Stop the shared playback player and reset playback state flags.
        Uses wait=True so the worker thread is joined -> no overlapping audio."""
        self._playing = False
        self._paused = False
        core.playback.stop(wait=True)   # join worker -> no overlapping audio

    def _on_slider_released(self):
        """User let go of the slider (mouse release or << / >> keys):
        jump straight to the released position."""
        v = self.slider.value()
        if self._playing and not self._paused:
            core.playback.seek(v / 1000.0)
        elif self._paused:
            core.playback.seek(v / 1000.0)

    def _on_slider_value_changed(self, value):
        """While the user drags, preview the position in the label without
        jumping the audio (seek happens only on release)."""
        if self.slider.isSliderDown():
            ms = value / 1000.0
            dur = core.playback.position()[1]
            time_str = self._fmt(ms)
            dur_str = self._fmt(dur) if dur else ""
            self.time_lbl.setText(
                f"{time_str} / {dur_str}" if dur else f"{time_str}")

    def _update_play_ui(self):
        """Refresh the short status label above the slider reflecting the
        current playback state (▶ track / ⏸ paused / empty)."""
        if self._paused:
            self.play_state.setText("⏸ paused")
        elif self._playing:
            name = os.path.basename(self.playlist.current() or "")
            self.play_state.setText(f"▶ {name}")
        else:
            self.play_state.setText("")

    def _timer_tick(self):
        """Periodic timer (every 200 ms): poll the player position and update
        the position slider and the elapsed/ETA label. Doesn't fight the user
        while they are dragging the slider."""
        if self._playing and not self._paused:
            el, dur = core.playback.position()
            el = min(el, dur) if dur else el
            self.slider.setEnabled(True)
            self.slider.setMaximum(max(1, int(dur * 1000)))
            if not self.slider.isSliderDown():
                # Don't fight the user while they drag; update otherwise.
                self.slider.blockSignals(True)
                self.slider.setValue(int(el * 1000))
                self.slider.blockSignals(False)
                eta = max(0.0, dur - el) if dur else 0.0
                self.time_lbl.setText(
                    f"{self._fmt(el)} / {self._fmt(dur)}  −ETA {self._fmt(eta)}")
        elif self._paused:
            el, dur = core.playback.position()
            self.slider.blockSignals(True)
            self.slider.setEnabled(True)
            if dur:
                self.slider.setMaximum(max(1, int(dur * 1000)))
                self.slider.setValue(int(el * 1000))
            self.slider.blockSignals(False)
            self.time_lbl.setText(f"{self._fmt(el)} / {self._fmt(dur)}  ‑ paused")
        else:
            self.slider.blockSignals(True)
            self.slider.setEnabled(False)
            self.slider.setValue(0)
            self.slider.blockSignals(False)
            self.time_lbl.setText("")

    @staticmethod
    def _fmt(s: float) -> str:
        """Format a number of seconds as m:ss (e.g. 125.4 -> '2:05')."""
        m = int(s) // 60
        sec = int(s) % 60
        return f"{m}:{sec:02d}"

    def _reset_play_ui(self, status: str = ""):
        """Reset all playback state flags and the status label; optionally show
        the given status message."""
        self._playing = False
        self._paused = False
        self.play_state.setText("")
        if status:
            self.set_status(status)

    # -------------------------------------------------------------- status
    def set_status(self, text: str):
        """Set the text of the bottom status bar."""
        self.status.setText(text)

    # --------------------------------------------------- playlist persist
    @staticmethod
    def _playlist_path() -> str:
        """Path of the playlist.yml file, next to the exe/app folder."""
        base = os.path.dirname(os.path.abspath(sys.argv[0] or ""))
        return os.path.join(base, "playlist.yml")

    def _save_playlist(self):
        """Persist the playlist (items, active index, and — if a track was
        playing/paused — its position) to playlist.yml."""
        import yaml
        path = self._playlist_path()
        # remember the active track and, if playing/paused, its position
        playing = bool(self._playing or self._paused)
        position = core.playback.elapsed if playing else 0.0
        data = {
            "index": self.playlist.index,
            "items": list(self.playlist.items),
            "position": round(position, 1),
            "was_playing": playing,
        }
        try:
            with open(path, "w", encoding="utf-8") as f:
                yaml.safe_dump(data, f, allow_unicode=True, default_flow_style=False)
        except OSError as e:
            print(f"[playlist] save error: {e}")

    def _load_playlist(self):
        """Load the playlist and resume info from playlist.yml on startup.
        Removes items whose files no longer exist, restores the active index,
        and remembers the resume position (so Play resumes there)."""
        import yaml
        path = self._playlist_path()
        if not os.path.exists(path):
            return
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = yaml.safe_load(f) or {}
        except Exception as e:
            print(f"[playlist] load error: {e}")
            return
        items = data.get("items", []) if isinstance(data, dict) else []
        if not isinstance(items, list):
            items = []
        idx = int(data.get("index", -1) or -1) if isinstance(data, dict) else -1
        items = [p for p in items if isinstance(p, str) and os.path.exists(p)]
        if items:
            self.playlist.items = items
            self.playlist.index = idx if 0 <= idx < len(items) else -1
            self.playlist_list.refresh_items()
            # restore the active track selection
            row = self.playlist_list.currentRow()
            # remember where the user was so pressing Play resumes there
            self._resume_pos = float(data.get("position", 0) or 0)
            self._resume_flag = bool(data.get("was_playing", False))
            self._resume_index = idx
            self.set_status(f"Loaded playlist: {len(items)} item(s)")

    def closeEvent(self, event):
        """App shutdown: persist the playlist (with active track + position),
        stop the RFCOMM listener and the router, and stop any playback."""
        self._save_playlist()
        if self.rfcomm_listener is not None:
            self.rfcomm_listener.stop()
            self.rfcomm_listener = None
        if self.router is not None:
            self.router.stop()
        core.stop_playback()
        event.accept()


def _log_exception(exc_type, exc_value, exc_tb):
    """Unhandled-exception handler: append a timestamped traceback to
    bugtrack.log (next to the app), then delegate to the default excepthook."""
    import traceback
    try:
        path = os.path.join(os.path.dirname(os.path.abspath(sys.argv[0] or "")),
                            "bugtrack.log")
        lines = traceback.format_exception(exc_type, exc_value, exc_tb)
        with open(path, "a", encoding="utf-8") as f:
            f.write("=== %s ===\n" % time.strftime("%Y-%m-%d %H:%M:%S"))
            f.writelines(lines)
            f.write("\n")
    except Exception:
        pass
    # keep the default behaviour (print + crash)
    sys.__excepthook__(exc_type, exc_value, exc_tb)


def main():
    """Application entry point: install the unhandled-exception hook, create
    and show the main window, and run the Qt event loop."""
    import sys
    sys.excepthook = _log_exception
    app = QApplication(sys.argv)
    win = SoundBtWindow()
    win.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()