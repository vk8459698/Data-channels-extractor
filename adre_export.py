"""Drive ADRE Sxp's UI to export channel CSVs — then RotorDyn analyses them.

This removes the hand-export step **after** ADRE is installed and the click
recipe has been filled in on that machine.  Until then, ``find_adre_executable``
and ``tools/adre_ui_map.py`` are what you run on the EC2 box to teach the
recipe the real menus.

Architecture::

    native DB folder  →  ADRE Sxp (UI automation)  →  inbox/*.csv  →  pipeline
"""

from __future__ import annotations

import ctypes
import json
import os
import re
import subprocess
import time
from ctypes import wintypes
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

_IDC_SIZENS = 32645
_IDC_SIZEWE = 32644
_MOUSEEVENTF_LEFTDOWN = 0x0002
_MOUSEEVENTF_LEFTUP = 0x0004


def _enable_dpi_awareness() -> None:
    """UIA rectangles and SetCursorPos only match after the process is DPI-aware."""
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(2)
    except Exception:
        try:
            ctypes.windll.user32.SetProcessDPIAware()
        except Exception:
            pass

# Common install locations / executable names seen with Bently Nevada tools.
_ADRE_DIR_CANDIDATES = (
    r"C:\Program Files\Bently Nevada",
    r"C:\Program Files (x86)\Bently Nevada",
    r"C:\Program Files\Baker Hughes",
    r"C:\Program Files (x86)\Baker Hughes",
    r"C:\Program Files\ADRE",
    r"C:\Program Files (x86)\ADRE",
    r"C:\Program Files\ADRE Sxp",
    r"C:\Program Files (x86)\ADRE Sxp",
)
_ADRE_EXE_NAMES = (
    "ADRE Sxp.exe",
    "ADRESxp.exe",
    "AdreSxp.exe",
    "ADRE.exe",
    "Sxp.exe",
)


class AdreNotInstalled(RuntimeError):
    """ADRE Sxp is not on this machine (or not on PATH)."""


class RecipeIncomplete(RuntimeError):
    """The UI recipe still has placeholder steps that need mapping on EC2."""


@dataclass
class ExportJob:
    database: Path
    inbox: Path
    recipe_path: Path
    adre_exe: Path | None = None


def find_adre_executable(explicit: str | Path | None = None) -> Path:
    """Locate the ADRE Sxp executable, or raise :class:`AdreNotInstalled`."""
    if explicit:
        path = Path(explicit)
        if path.is_file():
            return path
        raise AdreNotInstalled(f"ADRE executable not found at {path}")

    env = os.environ.get("ADRE_SXP_EXE") or os.environ.get("ADRE_EXE")
    if env and Path(env).is_file():
        return Path(env)

    for folder in _ADRE_DIR_CANDIDATES:
        root = Path(folder)
        if not root.is_dir():
            continue
        for name in _ADRE_EXE_NAMES:
            hit = root / name
            if hit.is_file():
                return hit
        for hit in root.rglob("*.exe"):
            lowered = hit.name.lower()
            if "adre" in lowered and "uninstall" not in lowered:
                return hit
            if lowered in {n.lower() for n in _ADRE_EXE_NAMES}:
                return hit

    raise AdreNotInstalled(
        "ADRE Sxp is not installed on this machine. Install setup.exe on the "
        "Windows EC2 box, then either set ADRE_SXP_EXE to the .exe path or re-run "
        "this command there. Use tools/adre_ui_map.py once ADRE is open to fill "
        "adre_export_recipe.json."
    )


def load_recipe(path: str | Path) -> dict[str, Any]:
    path = Path(path)
    if not path.is_file():
        raise FileNotFoundError(
            f"No UI recipe at {path}. Copy adre_export_recipe.example.json to "
            f"adre_export_recipe.json and fill the steps using tools/adre_ui_map.py "
            f"on the EC2 box after ADRE is installed."
        )
    return json.loads(path.read_text(encoding="utf-8"))


def _require_pywinauto():
    try:
        from pywinauto import Application  # noqa: F401
    except ImportError as exc:
        raise ImportError(
            "UI automation needs pywinauto. Install it with: "
            "python -m pip install pywinauto"
        ) from exc


def _desktop_windows():
    from pywinauto import Desktop

    return Desktop(backend="uia").windows()


def _window_spec(win):
    """WindowSpecification. Desktop UIAWrapper has no child_window."""
    if win is None:
        return None
    if callable(getattr(win, "child_window", None)):
        return win
    from pywinauto import Application

    hwnd = int(win.handle)
    connected = Application(backend="uia").connect(handle=hwnd)
    return connected.window(handle=hwnd)


def _adre_top_window():
    for pat in (r"^ADRE® Sxp", r"^ADRE Sxp\b", r"^ADRE"):
        found = _uia_window_by_title(pat)
        if found is not None and "Cursor" not in (found.window_text() or ""):
            return found
    return None


def _by_auto_id(root, auto_id: str, *, descendants: bool = True):
    """Find by automation_id. Prefer children/descendants (child dialogs are not specs)."""
    try:
        nodes = list(root.children())
        if descendants:
            nodes.extend(root.descendants())
    except Exception:
        nodes = []
    for ctrl in nodes:
        if str(getattr(ctrl.element_info, "automation_id", "") or "") == auto_id:
            return ctrl
    if callable(getattr(root, "child_window", None)):
        try:
            spec = root.child_window(auto_id=auto_id)
            if spec.exists(timeout=0.5):
                return spec
        except Exception:
            pass
    return None


def _main_window(app, title_re: str = r"^ADRE"):
    """ADRE® Sxp (Configuration Hierarchy). Never HV - Plot Session."""
    try:
        spec = app.window(title_re=title_re)
        text = spec.window_text() or ""
        if "Plot Session" not in text:
            return spec
    except Exception:
        pass
    for pat in (r"^ADRE® Sxp", r"^ADRE Sxp\b", title_re):
        found = _uia_window_by_title(pat)
        if found is not None and "Plot Session" not in (found.window_text() or ""):
            return _window_spec(found)
    for win in _desktop_windows():
        text = win.window_text() or ""
        if not text or "Cursor" in text or "Plot Session" in text:
            continue
        if re.search(title_re, text, re.I) or re.search(r"^ADRE", text, re.I):
            return _window_spec(win)
    raise RecipeIncomplete(
        "ADRE® Sxp main window not found. HV - Plot Session may be covering it."
    )


def _plot_session_window(adre_window=None):
    """HV - Plot Session is a child of ADRE® Sxp (auto_id=PlotSessionWindow)."""
    roots = []
    if adre_window is not None:
        roots.append(adre_window)
    adre = _adre_top_window()
    if adre is not None:
        roots.append(adre)
    seen: set[int] = set()
    for root in roots:
        try:
            hwnd = int(root.handle)
        except Exception:
            hwnd = id(root)
        if hwnd in seen:
            continue
        seen.add(hwnd)
        try:
            kids = list(root.children())
        except Exception:
            continue
        for child in kids:
            auto = str(getattr(child.element_info, "automation_id", "") or "")
            text = child.window_text() or ""
            if auto == "PlotSessionWindow" or (child.window_text() or "").startswith("HV - Plot Session"):
                print(f"[adre] found child {text!r} auto_id={auto}", flush=True)
                return child
    return None


def _wait_plot_session(timeout: float = 25, adre_window=None):
    deadline = time.time() + timeout
    while time.time() < deadline:
        win = _plot_session_window(adre_window)
        if win is not None:
            return win
        time.sleep(0.4)
    return None


def _is_tree(ctrl) -> bool:
    kind = (ctrl.friendly_class_name() or "").lower()
    ctype = str(getattr(ctrl.element_info, "control_type", "") or "").lower()
    cls = str(getattr(ctrl.element_info, "class_name", "") or "").lower()
    return "tree" in kind or "tree" in ctype or "systreeview" in cls


def _is_tree_item(ctrl) -> bool:
    kind = (ctrl.friendly_class_name() or "").lower()
    ctype = str(getattr(ctrl.element_info, "control_type", "") or "").lower()
    return "treeitem" in kind or "tree item" in kind or "treeitem" in ctype


def _config_tree(window, index: int = 0):
    trees = [ctrl for ctrl in window.descendants() if _is_tree(ctrl)]
    if not trees:
        raise RecipeIncomplete("No Configuration Hierarchy tree in the ADRE window")
    if index >= len(trees):
        raise RecipeIncomplete(f"Tree index {index} not found ({len(trees)} trees)")
    return trees[index]


def _tree_items(tree) -> list:
    try:
        items = [ctrl for ctrl in tree.descendants() if _is_tree_item(ctrl)]
        if items:
            return items
    except Exception:
        pass
    try:
        return list(tree.roots())
    except Exception as exc:
        raise RecipeIncomplete(f"Could not read tree items: {exc}") from exc


def _item_text(item) -> str:
    try:
        return (item.window_text() or "").strip()
    except Exception:
        return ""


def _find_tree_item(tree, name: str, occurrence: int = 0):
    hits = [item for item in _tree_items(tree) if _item_text(item) == name]
    if not hits:
        raise RecipeIncomplete(f"Tree item {name!r} not found")
    if occurrence >= len(hits):
        raise RecipeIncomplete(f"Tree item {name!r} occurrence {occurrence} not found")
    return hits[occurrence]


def _expand(item) -> None:
    for method in ("expand", "select"):
        fn = getattr(item, method, None)
        if callable(fn):
            try:
                fn()
            except Exception:
                pass


_MANAGER_SKIP_TITLES = frozenset(
    {
        "open",
        "cancel",
        "apply",
        "help",
        "add database",
        "add connection",
        "remove from list",
        "refresh status",
        "database manager",
        "ok",
        "close",
        "name",
    }
)
_TOOLBAR_AUTO_IDS = frozenset(
    {
        "m_PlotsToolbar",
        "m_GeneralToolBar",
        "m_obMainMenu",
        "m_PSMToolbar",
        "Main_Configuration_Templates",
        "m_c1ToolBar",
        "m_c1PlotGroupToolBar",
    }
)
VK_SHIFT = 0x10
VK_CONTROL = 0x11
VK_ESCAPE = 0x1B
VK_F10 = 0x79
KEYEVENTF_KEYUP = 0x0002
_FLEX_CELL = re.compile(r"^Row (\d+) Column (\d+)$")
_MANAGER_NAME_COL = 3
_MANAGER_PATH_COL = 4


def _set_clipboard(text: str) -> None:
    """Paste-safe path entry (send_keys treats ``\\i`` etc. as escape sequences)."""
    payload = text.replace("'", "''")
    subprocess.run(
        [
            "powershell",
            "-NoProfile",
            "-Command",
            f"Set-Clipboard -Value '{payload}'",
        ],
        check=True,
        capture_output=True,
        text=True,
    )


def _is_save_dialog(win) -> bool:
    title = win.window_text() or ""
    if not title or "Cursor" in title:
        return False
    return bool(re.search(r"^Export Plots$|^Save As$|^Save$", title))


def _save_dialog_filename_edit(dlg):
    """The File name box (auto_id 1001), not the address bar."""
    try:
        nodes = list(dlg.descendants())
    except Exception:
        return None
    for ctrl in nodes:
        auto = str(getattr(ctrl.element_info, "automation_id", "") or "")
        kind = (ctrl.friendly_class_name() or "").lower()
        if auto == "1001" and kind == "edit":
            return ctrl
    for ctrl in nodes:
        auto = str(getattr(ctrl.element_info, "automation_id", "") or "")
        if auto == "FileNameControlHost":
            try:
                for child in ctrl.descendants():
                    if (child.friendly_class_name() or "").lower() == "edit":
                        return child
            except Exception:
                pass
            return ctrl
    edits = [
        c
        for c in nodes
        if (c.friendly_class_name() or "").lower() == "edit"
        and (c.window_text() or "") == "File name:"
    ]
    return edits[-1] if edits else None


def _save_dialog_save_button(dlg):
    try:
        nodes = list(dlg.descendants())
    except Exception:
        return None
    for ctrl in nodes:
        if (ctrl.window_text() or "") != "Save":
            continue
        kind = (ctrl.friendly_class_name() or "").lower()
        if "button" not in kind:
            continue
        auto = str(getattr(ctrl.element_info, "automation_id", "") or "")
        if auto == "1":
            return ctrl
    for ctrl in nodes:
        if (ctrl.window_text() or "") != "Save":
            continue
        if "button" in (ctrl.friendly_class_name() or "").lower():
            return ctrl
    return None


def _find_save_dialog():
    """Export Plots save dialog is a child of ADRE, not a top-level window."""
    from pywinauto import Desktop

    for win in Desktop(backend="uia").windows():
        if _is_save_dialog(win) and _save_dialog_filename_edit(win) is not None:
            return win
    adre = _adre_top_window()
    if adre is None:
        return None
    try:
        for child in adre.children():
            if _is_save_dialog(child) and _save_dialog_filename_edit(child) is not None:
                return child
    except Exception:
        pass
    return None


def _confirm_file_replace() -> None:
    from pywinauto import Desktop

    labels = {"yes", "&yes"}
    roots = list(Desktop(backend="uia").windows())
    adre = _adre_top_window()
    if adre is not None:
        roots.append(adre)
    for root in roots:
        title = ""
        try:
            title = root.window_text() or ""
        except Exception:
            pass
        if "Cursor" in title:
            continue
        if title and not re.search(r"Confirm|Exists|Save As|Export Plots", title, re.I):
            if title.startswith("ADRE"):
                pass
            else:
                continue
        try:
            nodes = list(root.children()) + list(root.descendants())
        except Exception:
            continue
        for ctrl in nodes:
            text = (ctrl.window_text() or "").strip().casefold()
            if text.replace("&", "") != "yes":
                continue
            kind = (ctrl.friendly_class_name() or "").lower()
            if "button" not in kind:
                continue
            print("[adre] confirm replace Yes", flush=True)
            try:
                ctrl.click_input()
            except Exception:
                continue
            return


def _fill_save_dialog(dlg, full: str) -> None:
    """Type the CSV path into File name and click Save."""
    from pywinauto.keyboard import send_keys

    print(f"[adre] File name ← {full}", flush=True)
    _ensure_foreground(dlg)
    time.sleep(0.2)
    edit = _save_dialog_filename_edit(dlg)
    if edit is None:
        raise RecipeIncomplete("Export Plots dialog has no File name box")
    try:
        edit.set_focus()
    except Exception:
        pass
    try:
        edit.set_edit_text(full)
    except Exception:
        _set_clipboard(full)
        send_keys("^a^v")
    time.sleep(0.25)
    btn = _save_dialog_save_button(dlg)
    if btn is not None:
        btn.click_input()
    else:
        send_keys("{ENTER}")
    time.sleep(0.5)
    _confirm_file_replace()


def _ctrl_in_toolbar(ctrl) -> bool:
    try:
        current = ctrl
        for _ in range(14):
            current = current.parent()
            auto = str(getattr(current.element_info, "automation_id", "") or "")
            if auto in _TOOLBAR_AUTO_IDS:
                return True
    except Exception:
        return False
    return False


def _key_down(vk: int) -> None:
    ctypes.windll.user32.keybd_event(vk, 0, 0, 0)


def _key_up(vk: int) -> None:
    ctypes.windll.user32.keybd_event(vk, 0, KEYEVENTF_KEYUP, 0)


def _ensure_foreground(window, *, maximize: bool = False) -> None:
    """Bring a window to the front. Do not Restore — that un-maximizes HV."""
    hwnd = int(window.handle)
    user32 = ctypes.windll.user32
    if user32.IsIconic(hwnd):
        user32.ShowWindow(hwnd, 9)  # SW_RESTORE from minimized only
    elif maximize:
        user32.ShowWindow(hwnd, 3)  # SW_MAXIMIZE
        try:
            window.maximize()
        except Exception:
            pass
    else:
        user32.ShowWindow(hwnd, 5)  # SW_SHOW, keep size
    user32.SetForegroundWindow(hwnd)
    try:
        window.set_focus()
    except Exception:
        pass
    time.sleep(0.35)


def _titlebar_button(win, name: str):
    try:
        for child in win.children():
            kind = (child.friendly_class_name() or "").lower()
            if "titlebar" not in kind:
                continue
            for btn in child.children():
                if (btn.window_text() or "") == name:
                    return btn
    except Exception:
        return None
    return None


def _maximize_ctrl(ctrl) -> None:
    try:
        hwnd = int(ctrl.handle)
        ctypes.windll.user32.ShowWindow(hwnd, 3)
    except Exception:
        pass
    try:
        ctrl.maximize()
    except Exception:
        pass
    btn = _titlebar_button(ctrl, "Maximize")
    if btn is not None:
        try:
            btn.click_input()
            time.sleep(0.2)
        except Exception:
            pass


def _dismiss_adre_prompts() -> None:
    """Save-session / close prompts: click No so we do not keep HV open."""
    from pywinauto import Desktop

    labels = {"no", "&no", "don't save", "do not save", "no to all"}
    roots = list(Desktop(backend="uia").windows())
    adre = _adre_top_window()
    if adre is not None:
        roots.append(adre)
    for root in roots:
        title = ""
        try:
            title = root.window_text() or ""
        except Exception:
            pass
        if "Cursor" in title:
            continue
        try:
            nodes = list(root.children()) + list(root.descendants())
        except Exception:
            continue
        for ctrl in nodes:
            text = (ctrl.window_text() or "").strip().casefold()
            if text.replace("&", "") not in {s.replace("&", "") for s in labels} and text not in labels:
                continue
            kind = (ctrl.friendly_class_name() or "").lower()
            if "button" not in kind:
                continue
            print(f"[adre] dismiss prompt {ctrl.window_text()!r}", flush=True)
            try:
                ctrl.click_input()
            except Exception:
                continue
            time.sleep(0.3)
            return


def _close_plot_session(adre_window=None) -> None:
    """Close HV - Plot Session so Configuration Hierarchy is clickable again."""
    session = _plot_session_window(adre_window)
    if session is None:
        return
    print("[adre] close HV - Plot Session (it covers the database tree)", flush=True)
    _ensure_foreground(session)
    btn = _titlebar_button(session, "Close")
    if btn is not None:
        try:
            btn.click_input()
        except Exception:
            btn = None
    if btn is None:
        from pywinauto.keyboard import send_keys

        try:
            session.set_focus()
        except Exception:
            pass
        send_keys("%{F4}")
    time.sleep(0.4)
    _dismiss_adre_prompts()
    deadline = time.time() + 8
    while time.time() < deadline:
        if _plot_session_window(adre_window) is None:
            return
        _dismiss_adre_prompts()
        time.sleep(0.3)


def _click_control(window, ctrl) -> None:
    _ensure_foreground(window)
    ctrl.click_input()


def _uia_window_by_title(title_re: str):
    """Top-level UIA window whose title matches ``title_re`` (skips Cursor)."""
    from pywinauto import Desktop

    pat = re.compile(title_re, re.I)
    for win in Desktop(backend="uia").windows():
        text = win.window_text() or ""
        if not text or "Cursor" in text:
            continue
        if pat.search(text):
            return win
    return None


def _legacy_value(ctrl) -> str:
    try:
        return str((ctrl.legacy_properties() or {}).get("Value") or "").strip()
    except Exception:
        return ""


def _ctrl_visible(ctrl) -> bool:
    try:
        return bool(ctrl.is_visible())
    except Exception:
        return True


def _find_database_manager(window):
    """Database Manager is a child dialog of ADRE, not a top-level Desktop window."""
    for kwargs in (
        {"auto_id": "DatabaseManagerForm"},
        {"title": "Database Manager"},
    ):
        try:
            dlg = window.child_window(**kwargs)
            if dlg.exists(timeout=0.4):
                return dlg
        except Exception:
            continue
    try:
        for child in window.children():
            if (child.window_text() or "") == "Database Manager":
                return child
    except Exception:
        pass
    return None


def _click_button(window, title: str, auto_id: str | None = None) -> None:
    if auto_id:
        try:
            btn = window.child_window(auto_id=auto_id)
            if btn.exists(timeout=0.4):
                btn.click_input()
                return
        except Exception:
            pass
    kids = []
    try:
        kids = list(window.descendants())
    except Exception:
        pass
    buttons = []
    for ctrl in kids:
        if _item_text(ctrl) != title:
            continue
        kind = (ctrl.friendly_class_name() or "").lower()
        ctype = str(getattr(ctrl.element_info, "control_type", "") or "").lower()
        if "button" in kind or "button" in ctype:
            buttons.append(ctrl)
    if not buttons:
        raise RecipeIncomplete(
            f"Button {title!r} not found on {window.window_text()!r}"
        )
    buttons[0].click_input()


def _manager_row_score(text: str, name: str, path: str = "") -> int:
    """How well a Database Manager cell/row matches the vault database."""
    blob = (text or "").casefold()
    want = (name or "").casefold()
    folder = (path or "").replace("/", "\\").rstrip("\\").casefold()
    if not blob or not want:
        return 0
    score = 0
    if blob == want:
        score += 20
    elif blob.startswith(want + " ") or blob.startswith(want + "\t"):
        score += 14
    elif want in blob.split():
        score += 12
    elif want in blob:
        score += 8
    if folder:
        blob_path = blob.replace("/", "\\").rstrip("\\")
        if folder and folder in blob_path:
            score += 15
    return score


def _ctrl_label(ctrl) -> str:
    for getter in (
        lambda: ctrl.window_text(),
        lambda: getattr(ctrl.element_info, "name", None),
    ):
        try:
            val = getter()
        except Exception:
            val = None
        if val and str(val).strip():
            return str(val).strip()
    return ""


def _flexgrid_cells(dlg) -> dict[tuple[int, int], tuple[str, object]]:
    """Map (row, col) → (legacy Value, cell control) on C1 FlexGrid."""
    cells: dict[tuple[int, int], tuple[str, object]] = {}
    try:
        kids = list(dlg.descendants())
    except Exception:
        return cells
    for ctrl in kids:
        title = _ctrl_label(ctrl)
        match = _FLEX_CELL.fullmatch(title)
        if not match:
            continue
        row, col = int(match.group(1)), int(match.group(2))
        cells[(row, col)] = (_legacy_value(ctrl), ctrl)
    return cells


def _flex_cells_with_value(
    cells: dict[tuple[int, int], tuple[str, object]], value: str
) -> list[tuple[int, int]]:
    """Data rows whose FlexGrid Value equals ``value`` (the Variable column)."""
    want = (value or "").strip().casefold()
    if not want:
        return []
    hits: list[tuple[int, int]] = []
    for (row, col), (val, _ctrl) in sorted(cells.items()):
        if row == 0:
            continue
        if (val or "").strip().casefold() == want:
            hits.append((row, col))
    return hits


def _is_sync_waveform_label(val: str) -> bool:
    text = (val or "").strip().casefold()
    if not text or "async" in text:
        return False
    return text == "sync waveform" or text.endswith("sync waveform")


def _combo_item_is_choice(text: str, title: str) -> bool:
    """Exact list row only — not '#2 Async Waveform' or '#3 Async Waveform'."""
    got = (text or "").strip()
    want = (title or "").strip()
    if not got or not want:
        return False
    if got[:1] == "#":
        return False
    return got.casefold() == want.casefold()


def _plot_variable_column(
    cells: dict[tuple[int, int], tuple[str, object]],
) -> int | None:
    """Column with Sync/Async Waveform, not Compensation/Overlay Variable (Direct)."""
    scores: dict[int, int] = {}
    for (row, col), (val, _ctrl) in cells.items():
        text = (val or "").strip().casefold()
        if row == 0:
            if text == "variable":
                scores.setdefault(col, 0)
            continue
        if "waveform" in text:
            scores[col] = scores.get(col, 0) + 2
        elif text == "direct":
            scores[col] = scores.get(col, 0) - 1
    if not scores:
        return None
    return max(scores, key=scores.get)


def _wait_manager_grid(dlg, timeout: float = 12) -> dict[tuple[int, int], tuple[str, object]]:
    deadline = time.time() + timeout
    while time.time() < deadline:
        cells = _flexgrid_cells(dlg)
        names = [
            cells[(row, _MANAGER_NAME_COL)][0]
            for row in {r for r, _c in cells}
            if row > 0 and (row, _MANAGER_NAME_COL) in cells and cells[(row, _MANAGER_NAME_COL)][0]
        ]
        if names:
            return cells
        time.sleep(0.35)
    return _flexgrid_cells(dlg)


def _manager_names(cells: dict[tuple[int, int], tuple[str, object]]) -> list[str]:
    names = []
    for row in sorted({r for r, _c in cells}):
        if row == 0:
            continue
        val = cells.get((row, _MANAGER_NAME_COL), ("", None))[0]
        if val:
            names.append(val)
    return names


def _select_manager_row(dlg, name: str, path: str, *, adre_window=None) -> None:
    """Click the FlexGrid row whose Name / Path matches the vault database."""
    cells = _wait_manager_grid(dlg)
    rows = sorted({row for row, _col in cells})
    best_row = None
    best_score = 0
    for row in rows:
        if row == 0:
            continue
        name_val = cells.get((row, _MANAGER_NAME_COL), ("", None))[0]
        path_val = cells.get((row, _MANAGER_PATH_COL), ("", None))[0]
        score = _manager_row_score(name_val, name, "")
        if path_val:
            score += _manager_row_score(path_val, name, path)
        if score > best_score:
            best_score = score
            best_row = row
    if best_row is None or best_score < 8:
        listed = _manager_names(cells)
        raise RecipeIncomplete(
            f"Database {name!r} is not in Database Manager. "
            f"Listed: {listed or '(grid empty — is ADRE in front?)'}"
        )
    cell = cells.get((best_row, _MANAGER_NAME_COL), ("", None))[1]
    if cell is None:
        cell = cells.get((best_row, _MANAGER_PATH_COL), ("", None))[1]
    if cell is None:
        raise RecipeIncomplete(f"FlexGrid cell for {name!r} was empty")
    print(
        f"[adre] Database Manager row {best_row}: "
        f"{cells.get((best_row, _MANAGER_NAME_COL), ('', None))[0]}",
        flush=True,
    )
    if adre_window is not None:
        _click_control(adre_window, cell)
    else:
        cell.click_input()


def _click_general_toolbar_open(window) -> None:
    """Toolbar Open on m_GeneralToolBar — not File menu, not Cursor."""
    _ensure_foreground(window)
    bar = window.child_window(auto_id="m_GeneralToolBar")
    for kwargs in (
        {"title": "Open", "control_type": "MenuItem"},
        {"title": "Open"},
    ):
        try:
            btn = bar.child_window(**kwargs)
            if btn.exists(timeout=0.5):
                _click_control(window, btn)
                return
        except Exception:
            continue
    raise RecipeIncomplete("ADRE toolbar Open (m_GeneralToolBar) not found")


def _click_manager_toolbar(dlg, adre_window, title: str) -> None:
    _ensure_foreground(adre_window)
    bar = dlg.child_window(auto_id="m_c1ToolBar")
    btn = bar.child_window(title=title, control_type="MenuItem")
    if not btn.exists(timeout=0.5):
        btn = bar.child_window(title=title)
    _click_control(adre_window, btn)


def _open_database_manager_dialog(window):
    """Open Database Manager via the visible toolbar Open (File submenu is hidden)."""
    _ensure_foreground(window)
    dlg = _find_database_manager(window)
    if dlg is not None:
        try:
            if dlg.is_visible():
                _ensure_foreground(window)
                return dlg
        except Exception:
            pass
    _click_general_toolbar_open(window)
    deadline = time.time() + 15
    while time.time() < deadline:
        dlg = _find_database_manager(window)
        if dlg is not None:
            try:
                if dlg.is_visible():
                    _wait_manager_grid(dlg, timeout=3)
                    return dlg
            except Exception:
                return dlg
        time.sleep(0.3)
    raise RecipeIncomplete(
        "Database Manager did not open — move Cursor so it does not cover ADRE, "
        "then re-run."
    )


def _hierarchy_db_names(window) -> list[str]:
    try:
        w32 = _win32_window(window)
        tv = _treeview_named(w32, "Configuration Hierarchy")
        root = tv.roots()[0]
        _tv_expand(root)
        return [_tv_text(c) for c in _tv_children(root) if _tv_text(c)]
    except Exception:
        return []


def _hierarchy_find_db(window, db_name: str):
    try:
        w32 = _win32_window(window)
        tv = _treeview_named(w32, "Configuration Hierarchy")
        root = tv.roots()[0]
        _tv_expand(root)
        return _tv_find_child(root, db_name), root, tv
    except Exception:
        return None, None, None


def _wait_db_in_hierarchy(window, db_name: str, timeout: float = 90):
    deadline = time.time() + timeout
    while time.time() < deadline:
        item, _, _ = _hierarchy_find_db(window, db_name)
        if item is not None:
            return item
        time.sleep(1)
    return None


def _load_database_from_manager(window, db_name: str, db_path: str) -> None:
    """Select ``db_name`` in Database Manager and click Open so it enters the tree."""
    from pywinauto.keyboard import send_keys

    dlg = _open_database_manager_dialog(window)
    cells = _wait_manager_grid(dlg)
    listed = _manager_names(cells)
    try:
        _select_manager_row(dlg, db_name, db_path, adre_window=window)
    except RecipeIncomplete as exc:
        if db_name in listed:
            raise RecipeIncomplete(
                f"{db_name!r} is listed in Database Manager but could not be "
                f"selected: {exc}"
            ) from exc
        print(f"[adre] {db_name} not in Database Manager — Add Database {db_path}", flush=True)
        _click_manager_toolbar(dlg, window, "Add Database")
        time.sleep(1.5)
        _ensure_foreground(window)
        send_keys("^l")
        time.sleep(0.4)
        send_keys(db_path.replace(" ", "{SPACE}"), with_spaces=True)
        send_keys("{ENTER}")
        time.sleep(3)
        dlg = _find_database_manager(window) or dlg
        _select_manager_row(dlg, db_name, db_path, adre_window=window)
    time.sleep(0.35)
    print(f"[adre] Database Manager: Open {db_name}", flush=True)
    try:
        btn = dlg.child_window(auto_id="m_OpenButton")
        _click_control(window, btn)
    except Exception:
        _ensure_foreground(window)
        send_keys("{ENTER}")
    deadline = time.time() + 30
    while time.time() < deadline:
        dlg = _find_database_manager(window)
        if dlg is None:
            break
        try:
            if not dlg.is_visible():
                break
        except Exception:
            break
        time.sleep(0.4)


def _window_meta(win) -> tuple[str, str, str, str]:
    """title, class_name, automation_id, friendly_class_name (lower)."""
    text = cls = auto = kind = ""
    try:
        text = win.window_text() or ""
    except Exception:
        pass
    try:
        cls = str(getattr(win.element_info, "class_name", "") or "")
    except Exception:
        try:
            cls = win.class_name() or ""
        except Exception:
            cls = ""
    try:
        auto = str(getattr(win.element_info, "automation_id", "") or "")
    except Exception:
        auto = ""
    try:
        kind = (win.friendly_class_name() or "").lower()
    except Exception:
        kind = ""
    return text, cls, auto, kind


def _is_popup_search_window(
    title: str,
    class_name: str = "",
    auto_id: str = "",
    friendly: str = "",
) -> bool:
    """True if this top-level window might host Plots / Tabular List / Timebase.

    WinForms context menus are class ``#32768`` and often inherit the ADRE
    title. Skipping every window whose title starts with ADRE hid Plots.
    """
    if "Cursor" in (title or ""):
        return False
    if (auto_id or "") == "PlotSessionWindow":
        return False
    cls = class_name or ""
    kind = (friendly or "").lower()
    if cls == "#32768" or "menu" in kind:
        return True
    if not title:
        return True
    if title.startswith("ADRE") or "Plot Session" in title:
        return False
    return True


def _click_named(
    window, title: str, *, desktop: bool = True, visible_only: bool = False,
    skip_toolbars: bool = False,
) -> None:
    """Click a named control on ``window``. Do not scan Cursor; prefer visible items."""

    def collect(root) -> list:
        try:
            return list(root.descendants())
        except Exception:
            return []

    def pick(candidates):
        visible_menu, visible, menu, any_ = [], [], [], []
        for ctrl in candidates:
            if _item_text(ctrl) != title:
                continue
            if skip_toolbars and _ctrl_in_toolbar(ctrl):
                continue
            kind = (ctrl.friendly_class_name() or "").lower()
            vis = _ctrl_visible(ctrl)
            if visible_only and not vis:
                continue
            is_menu = "menu" in kind
            any_.append(ctrl)
            if is_menu:
                menu.append(ctrl)
            if vis:
                visible.append(ctrl)
            if vis and is_menu:
                visible_menu.append(ctrl)
        for group in (visible_menu, visible, menu, any_):
            if group:
                return group[0]
        return None

    time.sleep(0.2)
    hit = None
    if desktop:
        from pywinauto import Desktop

        extra = []
        for pop in Desktop(backend="uia").windows():
            text, cls, auto, kind = _window_meta(pop)
            if not _is_popup_search_window(text, cls, auto, kind):
                continue
            extra.extend(collect(pop))
        hit = pick(extra)
    if hit is None:
        hit = pick(collect(window))
    if hit is None:
        raise RecipeIncomplete(f"Menu item {title!r} not found")
    hit.click_input()


def _click_win32_popup(title: str) -> bool:
    """Win32 ``#32768`` menus are sometimes invisible to the UIA backend."""
    try:
        from pywinauto import Desktop

        desk = Desktop(backend="win32")
    except Exception:
        return False
    for pop in desk.windows():
        try:
            if pop.class_name() != "#32768":
                continue
        except Exception:
            continue
        try:
            for ctrl in pop.children():
                if _item_text(ctrl) == title:
                    print(f"[adre] menu {title!r} (win32)", flush=True)
                    ctrl.click_input()
                    return True
        except Exception:
            pass
        try:
            pop.menu_select(title)
            print(f"[adre] menu {title!r} (win32 select)", flush=True)
            return True
        except Exception:
            continue
    return False


def _click_popup_item(title: str, timeout: float = 5, adre_window=None) -> None:
    """Click Plots / Tabular List / Timebase on the right-click menu."""
    from pywinauto import Desktop

    def consider(ctrl) -> bool:
        if _item_text(ctrl) != title:
            return False
        if not _ctrl_visible(ctrl):
            return False
        if _ctrl_in_toolbar(ctrl):
            return False
        return True

    def scan(root) -> bool:
        try:
            nodes = list(root.children()) + list(root.descendants())
        except Exception:
            try:
                nodes = list(root.children())
            except Exception:
                return False
        for ctrl in nodes:
            if consider(ctrl):
                print(f"[adre] menu {title!r}", flush=True)
                ctrl.click_input()
                return True
        return False

    deadline = time.time() + timeout
    while time.time() < deadline:
        if _click_win32_popup(title):
            return
        for pop in Desktop(backend="uia").windows():
            text, cls, auto, kind = _window_meta(pop)
            if not _is_popup_search_window(text, cls, auto, kind):
                continue
            if scan(pop):
                return
        if adre_window is not None:
            try:
                for child in adre_window.children():
                    text, cls, auto, kind = _window_meta(child)
                    if auto in _TOOLBAR_AUTO_IDS or auto == "m_obMainMenu":
                        continue
                    if text == "Menu Bar" or text.startswith("HV -"):
                        continue
                    if auto == "PlotSessionWindow":
                        continue
                    if not (cls == "#32768" or "menu" in kind):
                        continue
                    if scan(child):
                        return
            except Exception:
                pass
        time.sleep(0.1)
    if adre_window is not None:
        _click_named(adre_window, title, desktop=True, skip_toolbars=True)
        print(f"[adre] menu {title!r} (ADRE tree)", flush=True)
        return
    raise RecipeIncomplete(f"Menu item {title!r} not found")


_HV_TOP_TOOLBAR_TITLES = frozenset(
    {
        "create replay database",
        "data source",
        "reject",
        "colors",
    }
)


def _ctrl_top(ctrl) -> int:
    try:
        return int(ctrl.rectangle().top)
    except Exception:
        return 0


def _in_hv_top_toolbar(ctrl) -> bool:
    """True for the large HV - Plot Session Export Plots (Print / Replay row)."""
    try:
        current = ctrl
        for _ in range(10):
            current = current.parent()
            titles = {_item_text(child).casefold() for child in current.children()}
            if titles & _HV_TOP_TOOLBAR_TITLES:
                return True
    except Exception:
        return False
    return False


def _plot_group_root(session, contains: str | None):
    """PlotGroupWindow inside HV - Plot Session (auto_id=PlotGroupWindow)."""
    matches = []
    others = []
    try:
        nodes = list(session.descendants())
    except Exception:
        nodes = []
    for ctrl in nodes:
        auto = str(getattr(ctrl.element_info, "automation_id", "") or "")
        if auto != "PlotGroupWindow":
            continue
        text = ctrl.window_text() or ""
        if contains and contains.casefold() in text.casefold():
            matches.append(ctrl)
        else:
            others.append(ctrl)
    if matches:
        return matches[-1]
    if others:
        return others[-1]
    hit = _by_auto_id(session, "PlotGroupWindow")
    return hit if hit is not None else session


def _click_plot_export(session, contains: str | None = None) -> None:
    """Click m_c1PlotGroupToolBar Export Plots (bottom of plot), not HV top bar."""
    adre = _adre_top_window()
    if adre is not None:
        _ensure_foreground(adre)
    _ensure_foreground(session, maximize=True)
    group = _plot_group_root(session, contains)
    if group is not session:
        _maximize_ctrl(group)
        time.sleep(0.45)
        group = _plot_group_root(session, contains)
    bar = _by_auto_id(group, "m_c1PlotGroupToolBar")
    if bar is None:
        bar = _by_auto_id(session, "m_c1PlotGroupToolBar")
    if bar is None:
        raise RecipeIncomplete(
            "Plot-group toolbar m_c1PlotGroupToolBar not found. "
            "That is the small Export Plots icon on the plot, not the HV top button."
        )
    btn = None
    if callable(getattr(bar, "child_window", None)):
        try:
            spec = bar.child_window(title="Export Plots")
            if spec.exists(timeout=0.8):
                btn = spec
        except Exception:
            btn = None
    if btn is None:
        try:
            nodes = list(bar.descendants()) + list(bar.children())
        except Exception:
            nodes = []
        hits = [c for c in nodes if _item_text(c) == "Export Plots" and _ctrl_visible(c)]
        if hits:
            btn = max(hits, key=_ctrl_top)
    if btn is None:
        raise RecipeIncomplete(
            "Export Plots on m_c1PlotGroupToolBar not found (bottom plot icon)."
        )
    print(
        "[adre] click m_c1PlotGroupToolBar Export Plots "
        f"(plot bottom icon, y={_ctrl_top(btn)})",
        flush=True,
    )
    try:
        btn.wait("enabled", timeout=20)
    except Exception:
        pass
    _ensure_foreground(session, maximize=True)
    time.sleep(0.15)
    btn.click_input()


_PLOT_CONFIG_DIALOG = "Timebase Plot Group Configuration"
_PLOT_CONTEXT_MARKERS = (
    "plotsession",
    "set sample as reference",
    "plot flagged data",
    "add plot",
)


def _click_combo_item(title: str, timeout: float = 1.5) -> bool:
    """Click an open combo-list row. Ignore FlexGrid cells with the same value."""
    from pywinauto import Desktop

    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            pops = Desktop(backend="uia").windows()
        except Exception:
            pops = []
        for pop in pops:
            try:
                rect = pop.rectangle()
                height = int(rect.bottom) - int(rect.top)
                width = int(rect.right) - int(rect.left)
            except Exception:
                continue
            if height > 420 or width > 700:
                continue
            try:
                nodes = list(pop.children()) + list(pop.descendants())
            except Exception:
                continue
            for ctrl in nodes:
                text = _item_text(ctrl)
                if not _combo_item_is_choice(text, title):
                    continue
                if _FLEX_CELL.fullmatch(text or ""):
                    continue
                if not _ctrl_visible(ctrl):
                    continue
                kind = (ctrl.friendly_class_name() or "").lower()
                if kind and not any(
                    token in kind
                    for token in ("list", "menu", "item", "combo", "text", "data", "pane")
                ):
                    continue
                rect = _cell_rect(ctrl)
                if rect is None:
                    continue
                _mouse_click_xy(
                    (int(rect.left) + int(rect.right)) // 2,
                    (int(rect.top) + int(rect.bottom)) // 2,
                )
                return True
        time.sleep(0.04)
    return False


def _click_dropdown_choice(title: str, root=None, timeout: float = 5) -> None:
    """Click Async Waveform (etc.) in a ComboBox / context list."""
    if _click_combo_item(title, timeout=min(timeout, 2.0)):
        return
    try:
        _click_popup_item(title, timeout=min(timeout, 2.5), adre_window=root)
        return
    except RecipeIncomplete:
        pass
    raise RecipeIncomplete(f"Dropdown item {title!r} not found")


def _find_plot_config_dialog(adre=None, session=None, timeout: float = 12):
    deadline = time.time() + timeout
    while time.time() < deadline:
        for root in (session, adre):
            if root is None:
                continue
            try:
                for child in root.children():
                    if (child.window_text() or "") == _PLOT_CONFIG_DIALOG:
                        return child
            except Exception:
                pass
        try:
            from pywinauto import Desktop

            for win in Desktop(backend="uia").windows():
                if (win.window_text() or "") == _PLOT_CONFIG_DIALOG:
                    return win
        except Exception:
            pass
        time.sleep(0.25)
    return None


def _config_splitter_target_y(top: int, bottom: int) -> int:
    """Y for a fully lowered gripper — just above the OK row."""
    height = max(1, int(bottom) - int(top))
    return int(top) + int(height * 0.92)


def _union_rect(rects: list) -> object | None:
    if not rects:
        return None

    class _R:
        pass

    box = _R()
    box.left = min(int(r.left) for r in rects)
    box.top = min(int(r.top) for r in rects)
    box.right = max(int(r.right) for r in rects)
    box.bottom = max(int(r.bottom) for r in rects)
    return box


def _flexgrid_bounds(dlg):
    """Union of every Row/Column cell — includes the lower property list too."""
    return _cells_bounds(_flexgrid_cells(dlg))


def _cells_bounds(cells: dict) -> object | None:
    rects = []
    for _key, (_val, ctrl) in cells.items():
        if ctrl is None:
            continue
        rect = _cell_rect(ctrl)
        if rect is not None:
            rects.append(rect)
    return _union_rect(rects)


def _channel_grid_cells(cells: dict) -> dict:
    """Upper Timebase channel FlexGrid only (many columns). Not the 2-col options list."""
    if not cells:
        return {}
    max_col = max(col for _row, col in cells)
    if max_col >= 3:
        return {key: val for key, val in cells.items() if key[1] >= 2}
    return cells


def _channel_grid_bounds(dlg):
    return _cells_bounds(_channel_grid_cells(_flexgrid_cells(dlg)))


def _win32_child_bars(dlg) -> list[tuple[int, int, int, int, str]]:
    """Child HWNDs that look like a horizontal splitter (wide, few pixels tall)."""
    try:
        hwnd = int(dlg.handle)
    except Exception:
        return []
    user32 = ctypes.windll.user32
    found: list[tuple[int, int, int, int, str]] = []
    buf = ctypes.create_unicode_buffer(256)
    wnd_enum = ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_void_p, ctypes.c_void_p)

    def _cb(child, _lp):
        user32.GetClassNameW(child, buf, 256)
        rect = wintypes.RECT()
        if not user32.GetWindowRect(child, ctypes.byref(rect)):
            return True
        width, height = rect.right - rect.left, rect.bottom - rect.top
        cls = buf.value
        name = cls.upper()
        thin = width > max(height * 4, 200) and 1 <= height <= 20
        named = "SPLITTER" in name or "SPLITCONTAINER" in name
        if thin or named:
            mid_y = (rect.top + rect.bottom) // 2
            found.append((mid_y, width, height, (rect.left + rect.right) // 2, cls))
        return True

    cb = wnd_enum(_cb)
    user32.EnumChildWindows(hwnd, cb, 0)
    return found


def _win32_horizontal_splitter_y(dlg) -> int | None:
    """Native splitter HWND — UIA often never sees this gripper."""
    bars = _win32_child_bars(dlg)
    if not bars:
        return None
    return int(sorted(bars, key=lambda row: row[2])[0][0])


def _cursor_is(idc: int) -> bool:
    class CURSORINFO(ctypes.Structure):
        _fields_ = (
            ("cbSize", wintypes.DWORD),
            ("flags", wintypes.DWORD),
            ("hCursor", wintypes.HANDLE),
            ("ptScreenPos", wintypes.POINT),
        )

    info = CURSORINFO()
    info.cbSize = ctypes.sizeof(CURSORINFO)
    if not ctypes.windll.user32.GetCursorInfo(ctypes.byref(info)):
        return False
    loaded = ctypes.windll.user32.LoadCursorW(None, idc)
    return int(info.hCursor or 0) == int(loaded or 0)


def _move_cursor(x: int, y: int) -> None:
    ctypes.windll.user32.SetCursorPos(int(x), int(y))


def _hunt_cursor(xs: list[int], ys: list[int], idc: int) -> tuple[int, int] | None:
    """Hover until Windows shows the resize cursor — that pixel is the gripper."""
    seen: set[tuple[int, int]] = set()
    for y in ys:
        for x in xs:
            pt = (int(x), int(y))
            if pt in seen:
                continue
            seen.add(pt)
            _move_cursor(pt[0], pt[1])
            time.sleep(0.05)
            if _cursor_is(idc):
                return pt
    return None


_OPTION_PANE_MARKERS = (
    "scaling",
    "dccoupled",
    "dc coupled",
    "plotresolution",
    "plot resolution",
    "wrappedtimebase",
    "nameoffield",
    "name of field",
    "cursors",
    "headers",
)


def _options_pane_top(dlg, host) -> int | None:
    """Top of the lower property list (Scaling / Name / DC Coupled)."""
    if host is None:
        return None
    floor = int(host.top) + int((int(host.bottom) - int(host.top)) * 0.22)
    best = None
    try:
        nodes = list(dlg.descendants())
    except Exception:
        return None
    for ctrl in nodes:
        text = ((_item_text(ctrl) or "") + " " + (_legacy_value(ctrl) or "")).casefold()
        compact = text.replace(" ", "")
        if not any(marker.replace(" ", "") in compact for marker in _OPTION_PANE_MARKERS):
            continue
        rect = _cell_rect(ctrl)
        if rect is None or int(rect.top) < floor:
            continue
        top = int(rect.top)
        if best is None or top < best:
            best = top
    return best


def _config_splitter_y(dlg, host) -> int | None:
    """Pixel row of the bar between the channel FlexGrid and the options list."""
    native = _win32_horizontal_splitter_y(dlg)
    if native is not None:
        return native
    grid = _channel_grid_bounds(dlg)
    options_top = _options_pane_top(dlg, host)
    if grid is not None and options_top is not None:
        gap = options_top - int(grid.bottom)
        if -20 <= gap <= 120:
            return int(grid.bottom) + max(3, min(8, gap // 2))
    if grid is not None:
        return int(grid.bottom) + 4
    if options_top is not None:
        return options_top - 4
    return None


def _drag_xy(x0: int, y0: int, x1: int, y1: int) -> None:
    """Hold-and-slide via SetCursorPos. pywinauto press/move often never captures WinForms."""
    user32 = ctypes.windll.user32
    x0, y0, x1, y1 = int(x0), int(y0), int(x1), int(y1)
    _move_cursor(x0, y0)
    time.sleep(0.05)
    user32.mouse_event(_MOUSEEVENTF_LEFTDOWN, 0, 0, 0, 0)
    time.sleep(0.04)
    _move_cursor(x0, y0 + 2)
    steps = 8
    for i in range(1, steps + 1):
        _move_cursor(x0 + (x1 - x0) * i / steps, y0 + (y1 - y0) * i / steps)
        time.sleep(0.01)
    user32.mouse_event(_MOUSEEVENTF_LEFTUP, 0, 0, 0, 0)
    time.sleep(0.08)


def _splitter_score(ctrl, host) -> float:
    """Higher = more like the horizontal SplitContainer bar."""
    rect = _cell_rect(ctrl)
    if rect is None or host is None:
        return -1.0
    width = int(rect.right) - int(rect.left)
    height = int(rect.bottom) - int(rect.top)
    host_w = int(host.right) - int(host.left)
    host_h = int(host.bottom) - int(host.top)
    if width < 40 or height < 2 or height > 28:
        return -1.0
    if host_w > 0 and width < host_w * 0.45:
        return -1.0
    auto = str(getattr(getattr(ctrl, "element_info", None), "automation_id", "") or "").lower()
    kind = (ctrl.friendly_class_name() or "").lower()
    name = (_item_text(ctrl) or "").lower()
    score = 1.0 + (width / max(host_w, 1.0))
    if any(token in auto for token in ("splitter", "splitcontainer", "split")):
        score += 8.0
    if any(token in kind for token in ("splitter", "thumb", "gripper")):
        score += 6.0
    if "splitter" in name:
        score += 4.0
    mid = (int(rect.top) + int(rect.bottom)) / 2
    if host_h > 0:
        frac = (mid - int(host.top)) / host_h
        if 0.25 <= frac <= 0.85:
            score += 2.0
        if frac < 0.12 or frac > 0.92:
            score -= 4.0
    return score


def _find_config_splitter(dlg):
    host = _cell_rect(dlg)
    best = None
    best_score = 2.5
    try:
        nodes = list(dlg.descendants()) + list(dlg.children())
    except Exception:
        return None
    for ctrl in nodes:
        score = _splitter_score(ctrl, host)
        if score > best_score:
            best_score = score
            best = ctrl
    return best


def _config_splitter_candidates(dlg, host) -> tuple[int, str, list[int]]:
    """Best guess Y plus nearby rows to hover for the SizeNS cursor."""
    grid = _channel_grid_bounds(dlg)
    options_top = _options_pane_top(dlg, host)
    bars = _win32_child_bars(dlg)
    ys: list[int] = []
    source = "scan"
    start_y = None
    if bars:
        start_y = int(sorted(bars, key=lambda row: row[2])[0][0])
        source = "win32 bar"
        ys.append(start_y)
    if grid is not None and options_top is not None:
        seam = (int(grid.bottom) + int(options_top)) // 2
        if start_y is None:
            start_y, source = seam, "grid/options seam"
        ys.extend(range(int(grid.bottom) - 1, int(options_top) + 3))
    elif grid is not None:
        if start_y is None:
            start_y, source = int(grid.bottom) + 2, "channel-grid bottom"
        ys.extend(range(int(grid.bottom) - 1, int(grid.bottom) + 14))
    elif options_top is not None:
        if start_y is None:
            start_y, source = options_top - 3, "options-pane top"
        ys.extend(range(options_top - 12, options_top + 2))
    if start_y is None:
        splitter = _find_config_splitter(dlg)
        rect = _cell_rect(splitter) if splitter is not None else None
        if rect is not None:
            start_y = (int(rect.top) + int(rect.bottom)) // 2
            source = "uia splitter"
            ys.append(start_y)
    if start_y is None:
        start_y = (int(host.top) + int(host.bottom)) // 2
        source = "dialog mid"
    mid = (int(host.top) + int(host.bottom)) // 2
    ys.extend(range(mid - 40, mid + 41, 4))
    # unique, keep order
    ordered: list[int] = []
    for y in [start_y, *ys]:
        if y not in ordered:
            ordered.append(int(y))
    return int(start_y), source, ordered


def _widen_options_name_column(dlg) -> None:
    """Property-grid Name column is crushed (Nameoffield). Drag its vertical bar right."""
    host = _cell_rect(dlg)
    if host is None:
        return
    cells = _flexgrid_cells(dlg)
    floor = int(host.top) + int((int(host.bottom) - int(host.top)) * 0.35)
    rights: list[int] = []
    tops: list[int] = []
    bottoms: list[int] = []
    for (row, col), (_val, ctrl) in cells.items():
        if col != 0 or ctrl is None:
            continue
        rect = _cell_rect(ctrl)
        if rect is None or int(rect.top) < floor:
            continue
        rights.append(int(rect.right))
        tops.append(int(rect.top))
        bottoms.append(int(rect.bottom))
    if not rights:
        return
    grab_x = max(rights)
    grab_y = (min(tops) + max(bottoms)) // 2
    target_x = min(int(host.left) + 280, int(host.right) - 360)
    if target_x <= grab_x + 20:
        return
    xs = [grab_x - 2, grab_x, grab_x + 2, grab_x + 4]
    hit = _hunt_cursor(xs, [grab_y, grab_y + 18, grab_y - 18], _IDC_SIZEWE)
    if hit is None:
        hit = (grab_x, grab_y)
    print(f"[adre] widen options Name column ({hit[0]} → {target_x})", flush=True)
    _drag_xy(hit[0], hit[1], target_x, hit[1])


def _lower_config_splitter(dlg) -> bool:
    """Drag the bar all the way down, just above OK, so the lower grid half is visible."""
    _enable_dpi_awareness()
    _ensure_foreground(dlg)
    host = _cell_rect(dlg)
    if host is None:
        return False
    options = _options_pane_top(dlg, host)
    native = _win32_horizontal_splitter_y(dlg)
    mid = (int(host.top) + int(host.bottom)) // 2
    if options is not None:
        start_y = int(options) - 4
    elif native is not None and int(host.top) + 80 < native < int(host.bottom) - 80:
        start_y = native
    else:
        start_y = mid
    target_y = int(host.bottom) - 52
    grab_x = int(host.left) + 72
    print(f"[adre] lower config splitter completely ({start_y} → {target_y})", flush=True)
    _drag_xy(grab_x, start_y, grab_x, target_y)
    after = _win32_horizontal_splitter_y(dlg)
    if after is not None and after < target_y - 50:
        print(f"[adre] splitter still high at {after}; second drag", flush=True)
        _drag_xy(grab_x, after, grab_x, target_y)
    return True


def _click_lower_option(dlg, *markers: str) -> bool:
    """Click a row in the lower options pane (Select All, Scaling, …)."""
    host = _cell_rect(dlg)
    if host is None:
        return False
    floor = int(host.top) + int((int(host.bottom) - int(host.top)) * 0.30)
    wants = [m.replace(" ", "") for m in markers]
    try:
        nodes = list(dlg.descendants())
    except Exception:
        return False
    for ctrl in nodes:
        compact = (
            ((_item_text(ctrl) or "") + " " + (_legacy_value(ctrl) or "")).casefold().replace(" ", "")
        )
        if not any(want in compact for want in wants):
            continue
        rect = _cell_rect(ctrl)
        if rect is None or int(rect.top) < floor:
            continue
        x = int(rect.right) + 24 if int(rect.right) - int(rect.left) < 180 else (
            int(rect.left) + int(rect.right)
        ) // 2
        y = (int(rect.top) + int(rect.bottom)) // 2
        print(f"[adre] click lower option {markers[0]!r} at ({x},{y})", flush=True)
        _mouse_click_xy(x, y)
        time.sleep(0.08)
        return True
    return False


def _select_lower_half_options(dlg) -> None:
    """Select All in the lower pane so the other half of the grid is included."""
    if _click_lower_option(dlg, "select all", "selectall"):
        return
    print("[adre] Select All not found in lower options", flush=True)


def _expand_dialog_trees(dlg) -> None:
    try:
        nodes = list(dlg.descendants())
    except Exception:
        return
    for ctrl in nodes:
        if not (_is_tree(ctrl) or _is_tree_item(ctrl)):
            continue
        _expand(ctrl)
        try:
            ctrl.expand()
        except Exception:
            pass


def _named_value_controls(dlg, value: str) -> list:
    want = value.casefold()
    hits = []
    try:
        nodes = list(dlg.descendants())
    except Exception:
        return hits
    for ctrl in nodes:
        text = (_legacy_value(ctrl) or _item_text(ctrl)).strip()
        if text.casefold() != want:
            continue
        kind = (ctrl.friendly_class_name() or "").lower()
        if "menu" in kind:
            continue
        if text in {"OK", "Cancel", "Apply", "Help"}:
            continue
        if _FLEX_CELL.fullmatch(_item_text(ctrl) or ""):
            hits.append(ctrl)
            continue
        if any(token in kind for token in ("combo", "edit", "data", "custom", "pane")):
            hits.append(ctrl)
    return hits


def _cell_rect(ctrl):
    """FlexGrid UIA rectangle. Never Invoke the cell — that raises COMError."""
    try:
        return ctrl.rectangle()
    except Exception:
        return None


def _mouse_click_xy(x: int, y: int) -> None:
    from pywinauto.mouse import click as mouse_click

    mouse_click(coords=(int(x), int(y)))


def _variable_col_span(cells: dict, col: int | None) -> tuple[int, int] | None:
    """Left/right of the Variable column. Prefer a narrow cell — some UIA rects are the whole row."""
    if col is None:
        return None
    widths: list[tuple[int, int, int]] = []
    for (row, c), (_val, ctrl) in cells.items():
        if c != col or ctrl is None:
            continue
        rect = _cell_rect(ctrl)
        if rect is None:
            continue
        width = int(rect.right) - int(rect.left)
        if width < 16:
            continue
        widths.append((width, int(rect.left), int(rect.right)))
    if not widths:
        return None
    widths.sort()
    _w, left, right = widths[0]
    if right - left > 420:
        return None
    return left, right


def _click_cell_dropdown(ctrl, col_span: tuple[int, int] | None = None) -> bool:
    """Click the Variable combo arrow, not the Name cell."""
    rect = _cell_rect(ctrl)
    if rect is None:
        return False
    y = (int(rect.top) + int(rect.bottom)) // 2
    if col_span is not None:
        x = int(col_span[1]) - 4
    else:
        width = int(rect.right) - int(rect.left)
        if width < 16:
            return False
        x = int(rect.right) - 4
    _mouse_click_xy(x, y)
    return True


def _click_cell_body(ctrl, col_span: tuple[int, int] | None = None) -> bool:
    """Click the Variable cell face so the combo attaches to this row."""
    rect = _cell_rect(ctrl)
    if rect is None:
        return False
    y = (int(rect.top) + int(rect.bottom)) // 2
    if col_span is not None:
        left, right = col_span
        x = left + max(12, (right - left) // 3)
    else:
        width = int(rect.right) - int(rect.left)
        x = int(rect.left) + min(28, max(10, width // 3))
    _mouse_click_xy(x, y)
    return True


def _rect_in_box(rect, box, *, pad_top: int = 4, pad_bottom: int = 10) -> bool:
    if rect is None or box is None:
        return False
    return (
        int(rect.top) >= int(box.top) + pad_top
        and int(rect.bottom) <= int(box.bottom) - pad_bottom
    )


def _grid_viewport(dlg):
    """Visible channel-grid strip — above the splitter, not the whole dialog."""
    host = _cell_rect(dlg)
    if host is None:
        return None

    class _R:
        pass

    box = _R()
    box.left = int(host.left)
    box.right = int(host.right)
    box.top = int(host.top) + 32
    bottom = int(host.bottom) - 48
    split = _config_splitter_y(dlg, host)
    options = _options_pane_top(dlg, host)
    grid = _channel_grid_bounds(dlg)
    if split is not None:
        bottom = min(bottom, int(split) - 2)
    if options is not None:
        bottom = min(bottom, int(options) - 2)
    if grid is not None:
        box.left = int(grid.left)
        box.right = int(grid.right)
        box.top = max(box.top, int(grid.top))
    box.bottom = max(box.top + 40, bottom)
    return box


def _grid_wheel_point(dlg) -> tuple[int, int] | None:
    """Left side of the channel grid (Name column) — never the Variable combo."""
    box = _grid_viewport(dlg)
    if box is None:
        return None
    return (int(box.left) + 36, (int(box.top) + int(box.bottom)) // 2)


def _win32_vscrollbars(dlg) -> list[tuple[int, int, int, int]]:
    """Vertical scrollbar HWNDs as (left, top, right, bottom)."""
    try:
        hwnd = int(dlg.handle)
    except Exception:
        return []
    user32 = ctypes.windll.user32
    found: list[tuple[int, int, int, int]] = []
    buf = ctypes.create_unicode_buffer(256)
    wnd_enum = ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_void_p, ctypes.c_void_p)

    def _cb(child, _lp):
        user32.GetClassNameW(child, buf, 256)
        rect = wintypes.RECT()
        if not user32.GetWindowRect(child, ctypes.byref(rect)):
            return True
        width, height = rect.right - rect.left, rect.bottom - rect.top
        name = buf.value.upper()
        thin = 8 <= width <= 28 and height > 60
        named = "SCROLL" in name
        if thin or (named and height > width * 2):
            found.append((int(rect.left), int(rect.top), int(rect.right), int(rect.bottom)))
        return True

    cb = wnd_enum(_cb)
    user32.EnumChildWindows(hwnd, cb, 0)
    return found


def _focus_channel_grid(dlg) -> None:
    """Click the Name column only. A Variable click opens the combo widget."""
    box = _grid_viewport(dlg)
    if box is None:
        return
    _mouse_click_xy(int(box.left) + 20, (int(box.top) + int(box.bottom)) // 2)
    time.sleep(0.08)


def _last_channel_cell(dlg):
    cells = _channel_grid_cells(_flexgrid_cells(dlg))
    if not cells:
        return None
    last_row = max(row for row, _col in cells)
    for col in sorted({c for r, c in cells if r == last_row}, reverse=True):
        _val, ctrl = cells[(last_row, col)]
        if ctrl is not None:
            return ctrl
    return None


def _scroll_channel_grid_to_bottom(dlg) -> None:
    """Bring the last channel row fully above the splitter.

    Never send PageDown / Ctrl+End — those scroll an open Variable combo.
    Close any dropdown first, then wheel the Name column.
    """
    print("[adre] scroll channel grid to bottom", flush=True)
    _dismiss_variable_combo(dlg)
    _focus_channel_grid(dlg)
    view = _grid_viewport(dlg)
    bars = _win32_vscrollbars(dlg)
    if view is not None and bars:
        mid_y = (int(view.top) + int(view.bottom)) // 2
        for left, top, right, bottom in bars:
            bar_mid = (top + bottom) // 2
            if abs(bar_mid - mid_y) > max(80, (int(view.bottom) - int(view.top))):
                continue
            sx = (left + right) // 2
            print(f"[adre] drag grid scrollbar to bottom ({sx},{bar_mid} → {bottom - 10})", flush=True)
            _drag_xy(sx, bar_mid, sx, bottom - 10)
            break
    pt = _grid_wheel_point(dlg)
    last = _last_channel_cell(dlg)
    if pt is not None:
        try:
            from pywinauto.mouse import scroll
        except Exception:
            scroll = None
        for _ in range(36):
            if last is not None and _cell_on_screen(last, dlg):
                break
            if scroll is not None:
                scroll(coords=pt, wheel_dist=-8)
            time.sleep(0.04)
        if scroll is not None:
            scroll(coords=pt, wheel_dist=-6)


def _scroll_cell_into_view(ctrl, dlg) -> bool:
    """Wheel the Name column only. Do not click — that hits option widgets."""
    try:
        from pywinauto.mouse import scroll
    except Exception:
        return _cell_on_screen(ctrl, dlg)
    host = _cell_rect(dlg)
    if host is None:
        return False
    pt = (int(host.left) + 36, int(host.top) + (int(host.bottom) - int(host.top)) // 3)
    for _ in range(16):
        if _cell_on_screen(ctrl, dlg):
            return True
        rect = _cell_rect(ctrl)
        if rect is None:
            return False
        if int(rect.bottom) > int(host.bottom) - 90:
            scroll(coords=pt, wheel_dist=-6)
        else:
            scroll(coords=pt, wheel_dist=6)
        time.sleep(0.02)
    return _cell_on_screen(ctrl, dlg)


def _cell_on_screen(ctrl, dlg) -> bool:
    """Cheap rect check — do not walk the UIA tree (that made every row slow)."""
    rect = _cell_rect(ctrl)
    host = _cell_rect(dlg)
    if rect is None or host is None:
        return False
    return int(rect.top) >= int(host.top) + 28 and int(rect.bottom) <= int(host.bottom) - 90


def _cell_is_sync(ctrl) -> bool:
    return _is_sync_waveform_label(_legacy_value(ctrl) or "")


def _set_one_variable_cell(
    ctrl, dlg, to_var: str, *, pause: float = 0.16, col_span: tuple[int, int] | None = None
) -> None:
    """Select the Variable cell, click its arrow, then pick exact Async Waveform."""
    from pywinauto.keyboard import send_keys

    if not _cell_on_screen(ctrl, dlg):
        _scroll_cell_into_view(ctrl, dlg)
    if not _click_cell_body(ctrl, col_span):
        return
    time.sleep(pause)
    if not _click_cell_dropdown(ctrl, col_span):
        send_keys("%{DOWN}")
    time.sleep(pause)
    if _click_combo_item(to_var, timeout=1.0):
        time.sleep(0.12)
        return
    send_keys("%{DOWN}")
    time.sleep(pause)
    if _click_combo_item(to_var, timeout=0.8):
        time.sleep(0.12)


def _pending_sync_rows(rows, dlg, col: int | None) -> list[tuple[int, object, str]]:
    """Leftovers from the original cell list. Do not re-walk the FlexGrid —
    after Sync→Async ADRE shows Loading plot groups and descendants() hangs."""
    still = []
    for row, ctrl, val in rows:
        try:
            if _cell_is_sync(ctrl):
                still.append((row, ctrl, val))
        except Exception:
            continue
    return still


def _text_is_loading_plot_groups(text: str) -> bool:
    return "loading plot group" in (text or "").casefold()


def _config_ok_coords(rect) -> tuple[int, int]:
    """OK is leftmost of [OK][Cancel][Apply][Help] on the dialog bottom-right."""
    btn_w, gap, margin_r, margin_b = 80, 8, 16, 18
    x = int(rect.right) - margin_r - 3 * (btn_w + gap) - btn_w // 2
    y = int(rect.bottom) - margin_b
    return x, y


def _named_button(root, title: str, depth: int = 2):
    want = title.casefold()
    stack = [(root, 0)]
    while stack:
        node, level = stack.pop()
        try:
            kids = list(node.children())
        except Exception:
            continue
        for child in kids:
            if _item_text(child).casefold() == want:
                return child
            if level < depth:
                stack.append((child, level + 1))
    return None


def _dismiss_variable_combo(dlg) -> None:
    """Click the title bar so the last Variable dropdown is not still open.

    Do not send Esc — that closes Timebase Plot Group Configuration.
    Do not send Enter — that confirms the combo instead of OK.
    """
    rect = _cell_rect(dlg)
    if rect is None:
        return
    _mouse_click_xy(int(rect.left) + 80, int(rect.top) + 12)
    time.sleep(0.08)


def _click_ok_control(dlg) -> bool:
    btn = _named_button(dlg, "OK")
    rect = _cell_rect(btn) if btn is not None else None
    if rect is None:
        host = _cell_rect(dlg)
        if host is None:
            return False
        x, y = _config_ok_coords(host)
    else:
        x = (int(rect.left) + int(rect.right)) // 2
        y = (int(rect.top) + int(rect.bottom)) // 2
    print(f"[adre] click OK at ({x},{y})", flush=True)
    _mouse_click_xy(x, y)
    return True


def _click_config_ok(dlg, adre=None, session=None) -> None:
    """Close any open Variable combo, then click OK (Enter would only close the list)."""
    _dismiss_variable_combo(dlg)
    print("[adre] OK Timebase Plot Group Configuration", flush=True)
    _click_ok_control(dlg)
    time.sleep(0.6)
    still = _find_plot_config_dialog(adre=adre, session=session, timeout=0.4)
    if still is None:
        print("[adre] Configure closed", flush=True)
        return
    _dismiss_variable_combo(still)
    _click_ok_control(still)
    time.sleep(0.8)
    if _find_plot_config_dialog(adre=adre, session=session, timeout=0.4) is None:
        print("[adre] Configure closed", flush=True)
        return
    print("[adre] OK clicked; dialog still open (plot groups may be loading)", flush=True)


def _wait_plot_groups_ready(adre=None, session=None, timeout: float = 120) -> None:
    """Fixed pause after Configure OK so ADRE can finish Loading plot groups."""
    seconds = int(timeout)
    print(
        f"[adre] waiting {seconds // 60} min after OK for plot groups to load",
        flush=True,
    )
    time.sleep(seconds)
    print("[adre] wait done — Export Plots next", flush=True)


def _set_all_plot_variables(dlg, from_var: str, to_var: str) -> int:
    """Set every Sync Waveform cell to Async; retry leftovers, then OK."""
    _ensure_foreground(dlg)
    _lower_config_splitter(dlg)
    _select_lower_half_options(dlg)
    cells = _flexgrid_cells(dlg)
    col = _plot_variable_column(cells)
    col_span = _variable_col_span(cells, col)
    if col_span is not None:
        print(f"[adre] Variable column x={col_span[0]}..{col_span[1]}", flush=True)
    rows = _variable_column_cells(cells, col) if col is not None else []
    if not rows:
        extras = _named_value_controls(dlg, from_var)
        if not extras:
            return 0
        rows = [(i, ctrl, from_var) for i, ctrl in enumerate(extras, start=1)]
    total = len(rows)
    print(f"[adre] set {total} Variable cells → {to_var!r}", flush=True)
    t0 = time.time()
    pending = list(rows)
    for attempt in range(1, 3):
        if not pending:
            break
        pause = 0.16 if attempt == 1 else 0.20
        print(f"[adre] pass {attempt}: {len(pending)} Sync cell(s)", flush=True)
        mid = max(1, len(pending) // 2)
        for index, (_row, ctrl, _val) in enumerate(pending, start=1):
            if index == mid:
                print("[adre] lower half of channel list", flush=True)
                host = _cell_rect(dlg)
                if host is not None:
                    try:
                        from pywinauto.mouse import scroll

                        pt = (int(host.left) + 36, int(host.top) + (int(host.bottom) - int(host.top)) // 3)
                        for _ in range(8):
                            scroll(coords=pt, wheel_dist=-6)
                            time.sleep(0.02)
                    except Exception:
                        pass
            _set_one_variable_cell(ctrl, dlg, to_var, pause=pause, col_span=col_span)
            if index == 1 or index == len(pending) or index % 10 == 0:
                print(f"[adre]   {index}/{len(pending)}", flush=True)
        pending = _pending_sync_rows(pending, dlg, col)
        if pending:
            print(
                f"[adre] {len(pending)} cell(s) still Sync — retry",
                flush=True,
            )
    print(
        f"[adre] Variable cells done ({total} in {time.time() - t0:.1f}s)",
        flush=True,
    )
    if pending:
        print(
            f"[adre] {len(pending)} UIA cell(s) still read as Sync; "
            "clicking OK so ADRE can load Async plot groups",
            flush=True,
        )
    return total


def _variable_column_cells(
    cells: dict[tuple[int, int], tuple[str, object]], col: int
) -> list[tuple[int, object, str]]:
    rows: list[tuple[int, object, str]] = []
    for (row, c), (val, ctrl) in sorted(cells.items()):
        if c != col or row == 0 or ctrl is None:
            continue
        text = (val or "").strip()
        if not _is_sync_waveform_label(text):
            continue
        rows.append((row, ctrl, text))
    return rows


def _sync_leftover_cells(
    cells: dict[tuple[int, int], tuple[str, object]], from_var: str
) -> list[tuple[int, int]]:
    leftover: list[tuple[int, int]] = []
    want = (from_var or "").strip().casefold()
    for (row, col), (val, _ctrl) in sorted(cells.items()):
        if row == 0:
            continue
        text = (val or "").strip()
        if text.casefold() == want or (
            _is_sync_waveform_label(from_var) and _is_sync_waveform_label(text)
        ):
            leftover.append((row, col))
    return leftover


def _right_click_plot_canvas(session, contains: str | None = None) -> None:
    """Right-click the Timebase waveform (not the HV toolbar)."""
    group = _plot_group_root(session, contains)
    _ensure_foreground(session, maximize=True)
    if group is not session:
        _maximize_ctrl(group)
        time.sleep(0.25)
        group = _plot_group_root(session, contains)
    rect = group.rectangle()
    x = int(rect.left) + max(60, (int(rect.right) - int(rect.left)) // 2)
    y = int(rect.top) + max(70, int((int(rect.bottom) - int(rect.top)) * 0.38))
    print(f"[adre] right-click Timebase plot at ({x},{y})", flush=True)
    try:
        group.click_input(button="right", coords=(x, y), absolute=True)
    except Exception:
        from pywinauto.mouse import right_click

        right_click(coords=(x, y))
    time.sleep(0.35)


def _click_plot_context_configure(timeout: float = 5) -> bool:
    """Configure on the plot right-click menu (PlotSession / Add Plot), not the gear."""
    from pywinauto import Desktop

    deadline = time.time() + timeout
    while time.time() < deadline:
        for pop in Desktop(backend="uia").windows():
            text, cls, auto, kind = _window_meta(pop)
            if not _is_popup_search_window(text, cls, auto, kind):
                continue
            try:
                kids = list(pop.children()) + list(pop.descendants())
            except Exception:
                continue
            blob = " ".join(_item_text(c).casefold() for c in kids)
            if not any(marker in blob for marker in _PLOT_CONTEXT_MARKERS):
                continue
            for ctrl in kids:
                if _item_text(ctrl) != "Configure":
                    continue
                if not _ctrl_visible(ctrl):
                    continue
                print("[adre] menu 'Configure' (plot context)", flush=True)
                ctrl.click_input()
                return True
        if _click_win32_popup("Configure"):
            return True
        time.sleep(0.1)
    return False


def _click_hv_configure(session) -> None:
    hits = [
        ctrl
        for ctrl in session.descendants()
        if _item_text(ctrl) == "Configure"
        and _ctrl_visible(ctrl)
        and _in_hv_top_toolbar(ctrl)
    ]
    if not hits:
        hits = [
            ctrl
            for ctrl in session.descendants()
            if _item_text(ctrl) == "Configure" and _ctrl_visible(ctrl)
        ]
    if not hits:
        raise RecipeIncomplete("Configure not found on HV - Plot Session")
    print("[adre] HV toolbar Configure", flush=True)
    hits[0].click_input()


def _open_timebase_configure(session, adre=None, contains: str | None = None):
    dlg = _find_plot_config_dialog(adre=adre, session=session, timeout=1.5)
    if dlg is not None:
        return dlg
    from pywinauto.keyboard import send_keys

    try:
        for ctrl in session.descendants():
            if not _is_tree_item(ctrl):
                continue
            text = _item_text(ctrl).casefold()
            if "plot group" in text or "timebase plot" in text:
                _expand(ctrl)
    except Exception:
        pass
    send_keys("{ESC}")
    time.sleep(0.2)
    _right_click_plot_canvas(session, contains)
    if _click_plot_context_configure():
        dlg = _find_plot_config_dialog(adre=adre, session=session, timeout=8)
        if dlg is not None:
            return dlg
    send_keys("{ESC}")
    time.sleep(0.2)
    _click_hv_configure(session)
    dlg = _find_plot_config_dialog(adre=adre, session=session, timeout=8)
    if dlg is None:
        raise RecipeIncomplete(
            "Timebase Plot Group Configuration did not open. "
            "Right-click the Timebase plot → Configure."
        )
    return dlg


def _win32_window(uia_window):
    """Same ADRE HWND through the win32 backend (SysTreeView32 item text)."""
    from pywinauto import Application

    hwnd = uia_window.handle
    app = Application(backend="win32").connect(handle=hwnd)
    return app.window(handle=hwnd)


def _treeview_named(win32_window, root_title: str):
    """Return the SysTreeView32 whose root node text is ``root_title``."""
    from pywinauto.controls.common_controls import TreeViewWrapper

    trees = [c for c in win32_window.children() if "SysTreeView32" in (c.class_name() or "")]
    titles = []
    for ctrl in trees:
        tv = TreeViewWrapper(ctrl.element_info)
        try:
            roots = tv.roots()
            title = (roots[0].text() or "").strip() if roots else ""
        except Exception:
            title = ""
        titles.append(title)
        if title == root_title:
            return tv
    raise RecipeIncomplete(
        f"Tree {root_title!r} not found. Visible roots: {titles}"
    )


def _tv_children(item) -> list:
    """Direct children of a SysTreeView32 node.

    WinForms ADRE trees set ``cChildren = I_CHILDRENCALLBACK (-1)``. pywinauto
    then skips TVM_GETNEXTITEM and ``item.children()`` returns an empty list
    (that is why ingest reported ``Databases: []``).
    """
    try:
        item.expand()
    except Exception:
        pass
    tree_ctrl = getattr(item, "tree_ctrl", None)
    elem = getattr(item, "elem", None)
    if tree_ctrl is not None and elem is not None:
        from pywinauto import win32defines
        from pywinauto.controls.common_controls import _treeview_element

        child_elem = tree_ctrl.send_message(
            win32defines.TVM_GETNEXTITEM,
            win32defines.TVGN_CHILD,
            elem,
        )
        kids = []
        if child_elem:
            current = _treeview_element(child_elem, tree_ctrl)
            kids.append(current)
            while True:
                nxt = current.next_item()
                if nxt is None:
                    break
                kids.append(nxt)
                current = nxt
        if kids:
            return kids
    try:
        return [c for c in item.children() if c is not None]
    except Exception:
        return []


def _tv_text(item) -> str:
    try:
        return (item.text() or "").strip()
    except Exception:
        return ""


def _tv_find_child(item, name: str):
    want = name.casefold()
    for child in _tv_children(item):
        if _tv_text(child).casefold() == want:
            return child
    return None


def _tv_walk(item, acc: list | None = None) -> list:
    acc = acc if acc is not None else []
    acc.append(item)
    for child in _tv_children(item):
        _tv_walk(child, acc)
    return acc


def _tv_expand(item) -> None:
    # Do not call select() here — that wipes a Shift multi-select.
    for method in ("expand", "ensure_visible"):
        fn = getattr(item, method, None)
        if callable(fn):
            try:
                fn()
            except Exception:
                pass


def _tv_collapse(item) -> None:
    fn = getattr(item, "collapse", None)
    if callable(fn):
        try:
            fn()
        except Exception:
            pass


def _tv_ensure_visible(item) -> None:
    fn = getattr(item, "ensure_visible", None)
    if callable(fn):
        try:
            fn()
        except Exception:
            pass


def _safe_selected(item) -> bool:
    try:
        return bool(item.is_selected())
    except Exception:
        return False


def _tv_click(
    item,
    *,
    double: bool = False,
    button: str = "left",
    expand: bool = True,
    pressed: str = "",
) -> None:
    """Real mouse click. SendMessage click() ignores Shift in ADRE's WinForms tree."""
    if expand:
        _tv_expand(item)
    else:
        _tv_ensure_visible(item)
    try:
        item.click_input(button=button, double=double, pressed=pressed)
        return
    except TypeError:
        pass
    try:
        item.click_input(button=button, pressed=pressed)
        return
    except Exception:
        pass
    if button == "right" and hasattr(item, "right_click"):
        item.right_click()
        return
    item.click()


def _focus_config_tree(window) -> None:
    w32 = _win32_window(window)
    tv = _treeview_named(w32, "Configuration Hierarchy")
    try:
        tv.set_focus()
    except Exception:
        try:
            tv.click_input()
        except Exception:
            pass


def _sampling_card_nodes(parent, card_name: str, *, collapse: bool = True) -> list:
    """Return Dynamic Sampling Card nodes. Collapse so Shift-click hits cards, not probes."""
    cards = [c for c in _tv_children(parent) if _tv_text(c) == card_name]
    if collapse:
        for card in cards:
            _tv_collapse(card)
    return cards


def _tv_item_rect(item, text_only: bool):
    try:
        return item.client_rect(text_only)
    except TypeError:
        return item.client_rect()
    except Exception:
        return None


def _tv_icon_coords(item):
    """Client point on the item icon (cylinder), not the editable name."""
    text = _tv_item_rect(item, True)
    rect = text or _tv_item_rect(item, False)
    if rect is None:
        return None
    if text is not None:
        # 8–12 px left of the label is the yellow cylinder; the name itself
        # starts in-place rename, and a right-click then shows Cut/Copy.
        x = int(text.left) - 10
        if x < 16:
            x = int(text.left) + 4
        y = int(text.top) + max(2, (int(text.bottom) - int(text.top)) // 2)
        return (x, y)
    return (
        int(rect.left) + 22,
        int(rect.top) + max(2, (int(rect.bottom) - int(rect.top)) // 2),
    )


def _tv_click_label(item, *, pressed: str = "", button: str = "left") -> None:
    """Click the item icon, not the name text (name click starts rename)."""
    _tv_ensure_visible(item)
    try:
        item.click_input(button=button, where="icon", pressed=pressed)
        return
    except Exception:
        pass
    coords = _tv_icon_coords(item)
    tree = getattr(item, "tree_ctrl", None)
    if tree is not None and coords is not None:
        tree.click_input(button=button, coords=coords, pressed=pressed, absolute=False)
        return
    item.click_input(button=button, where="text", pressed=pressed)


def _cancel_label_edit(window) -> None:
    """Dismiss in-place rename / the Windows edit context menu."""
    from pywinauto.keyboard import send_keys

    _focus_config_tree(window)
    send_keys("{ESC}")
    time.sleep(0.12)


def _edit_context_menu_open() -> bool:
    """True if the last right-click opened Cut/Copy instead of ADRE Plots."""
    from pywinauto import Desktop

    markers = (
        "insert unicode control character",
        "right to left reading order",
        "show unicode control characters",
    )
    for win in Desktop(backend="uia").windows():
        title = win.window_text() or ""
        try:
            cls = str(getattr(win.element_info, "class_name", "") or "")
        except Exception:
            cls = ""
        if "Cursor" in title:
            continue
        if title.startswith("ADRE") and cls != "#32768":
            continue
        blob = title.casefold()
        try:
            for ctrl in win.children():
                blob += " " + (ctrl.window_text() or "").casefold()
        except Exception:
            continue
        if any(marker in blob for marker in markers):
            return True
    return False


def _tv_select(item) -> None:
    fn = getattr(item, "select", None)
    if callable(fn):
        try:
            fn()
        except Exception:
            pass


def _tv_right_click_node(window, item) -> None:
    """Right-click the database row so Plots appears, not Cut/Copy."""
    from pywinauto.keyboard import send_keys

    _cancel_label_edit(window)
    _tv_ensure_visible(item)
    _tv_select(item)
    time.sleep(0.12)
    print(f"[adre] right-click icon {_tv_text(item)!r}", flush=True)
    _tv_click_label(item, button="right")
    time.sleep(0.4)
    if not _edit_context_menu_open():
        return
    send_keys("{ESC}")
    time.sleep(0.15)
    _cancel_label_edit(window)
    _tv_select(item)
    _focus_config_tree(window)
    time.sleep(0.1)
    send_keys("+{F10}")
    time.sleep(0.4)
    if _edit_context_menu_open():
        send_keys("{ESC}")
        raise RecipeIncomplete(
            "Right-click opened the Windows rename menu (Cut/Copy) on "
            f"{_tv_text(item)!r}. Close that menu, then re-run — the click "
            "must hit the yellow cylinder icon, not the name text."
        )


def _select_collapsed_cards(cards) -> None:
    """Select every Dynamic Sampling Card row without expanding probes.

    Shift-click from first→last is how ADRE is used by hand, but SendInput
    Shift often keeps only one row. Click the first card, Shift-click the
    last, then Control-click any card that is still unselected.
    """
    for card in cards:
        _tv_collapse(card)
    time.sleep(0.2)
    _tv_click_label(cards[0])
    time.sleep(0.25)
    if len(cards) == 1:
        return
    _key_down(VK_SHIFT)
    try:
        time.sleep(0.08)
        _tv_click_label(cards[-1])
        time.sleep(0.2)
    finally:
        _key_up(VK_SHIFT)
    time.sleep(0.15)
    missing = [c for c in cards if not _safe_selected(c)]
    if not missing:
        return
    # is_selected is often wrong on this WinForms tree; Control-click each
    # remaining card. Skip cards already reported selected to avoid toggle-off.
    if len(missing) == len(cards):
        missing = cards[1:]
    _key_down(VK_CONTROL)
    try:
        for card in missing:
            time.sleep(0.12)
            _tv_click_label(card)
    finally:
        _key_up(VK_CONTROL)


def launch_adre(adre_exe: Path, database: Path | None = None) -> "object":
    """Start ADRE (or attach if it is already running) and return a pywinauto app."""
    _enable_dpi_awareness()
    _require_pywinauto()
    from pywinauto import Application

    # Prefer attaching to a running instance so a license dialog is not re-triggered.
    # Do not match Cursor tabs named adre_*.json — title_re "ADRE" is case-insensitive.
    for title_re in (r"^ADRE", r".*ADRE® Sxp.*", r".*ADRE SXP.*"):
        try:
            return Application(backend="uia").connect(title_re=title_re, timeout=3)
        except Exception:
            continue

    args = [str(adre_exe)]
    # Some InstallShield apps accept a database path as the first argument; if
    # this build ignores it, the recipe's "open_database" step still runs.
    if database is not None:
        args.append(str(database.resolve()))
    return Application(backend="uia").start(" ".join(f'"{a}"' for a in args))


def _step_handlers(app, job: ExportJob) -> dict[str, Callable[[dict[str, Any]], None]]:
    """Built-in step kinds the recipe can call."""

    def wait(step: dict[str, Any]) -> None:
        time.sleep(float(step.get("seconds", 1)))

    def focus_main(step: dict[str, Any]) -> None:
        title = step.get("title_re", r"^ADRE")
        window = _main_window(app, title)
        _ensure_foreground(window)

    def menu_click(step: dict[str, Any]) -> None:
        path = step.get("path")
        if not path:
            raise RecipeIncomplete("menu_click step needs a 'path' list of menu titles")
        title = step.get("title_re", r"^ADRE")
        window = _main_window(app, title)
        window.set_focus()
        # Do not use menu_select: Plots → Export Plots is disabled until a plot
        # group exists. Click the leaf item after opening each parent.
        for name in path:
            _click_named(window, name, desktop=True)
            time.sleep(0.3)

    def click(step: dict[str, Any]) -> None:
        title = step.get("title_re", r"^ADRE")
        window = _main_window(app, title)
        autoid = step.get("auto_id")
        name = step.get("name")
        index = int(step.get("found_index", 0))
        kwargs: dict[str, Any] = {"found_index": index}
        if autoid:
            kwargs["auto_id"] = autoid
        elif name:
            kwargs["title"] = name
        else:
            raise RecipeIncomplete("click step needs 'auto_id' or 'name'")
        ctrl = window.child_window(**kwargs)
        try:
            ctrl.wait("enabled", timeout=float(step.get("enabled_timeout", 20)))
        except Exception:
            pass
        ctrl.click_input()

    def type_keys(step: dict[str, Any]) -> None:
        from pywinauto.keyboard import send_keys

        keys = step.get("keys")
        if not keys:
            raise RecipeIncomplete("type_keys step needs 'keys'")
        keys = keys.format(
            database=str(job.database.resolve()),
            inbox=str(job.inbox.resolve()),
            database_name=job.database.name,
        )
        send_keys(keys, with_spaces=True)

    def open_file_dialog(step: dict[str, Any]) -> None:
        """Type the CSV path into the Export Plots File name box and Save."""

        raw = step.get("path", "{database}").format(
            database=str(job.database.resolve()),
            inbox=str(job.inbox.resolve()),
            database_name=job.database.name,
        )
        dest = Path(raw).resolve()
        dest.parent.mkdir(parents=True, exist_ok=True)
        full = str(dest)
        print(f"[adre] save dialog → {full}", flush=True)
        deadline = time.time() + 30
        dlg = None
        while time.time() < deadline:
            dlg = _find_save_dialog()
            if dlg is not None:
                break
            time.sleep(0.3)
        if dlg is None:
            raise RecipeIncomplete(
                "Save dialog did not open after Export Plots. "
                "Maximize HV - Plot Session and keep Cursor off the plot."
            )
        _fill_save_dialog(dlg, full)

    def _database_node(window, db_name: str):
        db_item, root, tv = _hierarchy_find_db(window, db_name)
        if db_item is None:
            _load_database_from_manager(
                window, db_name, str(job.database.resolve())
            )
            db_item = _wait_db_in_hierarchy(window, db_name, timeout=90)
        if db_item is None:
            names = _hierarchy_db_names(window)
            raise RecipeIncomplete(
                f"Tree item {db_name!r} not found under Configuration Hierarchy. "
                f"Databases: {names}"
            )
        return db_item

    def select_sampling_cards(step: dict[str, Any]) -> None:
        """Select the database node only — do not expand cards or probes."""
        title = step.get("title_re", r"^ADRE")
        window = _main_window(app, title)
        _close_plot_session(window)
        _ensure_foreground(window)
        db_name = step.get("database_name") or job.database.name
        db_item = _database_node(window, db_name)
        _tv_collapse(db_item)
        time.sleep(0.2)
        _focus_config_tree(window)
        _tv_click_label(db_item)
        time.sleep(0.15)
        _cancel_label_edit(window)
        print(f"[adre] selected database {db_name!r} (collapsed — all channels)", flush=True)
        time.sleep(0.2)

    def context_plots(step: dict[str, Any]) -> None:
        """Right-click the database → Plots → Tabular List / Timebase."""
        plot = step.get("plot")
        if not plot:
            raise RecipeIncomplete("context_plots step needs 'plot' (e.g. Tabular List)")
        title = step.get("title_re", r"^ADRE")
        window = _main_window(app, title)
        _close_plot_session(window)
        _ensure_foreground(window)
        db_name = step.get("database_name") or job.database.name
        db_item = _database_node(window, db_name)
        _tv_collapse(db_item)
        time.sleep(0.15)
        last_exc: Exception | None = None
        for attempt in range(3):
            _tv_right_click_node(window, db_item)
            time.sleep(0.4)
            try:
                _click_popup_item("Plots", adre_window=window)
                time.sleep(0.35)
                _click_popup_item(plot, adre_window=window)
                last_exc = None
                break
            except RecipeIncomplete as exc:
                last_exc = exc
                print(f"[adre] Plots menu retry {attempt + 1}: {exc}", flush=True)
                from pywinauto.keyboard import send_keys

                send_keys("{ESC}{ESC}")
                time.sleep(0.35)
        if last_exc is not None:
            raise last_exc
        session = _wait_plot_session(
            timeout=float(step.get("plot_timeout", 20)), adre_window=window
        )
        if session is not None:
            print("[adre] HV - Plot Session opened (PlotSessionWindow)", flush=True)

    def tree_click(step: dict[str, Any]) -> None:
        """Click (or double-click) a node in the Plot Session Manager tree."""
        session = _plot_session_window()
        if session is not None:
            print("[adre] plot session already open — skip tree double-click", flush=True)
            _ensure_foreground(session)
            return
        name = step.get("name")
        name_contains = step.get("name_contains")
        if not name and not name_contains:
            raise RecipeIncomplete("tree_click step needs 'name' or 'name_contains'")
        title = step.get("title_re", r"^ADRE")
        try:
            window = _main_window(app, title)
        except RecipeIncomplete:
            session = _wait_plot_session(timeout=8)
            if session is not None:
                print("[adre] plot session opened — skip tree double-click", flush=True)
                _ensure_foreground(session)
                return
            raise
        w32 = _win32_window(window)
        root_title = step.get("tree_root", "Plot Session Manager Hierarchy")
        db_name = step.get("database_name") or job.database.name
        timeout = float(step.get("timeout", 45))
        deadline = time.time() + timeout
        use: list = []
        while time.time() < deadline:
            tv = _treeview_named(w32, root_title)
            hits = []
            for item in _tv_walk(tv.roots()[0]):
                text = _tv_text(item)
                if " - " in text and "plot group" not in text.casefold():
                    continue
                if name and text == name:
                    hits.append(item)
                elif name_contains and name_contains.casefold() in text.casefold():
                    hits.append(item)
                elif name_contains and "plot group" in text.casefold() and name_contains.split()[0].casefold() in text.casefold():
                    hits.append(item)
            scoped = []
            for item in hits:
                try:
                    parent = item
                    for _ in range(8):
                        parent = parent.parent()
                        if _tv_text(parent) == db_name:
                            scoped.append(item)
                            break
                except Exception:
                    continue
            use = scoped or hits
            if use:
                break
            time.sleep(1)
        if not use:
            tv = _treeview_named(w32, root_title)
            seen = [
                _tv_text(item)
                for item in _tv_walk(tv.roots()[0])
                if "plot group" in _tv_text(item).casefold()
            ]
            raise RecipeIncomplete(
                f"Tree item {name or name_contains!r} not found in {root_title}. "
                f"Visible: {seen[-12:]}"
            )
        occurrence = int(step.get("occurrence", 0))
        item = use[occurrence] if occurrence < len(use) else use[-1]
        print(f"[adre] tree_click {_tv_text(item)!r}", flush=True)
        _tv_click(item, double=bool(step.get("double")))

    def export_plots(step: dict[str, Any]) -> None:
        """Plot-window Export Plots (bottom icon), not the HV top toolbar."""
        title = step.get("title_re", r"^ADRE")
        try:
            adre = _main_window(app, title)
        except RecipeIncomplete:
            adre = None
        session = _wait_plot_session(
            timeout=float(step.get("timeout", 25)), adre_window=adre
        )
        if session is None:
            raise RecipeIncomplete(
                "HV - Plot Session (PlotSessionWindow child of ADRE) did not open. "
                "Right-click the database → Plots → Tabular List / Timebase first."
            )
        contains = step.get("plot_group_contains")
        _click_plot_export(session, contains)

    def configure_plots(step: dict[str, Any]) -> None:
        """Same Timebase plot: click first Sync cell, Shift+Down, set dropdown."""
        to_var = step.get("variable") or step.get("to_variable")
        from_var = step.get("from_variable") or "Sync Waveform"
        if not to_var:
            raise RecipeIncomplete("configure_plots step needs 'variable' (e.g. Async Waveform)")
        title = step.get("title_re", r"^ADRE")
        try:
            adre = _main_window(app, title)
        except RecipeIncomplete:
            adre = None
        session = _wait_plot_session(timeout=15, adre_window=adre)
        if session is None:
            raise RecipeIncomplete("HV - Plot Session not open for Configure")
        contains = step.get("plot_group_contains") or "Timebase"
        _ensure_foreground(session, maximize=True)
        dlg = _open_timebase_configure(session, adre=adre, contains=contains)
        print(f"[adre] {_PLOT_CONFIG_DIALOG}", flush=True)
        changed = _set_all_plot_variables(dlg, from_var, to_var)
        if changed == 0:
            raise RecipeIncomplete(
                f"No {from_var!r} Variable cells in {_PLOT_CONFIG_DIALOG}"
            )
        print(f"[adre] set {changed} plot Variable(s) to {to_var!r}", flush=True)
        _click_config_ok(dlg, adre=adre, session=session)
        _wait_plot_groups_ready(adre=adre, session=session, timeout=120)

    def data_source(step: dict[str, Any]) -> None:
        """Async is not on the Data Source toolbar — use plot Configure instead."""
        configure_plots(step)

    def placeholder(step: dict[str, Any]) -> None:
        raise RecipeIncomplete(
            step.get(
                "message",
                "This recipe step is still a placeholder. Run tools/adre_ui_map.py "
                "with ADRE open on the EC2 box, then fill adre_export_recipe.json.",
            )
        )

    def open_database(step: dict[str, Any]) -> None:
        """Database Manager: select this database and click Open."""
        title = step.get("title_re", r"^ADRE")
        window = _main_window(app, title)
        _ensure_foreground(window)
        db_name = step.get("database_name") or job.database.name
        item, _, _ = _hierarchy_find_db(window, db_name)
        if item is not None:
            print(f"[adre] {db_name} already in Configuration Hierarchy", flush=True)
            return
        _load_database_from_manager(window, db_name, str(job.database.resolve()))
        if _wait_db_in_hierarchy(window, db_name, timeout=90) is None:
            raise RecipeIncomplete(
                f"Opened Database Manager but {db_name!r} did not appear in "
                f"Configuration Hierarchy. Databases: {_hierarchy_db_names(window)}"
            )

    def wait_for_csvs(step: dict[str, Any]) -> None:
        timeout = float(step.get("timeout_seconds", 120))
        need = int(step.get("min_files", 3))
        deadline = time.time() + timeout
        while time.time() < deadline:
            if len(list(job.inbox.glob("*.csv"))) >= need:
                return
            time.sleep(1)
        raise TimeoutError(
            f"Timed out after {timeout:.0f}s waiting for {need} CSV files in {job.inbox}"
        )

    def split_static(step: dict[str, Any]) -> None:
        """Per-channel split + rotor groups. Only after the static Tabular List CSV exists."""
        from adre_split import split_export

        named = step.get("path") or "{inbox}\\export_static.csv"
        target = Path(named.replace("{inbox}", str(job.inbox)))
        timeout = float(step.get("timeout", 45))
        deadline = time.time() + timeout
        last_size = -1
        stable = 0
        while time.time() < deadline:
            if target.is_file() and target.stat().st_size > 80:
                size = target.stat().st_size
                if size == last_size:
                    stable += 1
                    if stable >= 2:
                        break
                else:
                    stable = 0
                last_size = size
            time.sleep(0.5)
        if not target.is_file():
            print(f"[adre] split static skipped — {target.name} not extracted yet", flush=True)
            return
        dest = job.inbox.parent / "channels"
        result = split_export(target, dest)
        print(
            f"[adre] split static {result.n_channels} channels → {result.groups_folder or dest}",
            flush=True,
        )

    return {
        "wait": wait,
        "focus_main": focus_main,
        "menu_click": menu_click,
        "click": click,
        "type_keys": type_keys,
        "open_file_dialog": open_file_dialog,
        "open_database": open_database,
        "select_sampling_cards": select_sampling_cards,
        "context_plots": context_plots,
        "tree_click": tree_click,
        "export_plots": export_plots,
        "configure_plots": configure_plots,
        "data_source": data_source,
        "wait_for_csvs": wait_for_csvs,
        "split_static": split_static,
        "placeholder": placeholder,
        "todo": placeholder,
    }


def run_recipe(app, job: ExportJob, recipe: dict[str, Any]) -> None:
    _enable_dpi_awareness()
    handlers = _step_handlers(app, job)
    steps = recipe.get("steps") or []
    if not steps:
        raise RecipeIncomplete("Recipe has no steps")
    for index, step in enumerate(steps, start=1):
        kind = step.get("action", "placeholder")
        if kind not in handlers:
            raise RecipeIncomplete(f"Unknown action {kind!r} at step {index}")
        print(f"[adre] step {index}/{len(steps)}: {kind} — {step.get('note', '')}", flush=True)
        handlers[kind](step)


def export_database_via_adre(
    database: str | Path,
    inbox: str | Path = "inbox",
    *,
    recipe_path: str | Path = "adre_export_recipe.json",
    adre_exe: str | Path | None = None,
    then_analyse: bool = True,
    outbox: str | Path = "outbox",
) -> Path:
    """Open ``database`` in ADRE, export CSVs into ``inbox``, optionally analyse.

    Returns the outbox folder if analysis ran, otherwise the inbox path.
    """
    database = Path(database).resolve()
    inbox = Path(inbox).resolve()
    inbox.mkdir(parents=True, exist_ok=True)

    if not database.is_dir():
        raise FileNotFoundError(f"Database folder not found: {database}")

    exe = find_adre_executable(adre_exe)
    recipe = load_recipe(recipe_path)
    job = ExportJob(
        database=database,
        inbox=inbox,
        recipe_path=Path(recipe_path),
        adre_exe=exe,
    )

    print(f"[adre] using {exe}", flush=True)
    print(f"[adre] database {database}", flush=True)
    print(f"[adre] exports → {inbox}", flush=True)

    app = launch_adre(exe, database)
    run_recipe(app, job, recipe)

    if not then_analyse:
        return inbox

    from .pipeline import process_export_directory

    result = process_export_directory(inbox, outbox)
    print(f"[adre] analysis written to {result.outbox}", flush=True)
    return result.outbox


def smoke_check() -> dict[str, str]:
    """Return what this machine knows about ADRE — safe to run anywhere."""
    info = {"adre_exe": "", "recipe": "", "status": ""}
    try:
        info["adre_exe"] = str(find_adre_executable())
        info["status"] = "ADRE found"
    except AdreNotInstalled as exc:
        info["status"] = str(exc)
    recipe = Path("adre_export_recipe.json")
    example = Path("adre_export_recipe.example.json")
    if recipe.is_file():
        data = load_recipe(recipe)
        placeholders = sum(
            1 for s in data.get("steps", []) if s.get("action") in {"placeholder", "todo"}
        )
        info["recipe"] = f"{recipe} ({placeholders} placeholder steps)"
    elif example.is_file():
        info["recipe"] = f"missing {recipe.name}; example present"
    else:
        info["recipe"] = "no recipe file"
    return info
