"""
Demand Forecast Processor - free desktop tool

Tkinter UI around the forecasting engine (app/forecast_engine.py). Type or paste
the path of an Excel workbook (or use Browse to fill it in), press Run, and the
engine writes a folder of Excel outputs next to the input file.

Everything the app does is written to a plain-text log file
(logs/demand_forecast_log.txt next to the app), and each run also writes
run_log.txt inside its output folder, so nothing lives only in the terminal.
"""

import sys
import threading
import traceback
from datetime import datetime
from pathlib import Path
import tkinter as tk
from tkinter import filedialog, messagebox

from app.forecast_engine import process_workbook

APP_TITLE = "Demand Forecast Processor"
SUPPORTED_EXTENSIONS = (".xlsx", ".xlsm")


def app_dir() -> Path:
    """Folder the app runs from (the .exe folder when packaged)."""
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent


LOG_DIR = app_dir() / "logs"
LOG_FILE = LOG_DIR / "demand_forecast_log.txt"
_log_lock = threading.Lock()


def log(message: str) -> None:
    """Append a timestamped line to the text log file (and echo it to the console)."""
    line = f"{datetime.now():%Y-%m-%d %H:%M:%S} | {message}"
    print(line)
    try:
        LOG_DIR.mkdir(parents=True, exist_ok=True)
        with _log_lock, open(LOG_FILE, "a", encoding="utf-8") as fh:
            fh.write(line + "\n")
    except OSError:
        pass


def write_run_log(summary: dict, input_path: str) -> None:
    """Write a run_log.txt with the run summary into the output folder."""
    output_dir = summary.get("output_dir")
    if not output_dir:
        return
    lines = [
        f"{APP_TITLE} run log",
        f"Finished: {datetime.now():%Y-%m-%d %H:%M:%S}",
        f"Input file: {input_path}",
    ]
    for key, value in summary.items():
        lines.append(f"{key}: {value}")
    try:
        with open(Path(output_dir) / "run_log.txt", "w", encoding="utf-8") as fh:
            fh.write("\n".join(lines) + "\n")
    except OSError as exc:
        log(f"Could not write run_log.txt: {exc}")


def validate_input_path(raw_path: str) -> "tuple[bool, str]":
    """Check the path the user typed. Returns (ok, cleaned_path_or_error)."""
    path_text = raw_path.strip().strip('"').strip("'")
    if not path_text:
        return False, "Enter the path of an Excel workbook."
    path = Path(path_text).expanduser()
    if not path.is_file():
        return False, f"File not found:\n{path}"
    if path.suffix.lower() not in SUPPORTED_EXTENSIONS:
        return False, "The file must be an Excel workbook (.xlsx or .xlsm)."
    return True, str(path.resolve())


def browse_file() -> None:
    chosen = filedialog.askopenfilename(filetypes=[("Excel files", "*.xlsx *.xlsm")])
    if chosen:
        path_var.set(chosen)


def process_files() -> None:
    ok, result = validate_input_path(path_var.get())
    if not ok:
        log(f"Input rejected: {result}")
        messagebox.showerror("Input file", result)
        return
    selected_file = result

    def set_ui_processing_state(is_processing: bool, text_message: str = "") -> None:
        state = tk.DISABLED if is_processing else tk.NORMAL
        process_button.config(state=state)
        browse_button.config(state=state)
        path_entry.config(state=state)
        if text_message:
            info_label.config(text=text_message)
        root.config(cursor="watch" if is_processing else "")
        root.update_idletasks()

    def on_success(summary: dict) -> None:
        set_ui_processing_state(False, "Done. Output folder created.")
        messagebox.showinfo(
            "Done",
            "Processing complete.\n\n"
            f"Output folder:\n{summary.get('output_dir')}\n\n"
            f"Categories processed: {summary.get('categories_processed')}\n"
            f"Elapsed seconds: {summary.get('elapsed_seconds'):.2f}\n\n"
            f"Log file:\n{LOG_FILE}",
        )

    def on_error(error_text: str) -> None:
        set_ui_processing_state(False, "Processing failed. See the log file for details.")
        messagebox.showerror("Error", f"{error_text}\n\nLog file:\n{LOG_FILE}")

    def worker() -> None:
        try:
            log(f"Processing started for file: {selected_file}")
            summary = process_workbook(input_path=selected_file, output_dir=None, workers=None)
            write_run_log(summary, selected_file)
            log(
                f"Processing completed. output_dir={summary.get('output_dir')}, "
                f"categories={summary.get('categories_processed')}"
            )
            root.after(0, lambda: on_success(summary))
        except Exception as exc:  # noqa: BLE001 - shown to the user and logged
            log(f"ERROR during processing: {exc}\n{traceback.format_exc()}")
            message = str(exc)
            root.after(0, lambda: on_error(message))

    set_ui_processing_state(
        True, "Processing... the full model search runs per category, so large files take time."
    )
    threading.Thread(target=worker, daemon=True).start()


def main() -> None:
    global root, path_var, path_entry, browse_button, process_button, info_label

    root = tk.Tk()
    root.title(APP_TITLE)
    root.geometry("620x280")

    tk.Label(root, text=APP_TITLE, font=("Arial", 13, "bold")).pack(pady=(12, 2))
    tk.Label(
        root, text="Free desktop tool: Excel in, Excel out.", font=("Arial", 9), fg="gray"
    ).pack()

    tk.Label(root, text="Path of the input Excel workbook:", font=("Arial", 10)).pack(
        anchor="w", padx=20, pady=(14, 2)
    )
    row = tk.Frame(root)
    row.pack(fill=tk.X, padx=20)
    path_var = tk.StringVar()
    path_entry = tk.Entry(row, textvariable=path_var, font=("Arial", 10))
    path_entry.pack(side=tk.LEFT, fill=tk.X, expand=True)
    path_entry.focus_set()
    browse_button = tk.Button(row, text="Browse...", command=browse_file, width=10)
    browse_button.pack(side=tk.LEFT, padx=(8, 0))

    process_button = tk.Button(
        root, text="Run forecast", command=process_files,
        width=20, height=2, font=("Arial", 10), bg="#28a745", fg="white",
    )
    process_button.pack(pady=16)
    root.bind("<Return>", lambda _event: process_files())

    info_label = tk.Label(
        root, text="Full model search across all 8 forecasting methods.",
        font=("Arial", 9, "italic"), fg="blue",
    )
    info_label.pack()

    log("Application started")
    root.mainloop()
    log("Application closed")


if __name__ == "__main__":
    import multiprocessing

    multiprocessing.freeze_support()

    try:
        main()
    except Exception as exc:  # noqa: BLE001 - last-resort startup handler
        log(f"STARTUP ERROR: {exc}\n{traceback.format_exc()}")
        try:
            fallback = tk.Tk()
            fallback.withdraw()
            messagebox.showerror(
                "Startup Error", f"Failed to start the application:\n\n{exc}\n\nLog file:\n{LOG_FILE}"
            )
            fallback.destroy()
        except Exception:  # noqa: BLE001
            pass
