from __future__ import annotations

import os
import sys
from pathlib import Path


def _valid_pair(base: Path) -> tuple[Path, Path] | None:
    tcl = base / "tcl8.6"
    tk = base / "tk8.6"
    if (tcl / "init.tcl").exists() and (tk / "tk.tcl").exists():
        return tcl, tk
    return None


def _candidate_initializes(tcl: Path, tk: Path) -> bool:
    old_tcl = os.environ.get("TCL_LIBRARY")
    old_tk = os.environ.get("TK_LIBRARY")
    os.environ["TCL_LIBRARY"] = str(tcl)
    os.environ["TK_LIBRARY"] = str(tk)
    try:
        import tkinter

        interp = tkinter.Tcl()
        interp.eval("info patchlevel")
        return True
    except Exception:
        return False
    finally:
        if old_tcl is None:
            os.environ.pop("TCL_LIBRARY", None)
        else:
            os.environ["TCL_LIBRARY"] = old_tcl
        if old_tk is None:
            os.environ.pop("TK_LIBRARY", None)
        else:
            os.environ["TK_LIBRARY"] = old_tk


def ensure_tcl_tk_paths() -> None:
    existing_tcl = os.environ.get("TCL_LIBRARY")
    existing_tk = os.environ.get("TK_LIBRARY")
    if (
        existing_tcl
        and existing_tk
        and (Path(existing_tcl) / "init.tcl").exists()
        and (Path(existing_tk) / "tk.tcl").exists()
        and _candidate_initializes(Path(existing_tcl), Path(existing_tk))
    ):
        return

    candidates = [
        Path(os.environ.get("ProgramFiles", r"C:\Program Files")) / "Git" / "mingw64" / "lib",
        Path(sys.prefix) / "tcl",
        Path(sys.base_prefix) / "tcl",
        Path(sys.executable).parent / "tcl",
        Path(os.environ.get("ProgramFiles", r"C:\Program Files")) / "Inkscape" / "lib",
        Path(os.environ.get("ProgramFiles", r"C:\Program Files")) / "FreeCAD 0.21" / "bin" / "tcl",
    ]
    for base in candidates:
        pair = _valid_pair(base)
        if not pair:
            continue
        tcl, tk = pair
        if _candidate_initializes(tcl, tk):
            os.environ["TCL_LIBRARY"] = str(tcl)
            os.environ["TK_LIBRARY"] = str(tk)
            return
