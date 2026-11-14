"""
Demand Forecast Processor — Desktop App

Tkinter UI wrapping the forecasting engine (app/forecast_engine.py, preserved
verbatim) behind a time-limited license (app/license_manager.py). See
app/license_manager.py for the full licensing model and design notes.
"""

import os
import threading
import tkinter as tk
from tkinter import filedialog, messagebox

from app.forecast_engine import process_workbook
from app.license_manager import LicenseManager

filename = None
license_manager = LicenseManager()


def show_license_activation_dialog(parent=None) -> bool:
    """Show dialog for signed license token activation. Returns True if activated."""
    activation_window = tk.Toplevel(parent) if parent else tk.Toplevel()
    activation_window.title("License Activation")
    activation_window.geometry("650x420")
    activation_window.grab_set()

    tk.Label(activation_window, text="License Activation", font=("Arial", 14, "bold")).pack(pady=10)
    tk.Label(
        activation_window,
        text="Enter the customer name and signed license key provided by the vendor.",
        font=("Arial", 9), fg="gray", wraplength=600,
    ).pack(pady=5)

    tk.Label(activation_window, text="Customer Name:").pack(pady=5)
    customer_entry = tk.Entry(activation_window, width=50)
    customer_entry.pack(pady=5)

    tk.Label(activation_window, text="Signed License Key:").pack(pady=5)
    license_text = tk.Text(activation_window, width=72, height=8, wrap=tk.WORD)
    license_text.pack(pady=5)

    result = {"activated": False}

    def activate():
        customer_name = customer_entry.get().strip()
        license_token = license_text.get("1.0", tk.END).strip()

        if not customer_name or not license_token:
            messagebox.showerror("Error", "Please enter both customer name and license key.")
            return

        success, message = license_manager.activate_license(customer_name, license_token)
        if success:
            messagebox.showinfo("Success", "License activated successfully!")
            result["activated"] = True
            activation_window.destroy()
        else:
            messagebox.showerror("Activation Failed", message)

    tk.Button(activation_window, text="Activate", command=activate, width=20, bg="#4CAF50", fg="white").pack(pady=10)
    tk.Button(activation_window, text="Cancel", command=activation_window.destroy, width=20).pack(pady=5)

    activation_window.wait_window()
    return result["activated"]


def browse_file():
    global filename
    filename = filedialog.askopenfilename(filetypes=[("Excel files", "*.xlsx;*.xlsm")])
    file_label.config(text=f"File Selected: {os.path.basename(filename)}" if filename else "No file selected")


def process_files():
    global filename

    if not filename:
        messagebox.showerror("Error", "No file selected")
        return

    has_access, message, _ = license_manager.check_license_status()
    if not has_access:
        messagebox.showerror("License Expired", f"{message}")
        return

    selected_file = filename

    def set_ui_processing_state(is_processing: bool, text_message: str = ""):
        process_button.config(state=tk.DISABLED if is_processing else tk.NORMAL)
        browse_button.config(state=tk.DISABLED if is_processing else tk.NORMAL)
        if text_message:
            info_label.config(text=text_message)
        root.config(cursor="watch" if is_processing else "")
        root.update_idletasks()

    def on_success(summary):
        set_ui_processing_state(False, "Processing completed. Output folder created successfully.")
        messagebox.showinfo(
            "Success",
            "Processing complete!\n\n"
            f"Output folder:\n{summary.get('output_dir')}\n\n"
            f"Categories processed: {summary.get('categories_processed')}\n"
            f"Elapsed seconds: {summary.get('elapsed_seconds'):.2f}",
        )

    def on_error(error_text: str):
        set_ui_processing_state(False, "Processing failed. Check app log for details.")
        messagebox.showerror("Error", f"{error_text}\n\nCheck app log for details.")

    def worker():
        try:
            license_manager._log_event(f"Processing started for file: {selected_file}")
            summary = process_workbook(input_path=selected_file, output_dir=None, workers=None)
            license_manager._log_event(
                f"Processing completed. output_dir={summary.get('output_dir')}, "
                f"categories={summary.get('categories_processed')}"
            )
            root.after(0, lambda: on_success(summary))
        except Exception as e:
            import traceback
            license_manager._log_event(f"ERROR during processing: {e}\n{traceback.format_exc()}")
            root.after(0, lambda: on_error(str(e)))

    set_ui_processing_state(True, "Processing started. The full model search is retained, so large files may take time.")
    threading.Thread(target=worker, daemon=True).start()


def activate_license_from_main():
    if show_license_activation_dialog(root):
        messagebox.showinfo("Success", "License activated! Please restart the application.")
        root.quit()


def main():
    global root, file_label, browse_button, process_button, info_label

    has_access, message, _ = license_manager.check_license_status()

    if not has_access:
        temp_root = tk.Tk()
        temp_root.withdraw()
        response = messagebox.askquestion(
            "License Required",
            f"{message}\n\nWould you like to activate a license key now?",
        )
        if response == "yes" and show_license_activation_dialog(temp_root):
            has_access, message, _ = license_manager.check_license_status()
            if not has_access:
                messagebox.showerror("Error", "License activation failed. Please contact support.")
                temp_root.destroy()
                return
        else:
            temp_root.destroy()
            return
        temp_root.destroy()

    root = tk.Tk()
    root.title("Demand Forecast Processor")
    root.geometry("550x320")

    status_frame = tk.Frame(root, bg="#f0f0f0", relief=tk.RIDGE, borderwidth=2)
    status_frame.pack(fill=tk.X, padx=10, pady=10)

    tk.Label(
        status_frame, text=message, bg="#d4edda", fg="#155724",
        font=("Arial", 9, "bold"), padx=10, pady=5,
    ).pack()
    tk.Button(
        status_frame, text="Renew / Activate License", command=activate_license_from_main,
        bg="#007bff", fg="white", font=("Arial", 9),
    ).pack(pady=5)

    file_label = tk.Label(root, text="No file selected", font=("Arial", 10))
    file_label.pack(pady=10)

    browse_button = tk.Button(root, text="Browse", command=browse_file, width=20, height=2, font=("Arial", 10))
    browse_button.pack(pady=5)

    process_button = tk.Button(
        root, text="Process with Optimization", command=process_files,
        width=20, height=2, font=("Arial", 10), bg="#28a745", fg="white",
    )
    process_button.pack(pady=5)

    info_label = tk.Label(
        root, text="Full model-search optimization across all 8 forecasting methods.",
        font=("Arial", 9, "italic"), fg="blue",
    )
    info_label.pack(pady=10)

    license_manager._log_event("Application started successfully")
    root.mainloop()


if __name__ == "__main__":
    import multiprocessing

    multiprocessing.freeze_support()

    try:
        main()
    except Exception as e:
        import traceback
        error_details = traceback.format_exc()
        print(f"STARTUP ERROR: {e}\n{error_details}")
        try:
            root = tk.Tk()
            root.withdraw()
            messagebox.showerror("Startup Error", f"Failed to start application:\n\n{e}")
            root.destroy()
        except Exception:
            pass
        try:
            with open("startup_error.log", "w", encoding="utf-8") as f:
                f.write(f"Startup Error: {e}\n\n{error_details}")
        except OSError:
            pass
