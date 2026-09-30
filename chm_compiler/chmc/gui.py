"""Tkinter front end for chmc: ``python -m chmc gui``."""

from __future__ import annotations

import os
import queue
import subprocess
import sys
import threading
import traceback

try:
    import tkinter as tk
    from tkinter import filedialog, messagebox, ttk
except ImportError:  # pragma: no cover - depends on the Python build
    tk = None

from .project import compile_project, load_folder, load_hhp
from .scaffold import create_project


class App:
    def __init__(self, root: "tk.Tk") -> None:
        self.root = root
        root.title("CHM Compiler")
        root.minsize(640, 460)
        self.msgs: "queue.Queue[tuple]" = queue.Queue()
        self.busy = False

        self.source = tk.StringVar()
        self.output = tk.StringVar()
        self.title = tk.StringVar()
        self.level = tk.IntVar(value=6)
        self.auto_toc = tk.BooleanVar(value=True)
        self.auto_index = tk.BooleanVar(value=True)

        frm = ttk.Frame(root, padding=12)
        frm.pack(fill="both", expand=True)
        frm.columnconfigure(1, weight=1)

        ttk.Label(frm, text="Source (.hhp or folder):").grid(row=0, column=0, sticky="w")
        ttk.Entry(frm, textvariable=self.source).grid(row=0, column=1, sticky="ew", padx=6)
        btns = ttk.Frame(frm)
        btns.grid(row=0, column=2, sticky="e")
        ttk.Button(btns, text="Project…", command=self.pick_hhp).pack(side="left")
        ttk.Button(btns, text="Folder…", command=self.pick_folder).pack(side="left", padx=(4, 0))

        ttk.Label(frm, text="Output .chm:").grid(row=1, column=0, sticky="w", pady=(6, 0))
        ttk.Entry(frm, textvariable=self.output).grid(row=1, column=1, sticky="ew", padx=6, pady=(6, 0))
        ttk.Button(frm, text="Save as…", command=self.pick_output).grid(row=1, column=2, sticky="e", pady=(6, 0))

        ttk.Label(frm, text="Title override:").grid(row=2, column=0, sticky="w", pady=(6, 0))
        ttk.Entry(frm, textvariable=self.title).grid(row=2, column=1, sticky="ew", padx=6, pady=(6, 0))

        opts = ttk.Frame(frm)
        opts.grid(row=3, column=0, columnspan=3, sticky="w", pady=(8, 0))
        ttk.Label(opts, text="Compression:").pack(side="left")
        ttk.Spinbox(opts, from_=0, to=9, width=3, textvariable=self.level).pack(side="left", padx=(4, 12))
        ttk.Checkbutton(opts, text="Generate TOC for folders", variable=self.auto_toc).pack(side="left")
        ttk.Checkbutton(opts, text="Generate index for folders", variable=self.auto_index).pack(side="left", padx=(8, 0))

        actions = ttk.Frame(frm)
        actions.grid(row=4, column=0, columnspan=3, sticky="ew", pady=(10, 6))
        self.compile_btn = ttk.Button(actions, text="Compile", command=self.compile)
        self.compile_btn.pack(side="left")
        ttk.Button(actions, text="New project…", command=self.new_project).pack(side="left", padx=6)
        self.open_btn = ttk.Button(actions, text="Open .chm", command=self.open_output, state="disabled")
        self.open_btn.pack(side="left")
        self.progress = ttk.Progressbar(actions, mode="indeterminate", length=160)
        self.progress.pack(side="right")

        self.log = tk.Text(frm, height=16, wrap="word", state="disabled")
        self.log.grid(row=5, column=0, columnspan=3, sticky="nsew")
        sb = ttk.Scrollbar(frm, orient="vertical", command=self.log.yview)
        sb.grid(row=5, column=3, sticky="ns")
        self.log["yscrollcommand"] = sb.set
        frm.rowconfigure(5, weight=1)

        self.status = tk.StringVar(value="Pick a .hhp project or a folder of HTML files.")
        ttk.Label(root, textvariable=self.status, relief="sunken", anchor="w", padding=(6, 2)).pack(fill="x", side="bottom")
        self.root.after(100, self._poll)

    # -- pickers ---------------------------------------------------------
    def pick_hhp(self) -> None:
        path = filedialog.askopenfilename(filetypes=[("HTML Help project", "*.hhp"), ("All files", "*.*")])
        if path:
            self._set_source(path)

    def pick_folder(self) -> None:
        path = filedialog.askdirectory()
        if path:
            self._set_source(path)

    def _set_source(self, path: str) -> None:
        self.source.set(path)
        if os.path.isdir(path):
            out = os.path.join(os.path.dirname(os.path.abspath(path)), os.path.basename(path.rstrip("/\\")) + ".chm")
        else:
            try:
                proj = load_hhp(path)
                out = os.path.join(proj.base_dir, proj.compiled_file)
            except Exception:
                out = os.path.splitext(path)[0] + ".chm"
        self.output.set(os.path.normpath(out))

    def pick_output(self) -> None:
        path = filedialog.asksaveasfilename(defaultextension=".chm", filetypes=[("Compiled HTML Help", "*.chm")])
        if path:
            self.output.set(path)

    def new_project(self) -> None:
        folder = filedialog.askdirectory(title="Folder for the new project")
        if not folder:
            return
        try:
            hhp = create_project(folder)
        except OSError as exc:
            messagebox.showerror("New project", str(exc))
            return
        self._set_source(hhp)
        self._append(f"Created starter project {hhp}\n")

    # -- compile ---------------------------------------------------------
    def compile(self) -> None:
        if self.busy:
            return
        src = self.source.get().strip()
        if not src or not os.path.exists(src):
            messagebox.showwarning("Compile", "Choose a .hhp project file or a folder first.")
            return
        self.busy = True
        self.compile_btn["state"] = "disabled"
        self.open_btn["state"] = "disabled"
        self.progress.start(12)
        self.status.set("Compiling…")
        self._clear()
        args = (src, self.output.get().strip() or None, self.title.get().strip(), self.level.get(),
                self.auto_toc.get(), self.auto_index.get())
        threading.Thread(target=self._worker, args=args, daemon=True).start()

    def _worker(self, src, output, title, level, toc, index) -> None:
        log = lambda m: self.msgs.put(("log", m + "\n"))  # noqa: E731
        try:
            if os.path.isdir(src):
                proj = load_folder(src, title=title, make_toc=toc, make_index=index)
            else:
                proj = load_hhp(src)
                if title:
                    proj.title = title
            res = compile_project(proj, output, level=level, log=log)
            self.msgs.put(("done", res))
        except Exception as exc:
            self.msgs.put(("log", traceback.format_exc()))
            self.msgs.put(("error", exc))

    def _poll(self) -> None:
        try:
            while True:
                kind, payload = self.msgs.get_nowait()
                if kind == "log":
                    self._append(payload)
                elif kind == "done":
                    self._finish()
                    self.last_output = payload.output
                    self.open_btn["state"] = "normal"
                    warn = f", {len(payload.warnings)} warning(s)" if payload.warnings else ""
                    self.status.set(f"Done: {payload.output} ({payload.size:,} bytes{warn})")
                elif kind == "error":
                    self._finish()
                    self.status.set(f"Failed: {payload}")
                    messagebox.showerror("Compile failed", str(payload))
        except queue.Empty:
            pass
        self.root.after(100, self._poll)

    def _finish(self) -> None:
        self.busy = False
        self.progress.stop()
        self.progress["value"] = 0
        self.compile_btn["state"] = "normal"

    def open_output(self) -> None:
        path = getattr(self, "last_output", None)
        if not path:
            return
        if sys.platform.startswith("win"):
            os.startfile(path)  # type: ignore[attr-defined]
        elif sys.platform == "darwin":
            subprocess.Popen(["open", path])
        else:
            subprocess.Popen(["xdg-open", path])

    # -- log helpers -----------------------------------------------------
    def _append(self, text: str) -> None:
        self.log["state"] = "normal"
        self.log.insert("end", text)
        self.log.see("end")
        self.log["state"] = "disabled"

    def _clear(self) -> None:
        self.log["state"] = "normal"
        self.log.delete("1.0", "end")
        self.log["state"] = "disabled"


def main() -> None:
    if tk is None:
        sys.exit("tkinter is not available in this Python; use the command line: python -m chmc build ...")
    root = tk.Tk()
    App(root)
    root.mainloop()


if __name__ == "__main__":
    main()
