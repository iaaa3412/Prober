import tkinter as tk
from tkinter import ttk, filedialog
import os
import glob

class WaferDataPanel(ttk.Frame):
    def __init__(self, parent):
        super().__init__(parent)
        self.folder_path = None
        self.csv_files = []
        self.setup_ui()

    def setup_ui(self):
        controls_frame = ttk.Frame(self)
        controls_frame.pack(fill="x", padx=10, pady=10)
        self.btn_load = ttk.Button(
            controls_frame, 
            text="📁 Load ATA Folder", 
            command=self.load_ata_folder
        )
        self.btn_load.pack(side="left")
        self.lbl_path = ttk.Label(controls_frame, text="No folder selected...", foreground="gray")
        self.lbl_path.pack(side="left", padx=15)
        list_frame = ttk.LabelFrame(self, text="ATA Files (CSVs)")
        list_frame.pack(fill="both", expand=True, padx=10, pady=5)
        scrollbar = ttk.Scrollbar(list_frame, orient="vertical")
        self.file_listbox = tk.Listbox(
            list_frame, 
            yscrollcommand=scrollbar.set, 
            selectmode="extended",
            font=("Consolas", 10)
        )
        scrollbar.config(command=self.file_listbox.yview)
        scrollbar.pack(side="right", fill="y")
        self.file_listbox.pack(side="left", fill="both", expand=True, padx=5, pady=5)

    def load_ata_folder(self):
        selected_dir = filedialog.askdirectory(title="Select Wafer ATA Folder")
        
        if selected_dir:
            self.folder_path = selected_dir
            self.lbl_path.config(text=self.folder_path, foreground="black")
            self.file_listbox.delete(0, tk.END)
            self.csv_files.clear()
            search_pattern = os.path.join(self.folder_path, "*.csv")
            found_files = glob.glob(search_pattern)
            self.csv_files = sorted(found_files)
            for file_path in self.csv_files:
                filename = os.path.basename(file_path)
                self.file_listbox.insert(tk.END, filename)
            print(f"Loaded {len(self.csv_files)} ATA files from {os.path.basename(self.folder_path)}")