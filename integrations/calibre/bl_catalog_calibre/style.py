"""Reading Lamp Everforest palette, Qt stylesheet, and HTML detail renderer for BL-Catalog.

Shares the exact 15 reference hex tokens with ``src/bl_catalog/api/status.html`` and
``docs/design/library-surface.md`` (pinned to Library Finish 1.8.8 ``library-tokens.css``).
Dense panel labels blend 60% muted (#9da9a0) with 40% primary text (#d3c6aa) -> #b3b5a4
for >= 4.5:1 contrast over #343f44 and #3d484d surfaces.
"""

from __future__ import annotations

import html
from pathlib import Path
from typing import Any

TOKENS: dict[str, str] = {
    "color_bg": "#2d353b",
    "color_surface": "#343f44",
    "color_surface_2": "#3d484d",
    "color_border": "#475258",
    "color_border_control": "#9da9a0",
    "color_text": "#d3c6aa",
    "color_text_muted": "#9da9a0",
    "color_text_panel_muted": "#b3b5a4",
    "color_text_dim": "#7a8478",
    "color_on_primary": "#2d353b",
    "color_primary": "#7fbbb3",
    "color_primary_hover": "#96c9c2",
    "color_accent": "#d699b6",
    "color_success": "#a7c080",
    "color_warning": "#dbbc7f",
    "color_danger": "#e67e80",
}

BUCKET_STYLE: dict[str, dict[str, str]] = {
    "already": {
        "label": "Already stamped",
        "short": "already",
        "fg": TOKENS["color_text_panel_muted"],
        "bg": "#3d484d",
        "border": TOKENS["color_border"],
    },
    "certain_isbn": {
        "label": "Certain · ISBN",
        "short": "certain_isbn",
        "fg": TOKENS["color_success"],
        "bg": "#35443c",
        "border": "#5b7053",
    },
    "certain_title_author": {
        "label": "Certain · Title & Author",
        "short": "certain_title_author",
        "fg": TOKENS["color_success"],
        "bg": "#35443c",
        "border": "#5b7053",
    },
    "picked": {
        "label": "Picked · Owner",
        "short": "picked",
        "fg": TOKENS["color_primary"],
        "bg": "#314649",
        "border": TOKENS["color_primary"],
    },
    "ambiguous": {
        "label": "Ambiguous",
        "short": "ambiguous",
        "fg": TOKENS["color_warning"],
        "bg": "#44413a",
        "border": "#7a6d52",
    },
    "conflict": {
        "label": "Conflict",
        "short": "conflict",
        "fg": TOKENS["color_text"],
        "bg": "#47393d",
        "border": "#7f5357",
    },
    "not_in_blc": {
        "label": "Not in BLC",
        "short": "not_in_blc",
        "fg": TOKENS["color_text_panel_muted"],
        "bg": "#343f44",
        "border": TOKENS["color_border"],
    },
    "error": {
        "label": "Error",
        "short": "error",
        "fg": TOKENS["color_text"],
        "bg": "#47393d",
        "border": TOKENS["color_danger"],
    },
}

_CONTROL_RESOURCES: bytes | None = None


def _register_control_resources() -> None:
    """Load the arrows compiled with ``rcc --binary images/controls.qrc -o images/controls.rcc``."""
    global _CONTROL_RESOURCES
    if _CONTROL_RESOURCES is not None:
        return
    try:
        from qt.core import QResource  # type: ignore[import-not-found]
    except ImportError:
        try:
            from PyQt6.QtCore import QResource
        except ImportError:
            return
    resource_loader = globals().get("get_resources")
    if callable(resource_loader):
        data = resource_loader("images/controls.rcc")
    else:
        data = (Path(__file__).parent / "images" / "controls.rcc").read_bytes()
    if not data or not QResource.registerResourceData(data):
        raise RuntimeError("Could not load the BL-Catalog control arrow resources")
    # Qt requires the backing data to remain alive while the resource is registered.
    _CONTROL_RESOURCES = data


def build_stylesheet() -> str:
    """Return the Qt stylesheet using BL-Catalog's Reading Lamp tokens."""
    _register_control_resources()
    t = TOKENS
    return f"""
    QDialog, QWidget#MainSurface {{
        background-color: {t["color_bg"]};
        color: {t["color_text"]};
        font-family: "Inter", "Segoe UI", system-ui, sans-serif;
        font-size: 13px;
    }}
    QFrame#HeaderBar {{
        background-color: {t["color_bg"]};
        border-bottom: 1px solid {t["color_border"]};
    }}
    QLabel, QCheckBox {{
        color: {t["color_text"]};
    }}
    QLabel#BrandTitle {{
        color: {t["color_text"]};
        font-family: "Cinzel", "Georgia", serif;
        font-size: 18px;
        font-weight: 700;
        letter-spacing: 1.5px;
    }}
    QLabel#BrandVersion {{
        color: {t["color_text_muted"]};
        font-family: "JetBrains Mono", monospace;
        font-size: 11px;
    }}
    QFrame#Card, QFrame#HeroCard {{
        background-color: {t["color_surface"]};
        border: 1px solid {t["color_border"]};
        border-radius: 12px;
    }}
    QLabel#SectionHeading {{
        color: {t["color_text_panel_muted"]};
        font-size: 11px;
        font-weight: 600;
        letter-spacing: 1.2px;
    }}
    QLabel#HeroState {{
        color: {t["color_text"]};
        font-family: "Cinzel", "Georgia", serif;
        font-size: 20px;
        font-weight: 700;
    }}
    QLabel#HeroSub {{
        color: {t["color_text_panel_muted"]};
        font-size: 13px;
    }}
    QLabel#OperationProgress {{
        color: {t["color_text"]};
        background-color: transparent;
        font-weight: 600;
    }}
    QLabel#MutedLabel {{
        color: {t["color_text_panel_muted"]};
        font-size: 12px;
    }}
    QLineEdit, QComboBox, QSpinBox {{
        background-color: {t["color_bg"]};
        color: {t["color_text"]};
        border: 1px solid {t["color_border_control"]};
        border-radius: 8px;
        padding: 6px 10px;
        min-height: 24px;
        selection-background-color: {t["color_primary"]};
        selection-color: {t["color_on_primary"]};
    }}
    QLineEdit:focus, QComboBox:focus, QSpinBox:focus {{
        border: 2px solid {t["color_primary"]};
        padding: 5px 9px;
    }}
    QComboBox, QSpinBox {{
        padding-right: 42px;
    }}
    QComboBox:focus, QSpinBox:focus {{
        padding-right: 41px;
    }}
    QComboBox::drop-down {{
        subcontrol-origin: border;
        subcontrol-position: top right;
        width: 28px;
        border-left: 1px solid {t["color_border_control"]};
        border-top-right-radius: 8px;
        border-bottom-right-radius: 8px;
        background-color: {t["color_surface_2"]};
    }}
    QComboBox::down-arrow {{
        image: url(:/bl-catalog/controls/down.svg);
        width: 12px;
        height: 8px;
    }}
    QSpinBox {{
        min-height: 32px;
    }}
    QSpinBox::up-button {{
        subcontrol-origin: border;
        subcontrol-position: top right;
        width: 28px;
        border-left: 1px solid {t["color_border_control"]};
        border-top-right-radius: 8px;
        background-color: {t["color_surface_2"]};
    }}
    QSpinBox::down-button {{
        subcontrol-origin: border;
        subcontrol-position: bottom right;
        width: 28px;
        border-left: 1px solid {t["color_border_control"]};
        border-top: 1px solid {t["color_border_control"]};
        border-bottom-right-radius: 8px;
        background-color: {t["color_surface_2"]};
    }}
    QSpinBox::up-arrow {{
        image: url(:/bl-catalog/controls/up.svg);
        width: 12px;
        height: 8px;
    }}
    QSpinBox::down-arrow {{
        image: url(:/bl-catalog/controls/down.svg);
        width: 12px;
        height: 8px;
    }}
    QComboBox::drop-down:hover, QSpinBox::up-button:hover, QSpinBox::down-button:hover {{
        background-color: #314649;
    }}
    QPushButton {{
        background-color: {t["color_surface_2"]};
        color: {t["color_text"]};
        border: 1px solid {t["color_border_control"]};
        border-radius: 8px;
        padding: 6px 12px;
        font-weight: 600;
        min-height: 24px;
    }}
    QPushButton:hover:enabled {{
        border-color: {t["color_primary"]};
    }}
    QPushButton:checked {{
        background-color: #314649;
        border-color: {t["color_primary"]};
        color: {t["color_text"]};
    }}
    QPushButton#BucketButton {{
        padding: 4px 10px;
        min-height: 22px;
        border-radius: 6px;
        font-weight: 500;
    }}
    QPushButton#PrimaryButton {{
        background-color: {t["color_primary"]};
        border-color: {t["color_primary"]};
        color: {t["color_on_primary"]};
    }}
    QPushButton#PrimaryButton:hover:enabled {{
        background-color: {t["color_primary_hover"]};
        border-color: {t["color_primary_hover"]};
    }}
    QPushButton#DangerButton:hover:enabled {{
        background-color: #47393d;
        border-color: {t["color_danger"]};
        color: {t["color_text"]};
    }}
    QPushButton:focus:enabled {{
        border: 2px dashed {t["color_text"]};
        padding: 5px 11px;
    }}
    QPushButton#BucketButton:focus:enabled {{
        border: 2px dashed {t["color_text"]};
        padding: 3px 9px;
    }}
    QPushButton#PrimaryButton:focus:enabled {{
        background-color: {t["color_bg"]};
        color: {t["color_text"]};
        border: 2px dashed {t["color_primary"]};
    }}
    QPushButton:disabled, QPushButton#PrimaryButton:disabled,
    QPushButton#DangerButton:disabled {{
        background-color: {t["color_bg"]};
        color: {t["color_text_muted"]};
        border-color: {t["color_border"]};
    }}
    QTabWidget::pane {{
        border: none;
    }}
    QTabBar::tab {{
        background-color: {t["color_bg"]};
        color: {t["color_text_panel_muted"]};
        border: 1px solid {t["color_border"]};
        padding: 8px 10px;
        margin-bottom: 8px;
    }}
    QTabBar::tab:selected {{
        background-color: #314649;
        color: {t["color_text"]};
        border-color: {t["color_primary"]};
    }}
    QTabBar:focus {{
        border: 1px solid {t["color_primary"]};
    }}
    QTableWidget, QListWidget {{
        background-color: {t["color_surface"]};
        alternate-background-color: #313b40;
        color: {t["color_text"]};
        border: 1px solid {t["color_border"]};
        border-radius: 10px;
        gridline-color: {t["color_border"]};
        selection-background-color: {t["color_primary"]};
        selection-color: {t["color_on_primary"]};
    }}
    QTableWidget::item:selected, QListWidget::item:selected {{
        background-color: {t["color_primary"]};
        color: {t["color_on_primary"]};
    }}
    QTableWidget:focus, QListWidget:focus, QTextBrowser:focus {{
        border: 1px solid {t["color_primary"]};
    }}
    QHeaderView::section {{
        background-color: {t["color_bg"]};
        color: {t["color_text_panel_muted"]};
        border: none;
        border-bottom: 1px solid {t["color_border"]};
        border-right: 1px solid {t["color_border"]};
        padding: 6px 8px;
        font-size: 11px;
        font-weight: 600;
    }}
    QProgressBar {{
        background-color: {t["color_bg"]};
        border: 1px solid {t["color_border"]};
        border-radius: 5px;
        text-align: center;
        color: {t["color_text"]};
        max-height: 12px;
    }}
    QProgressBar::chunk {{
        background-color: {t["color_primary"]};
        border-radius: 4px;
    }}
    QCheckBox {{
        color: {t["color_text"]};
        spacing: 6px;
        border: 2px solid transparent;
        border-radius: 4px;
    }}
    QCheckBox:focus {{
        border-color: {t["color_primary"]};
        border-style: dashed;
    }}
    QTextBrowser {{
        background-color: {t["color_surface"]};
        color: {t["color_text"]};
        border: 1px solid {t["color_border"]};
        border-radius: 10px;
        padding: 10px;
    }}
    """


def _esc(value: Any) -> str:
    return html.escape(str(value if value is not None else ""))


def _scalar_val(field: Any) -> Any:
    return field.get("value") if isinstance(field, dict) else field


def render_work_detail_html(row: dict[str, Any], work: dict[str, Any] | None = None) -> str:
    """Render Reading Lamp HTML comparing a Calibre book row and its matched/candidate BLC work."""
    t = TOKENS
    bucket = str(row.get("bucket") or "not_in_blc")
    badge = BUCKET_STYLE.get(bucket, BUCKET_STYLE["not_in_blc"])
    add = row.get("add") or {}
    proposals = row.get("proposals") or {}

    cal_title = _esc(row.get("title") or "")
    cal_authors = _esc(row.get("authors") or "")
    cal_isbn = _esc(row.get("isbn") or "None")
    cal_blc = _esc(row.get("bl_catalog") or "None")
    reason = _esc(row.get("reason") or "")

    additions = []
    for k, v in sorted(add.items()):
        additions.append(
            f'<li style="color:{t["color_success"]};font-size:12px;">'
            f'<b>{_esc(k)}</b>: {_esc(v)}</li>'
        )
    for field_name, val in sorted(proposals.items()):
        if field_name == "identifiers" or not val:
            continue
        if field_name == "series" and isinstance(val, dict):
            shown = str(val.get("series") or "")
            if val.get("series_index") is not None:
                shown += f" #{val['series_index']}"
        elif field_name == "cover" and isinstance(val, dict):
            shown = str(val.get("label") or "Catalogue cover")
        else:
            shown = ", ".join(val) if isinstance(val, list) else str(val)
        if len(shown) > 72:
            shown = shown[:69] + "..."
        additions.append(
            f'<li style="color:{t["color_primary"]};font-size:12px;">'
            f'<b>{_esc(field_name)}</b>: {_esc(shown)}</li>'
        )
    additions_html = (
        '<ul style="margin-top:4px;margin-bottom:8px;margin-left:18px;">'
        + "".join(additions) + "</ul>"
    ) if additions else (
        f'<span style="color:{t["color_text_panel_muted"]};font-style:italic;">No changes proposed</span>'
    )

    work_html = ""
    if work:
        wid = _esc(work.get("id") or row.get("work_id") or "")
        w_title = _esc(_scalar_val(work.get("title")) or row.get("blc_title") or "")
        w_type = _esc(_scalar_val(work.get("type")) or "work")
        w_setting = _esc(_scalar_val(work.get("setting")) or "Not recorded")
        w_year = _esc(_scalar_val(work.get("first_published")) or "Not recorded")
        w_synopsis = _esc(_scalar_val(work.get("synopsis")) or "")

        title_obj = work.get("title") if isinstance(work.get("title"), dict) else {}
        agree = ", ".join(str(o) for o in (title_obj.get("agree") or [])) or "No sources recorded"
        score = title_obj.get("score")
        score_str = f"{score:.2f}" if isinstance(score, (int, float)) else "unavailable"

        people_rows = []
        for a in work.get("authors") or []:
            if not isinstance(a, dict):
                continue
            role = a.get("role") or "author"
            a_score = a.get("score")
            aliases = ", ".join(a.get("aliases") or [])
            alias_note = f' <span style="color:{t["color_text_panel_muted"]};">(aliases: {_esc(aliases)})</span>' if aliases else ""
            score_note = f" · score {a_score:.2f}" if isinstance(a_score, (int, float)) else ""
            people_rows.append(
                f"<li><b>{_esc(a.get('name'))}</b> "
                f"<span style=\"color:{t['color_text_panel_muted']};\">[{_esc(role)}{score_note}]</span>"
                f"{alias_note}</li>"
            )
        people_html = "<ul style='margin:4px 0 8px 18px;padding:0;'>" + "".join(people_rows) + "</ul>" if people_rows else "No credits recorded"

        series_list = work.get("series") or []
        series_html = ", ".join(
            f"{_esc(s.get('name'))}" + (f" #{_esc(s.get('position'))}" if s.get("position") is not None else "")
            for s in series_list if isinstance(s, dict)
        ) or "None"

        cover = work.get("cover") if isinstance(work.get("cover"), dict) else None
        if cover and cover.get("width") and cover.get("height"):
            up = " · Upscaled" if cover.get("upscaled") else ""
            good = cover.get("width", 0) >= 1000 or cover.get("upscaled")
            c_color = t["color_success"] if good else t["color_warning"]
            cover_html = (
                f'<span style="color:{c_color};font-weight:600;">'
                f'Lexicanum cover{up} · {int(cover["width"])}×{int(cover["height"])}</span>'
            )
        else:
            cover_html = f'<span style="color:{t["color_text_panel_muted"]};">No stored Lexicanum cover</span>'

        flags = work.get("flags") or []
        flags_html = ", ".join(
            f'<span style="color:{t["color_warning"]};font-family:monospace;">{_esc(f)}</span>'
            for f in flags
        ) or "none"

        synopsis_block = (
            f'<div style="margin-top:10px;padding:8px 10px;background:{t["color_bg"]};'
            f'border:1px solid {t["color_border"]};border-radius:8px;color:{t["color_text_panel_muted"]};'
            f'font-size:12px;">{w_synopsis}</div>'
            if w_synopsis else ""
        )

        work_html = f"""
        <hr style="border:none;border-top:1px solid {t["color_border"]};margin:12px 0;">
        <div style="font-size:11px;font-weight:600;text-transform:uppercase;letter-spacing:1px;color:{t["color_primary"]};margin-bottom:6px;">
            BL-Catalog Work · <span style="font-family:monospace;">{wid}</span>
        </div>
        <div style="font-size:15px;font-weight:700;color:{t["color_text"]};margin-bottom:4px;">
            {w_title}
        </div>
        <table style="width:100%;font-size:12px;border-collapse:collapse;">
            <tr><td style="color:{t["color_text_panel_muted"]};padding:2px 8px 2px 0;width:110px;">Type / Setting</td>
                <td style="color:{t["color_text"]};">{w_type} · {w_setting} (First pub: {w_year})</td></tr>
            <tr><td style="color:{t["color_text_panel_muted"]};padding:2px 8px 2px 0;">Series</td>
                <td style="color:{t["color_text"]};">{series_html}</td></tr>
            <tr><td style="color:{t["color_text_panel_muted"]};padding:2px 8px 2px 0;">Agreement</td>
                <td style="color:{t["color_text"]};">score {score_str} ({_esc(agree)}) · flags: {flags_html}</td></tr>
            <tr><td style="color:{t["color_text_panel_muted"]};padding:2px 8px 2px 0;">Cover</td>
                <td>{cover_html}</td></tr>
        </table>
        <div style="margin-top:6px;font-size:12px;">
            <span style="color:{t["color_text_panel_muted"]};">Credits:</span>
            {people_html}
        </div>
        {synopsis_block}
        """

    return f"""
    <div style="color:{t["color_text"]};font-family:'Inter','Segoe UI',sans-serif;line-height:1.45;">
        <div style="display:flex;align-items:center;justify-content:space-between;margin-bottom:8px;">
            <span style="display:inline-block;padding:3px 10px;border-radius:8px;
                         background:{badge["bg"]};border:1px solid {badge["border"]};
                         color:{badge["fg"]};font-weight:600;font-size:12px;">
                {_esc(badge["label"])}
            </span>
            <span style="color:{t["color_text_panel_muted"]};font-family:monospace;font-size:12px;">
                Calibre #{_esc(row.get("id"))}
            </span>
        </div>
        <div style="font-size:15px;font-weight:700;color:{t["color_text"]};margin-bottom:2px;">
            {cal_title}
        </div>
        <div style="font-size:12px;color:{t["color_text_panel_muted"]};margin-bottom:8px;">
            {cal_authors}
        </div>
        <table style="width:100%;font-size:12px;border-collapse:collapse;margin-bottom:8px;">
            <tr><td style="color:{t["color_text_panel_muted"]};padding:2px 8px 2px 0;width:110px;">Calibre ISBN</td>
                <td style="font-family:monospace;color:{t["color_text"]};">{cal_isbn}</td></tr>
            <tr><td style="color:{t["color_text_panel_muted"]};padding:2px 8px 2px 0;">Current BLC ID</td>
                <td style="font-family:monospace;color:{t["color_text"]};">{cal_blc}</td></tr>
            <tr><td style="color:{t["color_text_panel_muted"]};padding:2px 8px 2px 0;">Match reason</td>
                <td style="color:{t["color_text"]};">{reason}</td></tr>
        </table>
        <div style="margin:8px 0;">
            <div style="font-size:11px;font-weight:600;text-transform:uppercase;letter-spacing:1px;color:{t["color_text_panel_muted"]};margin-bottom:4px;">
                Proposed Updates
            </div>
            {additions_html}
        </div>
        {work_html}
    </div>
    """
