import sys
import tkinter as tk
from tkinter import messagebox

import data_stub
from opening_form import OpeningForm


if __name__ == "__main__":
    failures = data_stub.check_database_connections()
    if failures:
        root = tk.Tk()
        root.withdraw()
        for title, message in failures:
            messagebox.showerror(title, message)
        root.destroy()
        sys.exit(1)

    app = OpeningForm()
    app.mainloop()
