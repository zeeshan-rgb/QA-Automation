"""Small reusable widgets."""
from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtGui import QBrush, QColor
from PySide6.QtWidgets import (QFrame, QLabel, QTableWidget, QTableWidgetItem, QVBoxLayout,
                               QWidget, QHeaderView, QAbstractItemView)

from .theme import STATUS_COLORS


class Card(QFrame):
    def __init__(self, label: str, value: str = "-", hint: str = "", parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("card")
        lay = QVBoxLayout(self)
        lay.setContentsMargins(16, 12, 16, 12)
        self.value = QLabel(value)
        self.value.setObjectName("cardValue")
        self.label = QLabel(label)
        self.label.setObjectName("cardLabel")
        lay.addWidget(self.value)
        lay.addWidget(self.label)
        if hint:
            h = QLabel(hint)
            h.setObjectName("muted")
            h.setWordWrap(True)
            lay.addWidget(h)

    def set(self, value: str) -> None:
        self.value.setText(value)


def heading(text: str, sub: str = "") -> QWidget:
    w = QWidget()
    lay = QVBoxLayout(w)
    lay.setContentsMargins(0, 0, 0, 8)
    t = QLabel(text)
    t.setObjectName("h1")
    lay.addWidget(t)
    if sub:
        s = QLabel(sub)
        s.setObjectName("muted")
        s.setWordWrap(True)
        lay.addWidget(s)
    return w


def make_table(headers: list[str], stretch_last: bool = True) -> QTableWidget:
    t = QTableWidget(0, len(headers))
    t.setHorizontalHeaderLabels(headers)
    t.verticalHeader().setVisible(False)
    t.setSelectionBehavior(QAbstractItemView.SelectRows)
    t.setSelectionMode(QAbstractItemView.SingleSelection)
    t.setEditTriggers(QAbstractItemView.NoEditTriggers)
    t.setAlternatingRowColors(False)
    t.setWordWrap(False)
    hh = t.horizontalHeader()
    hh.setSectionResizeMode(QHeaderView.ResizeToContents)
    if stretch_last:
        hh.setStretchLastSection(True)
    return t


def item(text: str, status_key: str | None = None, tooltip: str | None = None) -> QTableWidgetItem:
    it = QTableWidgetItem(text)
    if status_key and status_key in STATUS_COLORS:
        bg, fg = STATUS_COLORS[status_key]
        it.setBackground(QBrush(QColor(bg)))
        it.setForeground(QBrush(QColor(fg)))
        it.setTextAlignment(Qt.AlignCenter)
    if tooltip:
        it.setToolTip(tooltip)
    return it


def pretty(value: str) -> str:
    return value.replace("_", " ").capitalize()
