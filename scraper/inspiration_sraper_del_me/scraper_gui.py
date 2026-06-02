#!/usr/bin/env python3
"""
Sreality Scraper GUI
====================
A user-friendly graphical interface for the Sreality scraper.
Run this file to launch the GUI application.
"""

import tkinter as tk
from tkinter import ttk, scrolledtext, messagebox, filedialog
import json
import csv
import threading
import subprocess
import sys
import os
from datetime import datetime
from pathlib import Path


class ScraperGUI:
    def __init__(self, root):
        self.root = root
        self.root.title("Sreality.cz Scraper")
        self.root.geometry("800x700")
        self.root.minsize(700, 600)
        
        self.config_path = "config.json"
        self.config = self.load_config()
        self.scraper_process = None
        self.scraper_processes = []  # For parallel execution
        self.is_running = False
        
        self.create_widgets()
        self.update_status()
    
    def load_config(self):
        """Load configuration from file."""
        try:
            with open(self.config_path, 'r', encoding='utf-8') as f:
                return json.load(f)
        except FileNotFoundError:
            messagebox.showerror("Error", "config.json not found!")
            return {}
        except json.JSONDecodeError as e:
            messagebox.showerror("Error", f"Invalid config.json: {e}")
            return {}
    
    def save_config(self):
        """Save configuration to file."""
        try:
            with open(self.config_path, 'w', encoding='utf-8') as f:
                json.dump(self.config, f, indent=4, ensure_ascii=False)
            self.log("Configuration saved.")
        except Exception as e:
            messagebox.showerror("Error", f"Could not save config: {e}")
    
    def create_widgets(self):
        """Create all GUI widgets."""
        # Main notebook for tabs
        self.notebook = ttk.Notebook(self.root)
        self.notebook.pack(fill='both', expand=True, padx=10, pady=5)
        
        # Tab 1: Main Control
        self.tab_main = ttk.Frame(self.notebook)
        self.notebook.add(self.tab_main, text="  Main  ")
        self.create_main_tab()
        
        # Tab 2: Configuration
        self.tab_config = ttk.Frame(self.notebook)
        self.notebook.add(self.tab_config, text="  Configuration  ")
        self.create_config_tab()
        
        # Tab 3: Cities
        self.tab_cities = ttk.Frame(self.notebook)
        self.notebook.add(self.tab_cities, text="  Cities  ")
        self.create_cities_tab()
        
        # Tab 4: Help
        self.tab_help = ttk.Frame(self.notebook)
        self.notebook.add(self.tab_help, text="  Help  ")
        self.create_help_tab()
    
    def create_main_tab(self):
        """Create the main control tab."""
        # Status frame
        status_frame = ttk.LabelFrame(self.tab_main, text="Status", padding=10)
        status_frame.pack(fill='x', padx=10, pady=5)
        
        self.status_label = ttk.Label(status_frame, text="Ready", font=('Arial', 12, 'bold'))
        self.status_label.pack()
        
        self.progress_var = tk.DoubleVar()
        self.progress_bar = ttk.Progressbar(status_frame, variable=self.progress_var, maximum=100)
        self.progress_bar.pack(fill='x', pady=5)
        
        self.progress_label = ttk.Label(status_frame, text="")
        self.progress_label.pack()
        
        # Batch selection frame
        batch_frame = ttk.LabelFrame(self.tab_main, text="Batch Selection", padding=10)
        batch_frame.pack(fill='x', padx=10, pady=5)
        
        ttk.Label(batch_frame, text="Select batch to run:").pack(side='left', padx=5)
        
        self.batch_var = tk.StringVar(value="config.json")
        self.batch_combo = ttk.Combobox(batch_frame, textvariable=self.batch_var, width=50, state='readonly')
        self.batch_combo['values'] = [
            "All cities (config.json)",
            "Batch 1: Teplice, Most, Usti, Chomutov, Sokolov, Beroun",
            "Batch 2: Kraluv Dvur, Kladno, Horovice, Mar. Lazne, Tachov, Cheb",
            "Batch 3: Ostrov, Klatovy, Plzen, Pardubice, HK, Rychnov",
            "Batch 4: Chrudim, Jihlava, Havl. Brod, Humpolec, Podebrad, Nymburk",
            "Batch 5: Liberec, Ceska Lipa, Ceske Budejovice, Pisek, Ml. Boleslav",
        ]
        self.batch_combo.current(0)
        self.batch_combo.pack(side='left', padx=5)
        
        # Control buttons
        control_frame = ttk.Frame(self.tab_main, padding=10)
        control_frame.pack(fill='x', padx=10, pady=5)
        
        self.headless_var = tk.BooleanVar(value=True)
        ttk.Checkbutton(control_frame, text="Headless mode (no browser window)", 
                       variable=self.headless_var).pack(side='left', padx=5)
        
        self.test_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(control_frame, text="Test mode (limited data)", 
                       variable=self.test_var).pack(side='left', padx=5)
        
        # Parallel workers frame
        parallel_frame = ttk.LabelFrame(self.tab_main, text="Parallel Processing", padding=10)
        parallel_frame.pack(fill='x', padx=10, pady=5)
        
        ttk.Label(parallel_frame, text="Number of parallel browsers:").pack(side='left', padx=5)
        self.workers_var = tk.IntVar(value=3)
        self.workers_spinbox = ttk.Spinbox(parallel_frame, from_=1, to=10, width=5, 
                                           textvariable=self.workers_var)
        self.workers_spinbox.pack(side='left', padx=5)
        
        ttk.Label(parallel_frame, text="(1 = sequential, 2-10 = parallel by cities)", 
                 foreground='gray').pack(side='left', padx=10)
        
        # Warning label for parallel mode
        self.parallel_warning = ttk.Label(parallel_frame, 
            text="⚠ More workers = faster but higher detection risk", 
            foreground='orange')
        self.parallel_warning.pack(side='left', padx=10)
        
        # Start/Stop buttons
        btn_frame = ttk.Frame(self.tab_main, padding=10)
        btn_frame.pack(fill='x', padx=10, pady=5)
        
        self.start_btn = ttk.Button(btn_frame, text="▶ Start Scraping", 
                                   command=self.start_scraping, width=20)
        self.start_btn.pack(side='left', padx=5)
        
        self.stop_btn = ttk.Button(btn_frame, text="■ Stop", 
                                  command=self.stop_scraping, width=20, state='disabled')
        self.stop_btn.pack(side='left', padx=5)
        
        ttk.Button(btn_frame, text="📁 Open Output Folder", 
                  command=self.open_output_folder, width=20).pack(side='left', padx=5)
        
        # Log output
        log_frame = ttk.LabelFrame(self.tab_main, text="Log", padding=10)
        log_frame.pack(fill='both', expand=True, padx=10, pady=5)
        
        self.log_text = scrolledtext.ScrolledText(log_frame, height=15, wrap='word')
        self.log_text.pack(fill='both', expand=True)
        
        # Clear log button
        ttk.Button(log_frame, text="Clear Log", command=self.clear_log).pack(pady=5)
    
    def create_config_tab(self):
        """Create the configuration tab."""
        # Create a canvas with scrollbar for the config tab
        canvas = tk.Canvas(self.tab_config)
        scrollbar = ttk.Scrollbar(self.tab_config, orient="vertical", command=canvas.yview)
        scrollable_frame = ttk.Frame(canvas)
        
        scrollable_frame.bind(
            "<Configure>",
            lambda e: canvas.configure(scrollregion=canvas.bbox("all"))
        )
        
        canvas.create_window((0, 0), window=scrollable_frame, anchor="nw")
        canvas.configure(yscrollcommand=scrollbar.set)
        
        # Enable mousewheel scrolling
        def _on_mousewheel(event):
            canvas.yview_scroll(int(-1*(event.delta/120)), "units")
        canvas.bind_all("<MouseWheel>", _on_mousewheel)
        
        canvas.pack(side="left", fill="both", expand=True)
        scrollbar.pack(side="right", fill="y")
        
        # Credentials
        cred_frame = ttk.LabelFrame(scrollable_frame, text="Login Credentials", padding=10)
        cred_frame.pack(fill='x', padx=10, pady=5)
        
        ttk.Label(cred_frame, text="Email:").grid(row=0, column=0, sticky='e', padx=5, pady=2)
        self.email_entry = ttk.Entry(cred_frame, width=40)
        self.email_entry.grid(row=0, column=1, padx=5, pady=2)
        self.email_entry.insert(0, self.config.get('credentials', {}).get('email', ''))
        
        ttk.Label(cred_frame, text="Password:").grid(row=1, column=0, sticky='e', padx=5, pady=2)
        self.password_entry = ttk.Entry(cred_frame, width=40, show='*')
        self.password_entry.grid(row=1, column=1, padx=5, pady=2)
        self.password_entry.insert(0, self.config.get('credentials', {}).get('password', ''))
        
        # Filters - using checkbox groups
        filter_frame = ttk.LabelFrame(scrollable_frame, text="Filters (select multiple for sequential runs)", padding=10)
        filter_frame.pack(fill='x', padx=10, pady=5)
        
        filters = self.config.get('filters', {})
        
        # Initialize filter checkbox variables
        self.filter_vars = {}
        
        row = 0
        
        # TYP (radio buttons - single select)
        ttk.Label(filter_frame, text="Typ:").grid(row=row, column=0, sticky='ne', padx=5, pady=2)
        typ_frame = ttk.Frame(filter_frame)
        typ_frame.grid(row=row, column=1, sticky='w', padx=5, pady=2)
        
        self.typ_var = tk.StringVar(value=filters.get('typ', 'Byty'))
        typ_options = ['Byty', 'Rodinné domy', 'Stavební pozemky', 'Chaty a chalupy']
        for opt in typ_options:
            ttk.Radiobutton(typ_frame, text=opt, variable=self.typ_var, value=opt).pack(anchor='w')
        row += 1
        
        # KATEGORIE (checkboxes - multi select)
        ttk.Label(filter_frame, text="Kategorie:").grid(row=row, column=0, sticky='ne', padx=5, pady=2)
        kat_frame = ttk.Frame(filter_frame)
        kat_frame.grid(row=row, column=1, sticky='w', padx=5, pady=2)
        
        kategorie_config = filters.get('kategorie_selection', {'prodej': True, 'pronajem': True})
        if isinstance(kategorie_config, str):
            kategorie_config = {'prodej': True, 'pronajem': True}
        
        self.prodej_var = tk.BooleanVar(value=kategorie_config.get('prodej', True))
        self.pronajem_var = tk.BooleanVar(value=kategorie_config.get('pronajem', True))
        
        ttk.Checkbutton(kat_frame, text="Prodej", variable=self.prodej_var).pack(anchor='w')
        ttk.Checkbutton(kat_frame, text="Pronájem", variable=self.pronajem_var).pack(anchor='w')
        row += 1
        
        # STAV OBJEKTU (checkboxes - multi select)
        ttk.Label(filter_frame, text="Stav objektu:").grid(row=row, column=0, sticky='ne', padx=5, pady=2)
        stav_frame = ttk.Frame(filter_frame)
        stav_frame.grid(row=row, column=1, sticky='w', padx=5, pady=2)
        
        stav_config = filters.get('stav_objektu_selection', ['Velmi dobrý'])
        if isinstance(stav_config, str):
            stav_config = [stav_config] if stav_config else ['Velmi dobrý']
        
        self.filter_vars['stav_objektu'] = {}
        stav_options = ['Velmi dobrý', 'Novostavba', 'Před rekonstrukcí']
        for opt in stav_options:
            var = tk.BooleanVar(value=(opt in stav_config))
            self.filter_vars['stav_objektu'][opt] = var
            ttk.Checkbutton(stav_frame, text=opt, variable=var).pack(anchor='w')
        row += 1
        
        # KONSTRUKCE (checkboxes - multi select)
        ttk.Label(filter_frame, text="Konstrukce:").grid(row=row, column=0, sticky='ne', padx=5, pady=2)
        konst_frame = ttk.Frame(filter_frame)
        konst_frame.grid(row=row, column=1, sticky='w', padx=5, pady=2)
        
        konst_config = filters.get('konstrukce_selection', ['Panel'])
        if isinstance(konst_config, str):
            konst_config = [konst_config] if konst_config else ['Panel']
        
        self.filter_vars['konstrukce'] = {}
        konst_options = ['Panel', 'Cihla', 'Ostatní']
        for opt in konst_options:
            var = tk.BooleanVar(value=(opt in konst_config))
            self.filter_vars['konstrukce'][opt] = var
            ttk.Checkbutton(konst_frame, text=opt, variable=var).pack(anchor='w')
        row += 1
        
        # VLASTNICTVÍ (checkboxes - multi select)
        ttk.Label(filter_frame, text="Vlastnictví:").grid(row=row, column=0, sticky='ne', padx=5, pady=2)
        vlast_frame = ttk.Frame(filter_frame)
        vlast_frame.grid(row=row, column=1, sticky='w', padx=5, pady=2)
        
        vlast_config = filters.get('vlastnictvi_selection', ['Osobní'])
        if isinstance(vlast_config, str):
            vlast_config = [vlast_config] if vlast_config else ['Osobní']
        
        self.filter_vars['vlastnictvi'] = {}
        vlast_options = ['Osobní', 'Družstevní']
        for opt in vlast_options:
            var = tk.BooleanVar(value=(opt in vlast_config))
            self.filter_vars['vlastnictvi'][opt] = var
            ttk.Checkbutton(vlast_frame, text=opt, variable=var).pack(anchor='w')
        row += 1
        
        # Filter combinations info
        self.combo_label = ttk.Label(filter_frame, text="", foreground='blue')
        self.combo_label.grid(row=row, column=0, columnspan=2, sticky='w', padx=5, pady=5)
        self.update_combo_count()
        
        # Bind checkbox changes to update count
        for filter_type in self.filter_vars:
            for opt, var in self.filter_vars[filter_type].items():
                var.trace_add('write', lambda *args: self.update_combo_count())
        row += 1
        
        # Area range
        ttk.Label(filter_frame, text="Užitná plocha od:").grid(row=row, column=0, sticky='e', padx=5, pady=2)
        self.area_from = ttk.Entry(filter_frame, width=10)
        self.area_from.grid(row=row, column=1, sticky='w', padx=5, pady=2)
        self.area_from.insert(0, str(filters.get('uzitna_plocha_od', 30)))
        row += 1
        
        ttk.Label(filter_frame, text="Užitná plocha do:").grid(row=row, column=0, sticky='e', padx=5, pady=2)
        self.area_to = ttk.Entry(filter_frame, width=10)
        self.area_to.grid(row=row, column=1, sticky='w', padx=5, pady=2)
        self.area_to.insert(0, str(filters.get('uzitna_plocha_do', 80)))
        row += 1
        
        # Date range
        date_frame = ttk.LabelFrame(scrollable_frame, text="Date Range", padding=10)
        date_frame.pack(fill='x', padx=10, pady=5)
        
        date_range = self.config.get('date_range', {})
        
        ttk.Label(date_frame, text="Start Year:").grid(row=0, column=0, sticky='e', padx=5, pady=2)
        self.start_year = ttk.Spinbox(date_frame, from_=2010, to=2030, width=10)
        self.start_year.grid(row=0, column=1, sticky='w', padx=5, pady=2)
        self.start_year.set(date_range.get('start_year', 2015))
        
        ttk.Label(date_frame, text="End Year:").grid(row=1, column=0, sticky='e', padx=5, pady=2)
        self.end_year = ttk.Spinbox(date_frame, from_=2010, to=2030, width=10)
        self.end_year.grid(row=1, column=1, sticky='w', padx=5, pady=2)
        self.end_year.set(date_range.get('end_year', 2026))
        
        # Scraping settings
        scraping_frame = ttk.LabelFrame(scrollable_frame, text="Scraping Speed", padding=10)
        scraping_frame.pack(fill='x', padx=10, pady=5)
        
        scraping = self.config.get('scraping', {})
        
        ttk.Label(scraping_frame, text="Min delay (seconds):").grid(row=0, column=0, sticky='e', padx=5, pady=2)
        self.min_delay = ttk.Spinbox(scraping_frame, from_=1, to=30, width=10)
        self.min_delay.grid(row=0, column=1, sticky='w', padx=5, pady=2)
        self.min_delay.set(scraping.get('min_delay_seconds', 3))
        
        ttk.Label(scraping_frame, text="Max delay (seconds):").grid(row=1, column=0, sticky='e', padx=5, pady=2)
        self.max_delay = ttk.Spinbox(scraping_frame, from_=1, to=60, width=10)
        self.max_delay.grid(row=1, column=1, sticky='w', padx=5, pady=2)
        self.max_delay.set(scraping.get('max_delay_seconds', 7))
        
        # Save button
        ttk.Button(scrollable_frame, text="💾 Save Configuration", 
                  command=self.save_all_config).pack(pady=10)
    
    def update_combo_count(self):
        """Update the filter combinations count display."""
        try:
            combos = self.get_filter_combinations()
            count = len(combos)
            if count == 1:
                self.combo_label.config(text=f"→ {count} filter combination (1 run)")
            else:
                self.combo_label.config(text=f"→ {count} filter combinations ({count} sequential runs)")
        except:
            pass
    
    def get_filter_combinations(self):
        """Generate all filter combinations from selected checkboxes."""
        from itertools import product
        
        # Get selected options for each filter
        stav_selected = [opt for opt, var in self.filter_vars['stav_objektu'].items() if var.get()]
        konst_selected = [opt for opt, var in self.filter_vars['konstrukce'].items() if var.get()]
        vlast_selected = [opt for opt, var in self.filter_vars['vlastnictvi'].items() if var.get()]
        
        # Default to at least one if none selected
        if not stav_selected:
            stav_selected = ['Velmi dobrý']
        if not konst_selected:
            konst_selected = ['Panel']
        if not vlast_selected:
            vlast_selected = ['Osobní']
        
        # Generate all combinations
        combinations = list(product(stav_selected, konst_selected, vlast_selected))
        
        return [{'stav_objektu': s, 'konstrukce': k, 'vlastnictvi': v} for s, k, v in combinations]
    
    def create_cities_tab(self):
        """Create the cities configuration tab."""
        ttk.Label(self.tab_cities, text="Cities to scrape (one per line):").pack(padx=10, pady=5)
        
        self.cities_text = scrolledtext.ScrolledText(self.tab_cities, height=20, width=50)
        self.cities_text.pack(fill='both', expand=True, padx=10, pady=5)
        
        # Load current cities
        cities = self.config.get('cities', [])
        self.cities_text.insert('1.0', '\n'.join(cities))
        
        btn_frame = ttk.Frame(self.tab_cities)
        btn_frame.pack(pady=5)
        
        ttk.Button(btn_frame, text="💾 Save Cities", 
                  command=self.save_cities).pack(side='left', padx=5)
        ttk.Button(btn_frame, text="↻ Reset to Default", 
                  command=self.reset_cities).pack(side='left', padx=5)
    
    def create_help_tab(self):
        """Create the help tab."""
        help_text = """
SREALITY.CZ SCRAPER - HELP
═══════════════════════════════════════

QUICK START:
1. Go to "Configuration" tab and verify your login credentials
2. Select filters (multi-select creates sequential runs)
3. Check the cities list in "Cities" tab
4. Click "Start Scraping" on the Main tab
5. Wait for completion (can take many hours for full data)
6. Find results in the "output" folder

RUN MODES:
• Normal mode: Browser window visible (good for debugging)
• Headless mode: No browser window (faster, uses less resources)
• Test mode: Only scrapes 1 city and 1 year (for testing)

PARALLEL PROCESSING:
Use "Number of parallel browsers" to speed up scraping:
• 1 = Sequential (safest, default)
• 2-3 = Good balance of speed and safety
• 4-5 = Fastest but higher detection risk
Cities are automatically split between workers.
Results are merged automatically when all workers complete.
Workers start 30 seconds apart to avoid simultaneous logins.

FILTER COMBINATIONS:
Multi-select filters create sequential scraping runs:
• Stav objektu: Velmi dobrý, Novostavba, Před rekonstrukcí
• Konstrukce: Panel, Cihla, Ostatní
• Vlastnictví: Osobní, Družstevní

Example: If you select [Novostavba, Velmi dobrý] × [Panel, Cihla]:
→ 4 combinations = 4 sequential runs
→ Output files include filter suffix (e.g., novostavba_panel_...)

KATEGORIE SELECTION:
Use the checkboxes in Configuration to select which categories to scrape:
• ☑ Prodej - scrape sale listings only
• ☑ Pronájem - scrape rental listings only
• ☑ Both - scrape both sale and rental listings

DATA COLLECTED (for selected categories):
• Průměrná cena (Average price per m²)
• Průměrná doba inzerce (Average listing duration)  
• Aktivních nabídek (Active offers count)
• Počet nových nabídek (New offers count)
• Průměrný počet zobrazení za den (Average daily views)

OUTPUT FILES:
Results are saved as CSV files in the "output" folder.
Files include filter suffix when multiple combos selected:
• sreality_data_[FILTER]_prumerna_cena_prodej_TIMESTAMP.csv
• sreality_data_[FILTER]_prumerna_cena_pronajem_TIMESTAMP.csv
• etc.

TIPS:
• Run in Test mode first to verify everything works
• Start with 1 filter combination to test
• Start with 2 parallel workers to test parallel mode
• Increase delays if you get blocked
• Check scraper.log for detailed error messages
• The scraper saves progress periodically

TROUBLESHOOTING:
• Login failed: Check email/password in Configuration
• No data: Some date/city combinations may have no data
• Timeout: Increase delays or check internet connection
• Blocked: Wait a few hours, then try with higher delays
• Parallel issues: Try reducing number of workers
        """
        
        text_widget = scrolledtext.ScrolledText(self.tab_help, wrap='word')
        text_widget.pack(fill='both', expand=True, padx=10, pady=10)
        text_widget.insert('1.0', help_text)
        text_widget.config(state='disabled')
    
    def save_all_config(self):
        """Save all configuration settings."""
        try:
            self.config['credentials']['email'] = self.email_entry.get()
            self.config['credentials']['password'] = self.password_entry.get()
            
            # Typ (radio button - single value)
            self.config['filters']['typ'] = self.typ_var.get()
            
            # Kategorie selection (checkboxes)
            prodej_selected = self.prodej_var.get()
            pronajem_selected = self.pronajem_var.get()
            
            if not prodej_selected and not pronajem_selected:
                messagebox.showerror("Error", "Please select at least one kategorie (Prodej or Pronájem)")
                return
            
            self.config['filters']['kategorie_selection'] = {
                'prodej': prodej_selected,
                'pronajem': pronajem_selected
            }
            self.config['filters']['kategorie'] = 'Prodej' if prodej_selected else 'Pronájem'
            
            # Multi-select filters
            stav_selected = [opt for opt, var in self.filter_vars['stav_objektu'].items() if var.get()]
            konst_selected = [opt for opt, var in self.filter_vars['konstrukce'].items() if var.get()]
            vlast_selected = [opt for opt, var in self.filter_vars['vlastnictvi'].items() if var.get()]
            
            if not stav_selected:
                messagebox.showerror("Error", "Please select at least one Stav objektu")
                return
            if not konst_selected:
                messagebox.showerror("Error", "Please select at least one Konstrukce")
                return
            if not vlast_selected:
                messagebox.showerror("Error", "Please select at least one Vlastnictví")
                return
            
            self.config['filters']['stav_objektu_selection'] = stav_selected
            self.config['filters']['konstrukce_selection'] = konst_selected
            self.config['filters']['vlastnictvi_selection'] = vlast_selected
            
            # Legacy single values for backwards compatibility
            self.config['filters']['stav_objektu'] = stav_selected[0]
            self.config['filters']['konstrukce'] = konst_selected[0]
            self.config['filters']['vlastnictvi'] = vlast_selected[0]
            
            # Store filter combinations
            self.config['filters']['filter_combinations'] = self.get_filter_combinations()
            
            self.config['filters']['uzitna_plocha_od'] = int(self.area_from.get())
            self.config['filters']['uzitna_plocha_do'] = int(self.area_to.get())
            
            self.config['date_range']['start_year'] = int(self.start_year.get())
            self.config['date_range']['end_year'] = int(self.end_year.get())
            
            self.config['scraping']['min_delay_seconds'] = int(self.min_delay.get())
            self.config['scraping']['max_delay_seconds'] = int(self.max_delay.get())
            
            self.save_config()
            
            combos = len(self.config['filters']['filter_combinations'])
            messagebox.showinfo("Success", f"Configuration saved!\n{combos} filter combination(s) will be scraped sequentially.")
            
        except ValueError as e:
            messagebox.showerror("Error", f"Invalid value: {e}")
    
    def save_cities(self):
        """Save the cities list."""
        cities_str = self.cities_text.get('1.0', 'end').strip()
        cities = [c.strip() for c in cities_str.split('\n') if c.strip()]
        self.config['cities'] = cities
        self.save_config()
        messagebox.showinfo("Success", f"Saved {len(cities)} cities.")
    
    def reset_cities(self):
        """Reset cities to default list."""
        default_cities = [
            "Teplice", "Most", "Ústí nad Labem", "Chomutov", "Sokolov",
            "Beroun", "Králův Dvůr", "Kladno", "Hořovice", "Mariánské Lázně",
            "Tachov", "Cheb", "Ostrov", "Klatovy", "Plzeň", "Pardubice",
            "Hradec Králové", "Rychnov nad Kněžnou", "Chrudim", "Jihlava",
            "Havlíčkův Brod", "Humpolec", "Poděbrady", "Nymburk", "Liberec",
            "Česká Lípa", "České Budějovice", "Písek", "Mladá Boleslav"
        ]
        self.cities_text.delete('1.0', 'end')
        self.cities_text.insert('1.0', '\n'.join(default_cities))
        self.log("Cities reset to default list.")
    
    def log(self, message):
        """Add message to log."""
        timestamp = datetime.now().strftime('%H:%M:%S')
        self.log_text.insert('end', f"[{timestamp}] {message}\n")
        self.log_text.see('end')
    
    def clear_log(self):
        """Clear the log."""
        self.log_text.delete('1.0', 'end')
    
    def update_status(self):
        """Update status display."""
        if self.is_running:
            self.status_label.config(text="Running...", foreground='green')
            self.start_btn.config(state='disabled')
            self.stop_btn.config(state='normal')
        else:
            self.status_label.config(text="Ready", foreground='black')
            self.start_btn.config(state='normal')
            self.stop_btn.config(state='disabled')
    
    def apply_gui_settings_to_config(self, config_file):
        """Apply current GUI settings to the specified config file before running."""
        try:
            # Load the target config file
            with open(config_file, 'r', encoding='utf-8') as f:
                target_config = json.load(f)
            
            # Get cities from the GUI text box
            cities_str = self.cities_text.get('1.0', 'end').strip()
            cities = [c.strip() for c in cities_str.split('\n') if c.strip()]
            
            # Get date range from spinboxes
            start_year = int(self.start_year.get())
            end_year = int(self.end_year.get())
            
            # Get kategorie selection
            prodej = self.prodej_var.get()
            pronajem = self.pronajem_var.get()
            
            # Get typ
            typ = self.typ_var.get()
            
            # Get area range
            area_from = int(self.area_from.get())
            area_to = int(self.area_to.get())
            
            # Get scraping speed
            min_delay = int(self.min_delay.get())
            max_delay = int(self.max_delay.get())
            
            # Get credentials
            email = self.email_entry.get()
            password = self.password_entry.get()
            
            # Update the config
            target_config['cities'] = cities
            target_config['date_range'] = {
                'start_year': start_year,
                'end_year': end_year
            }
            
            # Ensure nested dicts exist
            if 'filters' not in target_config:
                target_config['filters'] = {}
            if 'scraping' not in target_config:
                target_config['scraping'] = {}
            if 'credentials' not in target_config:
                target_config['credentials'] = {}
            
            # Credentials
            target_config['credentials']['email'] = email
            target_config['credentials']['password'] = password
            
            # Filters
            target_config['filters']['typ'] = typ
            target_config['filters']['kategorie_selection'] = {
                'prodej': prodej,
                'pronajem': pronajem
            }
            target_config['filters']['kategorie'] = 'Prodej' if prodej else 'Pronájem'
            target_config['filters']['uzitna_plocha_od'] = area_from
            target_config['filters']['uzitna_plocha_do'] = area_to
            
            # Scraping speed
            target_config['scraping']['min_delay_seconds'] = min_delay
            target_config['scraping']['max_delay_seconds'] = max_delay
            
            # Save the updated config
            with open(config_file, 'w', encoding='utf-8') as f:
                json.dump(target_config, f, indent=4, ensure_ascii=False)
            
            self.log(f"Applied GUI settings to {config_file}:")
            self.log(f"  - {len(cities)} cities: {', '.join(cities[:5])}{'...' if len(cities) > 5 else ''}")
            self.log(f"  - Date range: {start_year} - {end_year}")
            self.log(f"  - Typ: {typ}, Area: {area_from}-{area_to}m²")
            self.log(f"  - Kategorie: {'Prodej' if prodej else ''}{' + ' if prodej and pronajem else ''}{'Pronájem' if pronajem else ''}")
            self.log(f"  - Delays: {min_delay}-{max_delay}s")
            
        except Exception as e:
            self.log(f"Warning: Could not apply settings to {config_file}: {e}")
            import traceback
            self.log(f"  {traceback.format_exc()}")
    
    def start_scraping(self):
        """Start the scraping process."""
        if self.is_running:
            return
        
        self.is_running = True
        self.update_status()
        
        num_workers = self.workers_var.get()
        
        # Determine config file based on batch selection
        batch_selection = self.batch_combo.current()
        config_files = [
            'config.json',           # All cities
            'config_batch1.json',    # Batch 1
            'config_batch2.json',    # Batch 2
            'config_batch3.json',    # Batch 3
            'config_batch4.json',    # Batch 4
            'config_batch5.json',    # Batch 5
        ]
        selected_config = config_files[batch_selection] if batch_selection < len(config_files) else 'config.json'
        
        # AUTO-SAVE: Apply current GUI settings to the selected config file
        self.apply_gui_settings_to_config(selected_config)
        
        # Get filter combinations
        filter_combos = self.get_filter_combinations()
        
        # Log filter combinations
        self.log(f"Filter combinations to run: {len(filter_combos)}")
        for idx, combo in enumerate(filter_combos, 1):
            self.log(f"  {idx}. {combo['stav_objektu']} / {combo['konstrukce']} / {combo['vlastnictvi']}")
        
        if num_workers == 1:
            # Single worker - run filter combos sequentially
            self.log(f"Starting scraper with {len(filter_combos)} filter combination(s)...")
            self.log(f"Using config: {selected_config}")
            
            # Start in thread
            thread = threading.Thread(target=self.run_scraper_sequential_combos, 
                                      args=(selected_config, filter_combos))
            thread.daemon = True
            thread.start()
        else:
            # Parallel workers - run filter combos sequentially, but each combo with parallel workers
            self.log(f"Starting parallel scraper ({num_workers} workers) with {len(filter_combos)} filter combination(s)...")
            self.log(f"Using config: {selected_config}")
            
            # Start parallel execution in thread
            thread = threading.Thread(target=self.run_scraper_parallel_combos, 
                                      args=(selected_config, num_workers, filter_combos))
            thread.daemon = True
            thread.start()
    
    def run_scraper_sequential_combos(self, config_file, filter_combos):
        """Run scraper for each filter combination sequentially."""
        import re
        total_combos = len(filter_combos)
        
        try:
            for i, combo in enumerate(filter_combos, 1):
                if not self.is_running:
                    self.root.after(0, self.log, "Scraping stopped by user")
                    break
                
                self.root.after(0, self.log, f"\n{'='*50}")
                self.root.after(0, self.log, f"FILTER COMBO {i}/{total_combos}")
                self.root.after(0, self.log, f"  Stav: {combo['stav_objektu']}")
                self.root.after(0, self.log, f"  Konstrukce: {combo['konstrukce']}")
                self.root.after(0, self.log, f"  Vlastnictví: {combo['vlastnictvi']}")
                self.root.after(0, self.log, f"{'='*50}")
                
                # Reset progress bar for this combo
                self.root.after(0, lambda: self.progress_var.set(0))
                self.root.after(0, lambda c=i, t=total_combos: self.progress_label.config(
                    text=f"Combo {c}/{t}: Starting..."))
                
                cmd = [sys.executable, 'scraper.py', '--config', config_file,
                       '--filter-combo', json.dumps(combo)]
                
                if self.headless_var.get():
                    cmd.append('--headless')
                
                if self.test_var.get():
                    cmd.append('--test')
                
                self.scraper_process = subprocess.Popen(
                    cmd,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    text=True,
                    bufsize=1
                )
                
                # Read output and parse progress
                for line in self.scraper_process.stdout:
                    self.root.after(0, self.log, line.strip())
                    self.parse_and_update_progress(line, i, total_combos)
                
                self.scraper_process.wait()
                
                if self.scraper_process.returncode != 0:
                    self.root.after(0, self.log, f"Combo {i} exited with code {self.scraper_process.returncode}")
            
            # Mark complete
            self.root.after(0, lambda: self.progress_var.set(100))
            self.root.after(0, lambda: self.progress_label.config(text="Complete!"))
            
            self.root.after(0, self.log, f"\n✓ All {total_combos} filter combinations completed!")
            self.root.after(0, lambda: messagebox.showinfo("Complete", 
                f"Scraping finished!\n{total_combos} filter combination(s) completed.\nCheck the output folder for results."))
        
        except Exception as e:
            self.root.after(0, self.log, f"Error: {e}")
        
        finally:
            self.is_running = False
            self.root.after(0, self.update_status)
    
    def parse_and_update_progress(self, line, combo_num=1, total_combos=1):
        """Parse progress from log line like '[117/180]' and update progress bar."""
        import re
        # Match patterns like "[117/180]" or "[1/180]"
        match = re.search(r'\[(\d+)/(\d+)\]', line)
        if match:
            current = int(match.group(1))
            total = int(match.group(2))
            if total > 0:
                # Calculate overall progress including combo progress
                combo_progress = (combo_num - 1) / total_combos
                within_combo_progress = current / total / total_combos
                overall = (combo_progress + within_combo_progress) * 100
                
                self.root.after(0, lambda p=overall: self.progress_var.set(p))
                self.root.after(0, lambda c=combo_num, t=total_combos, cur=current, tot=total: 
                    self.progress_label.config(text=f"Combo {c}/{t}: {cur}/{tot} tasks"))
    
    def parse_worker_progress(self, line, worker_id, worker_progress, combo_num, total_combos, num_workers):
        """Parse progress from parallel worker and update combined progress bar."""
        import re
        # Match patterns like "[117/180]" or "[1/180]"
        match = re.search(r'\[(\d+)/(\d+)\]', line)
        if match:
            current = int(match.group(1))
            total = int(match.group(2))
            worker_progress[worker_id] = (current, total)
            
            # Calculate combined progress from all workers
            total_current = sum(p[0] for p in worker_progress.values())
            total_tasks = sum(p[1] for p in worker_progress.values())
            
            if total_tasks > 0:
                # Calculate overall progress including combo progress
                combo_progress = (combo_num - 1) / total_combos
                within_combo_progress = total_current / total_tasks / total_combos
                overall = (combo_progress + within_combo_progress) * 100
                
                self.root.after(0, lambda p=overall: self.progress_var.set(p))
                self.root.after(0, lambda c=combo_num, t=total_combos, cur=total_current, tot=total_tasks, w=num_workers: 
                    self.progress_label.config(text=f"Combo {c}/{t}: {cur}/{tot} tasks ({w} workers)"))
    
    def run_scraper_parallel_combos(self, config_file, num_workers, filter_combos):
        """Run scraper with parallel workers for each filter combination sequentially."""
        import time as time_module
        
        total_combos = len(filter_combos)
        
        try:
            for combo_idx, combo in enumerate(filter_combos, 1):
                if not self.is_running:
                    self.root.after(0, self.log, "Scraping stopped by user")
                    break
                
                self.root.after(0, self.log, f"\n{'='*50}")
                self.root.after(0, self.log, f"FILTER COMBO {combo_idx}/{total_combos}")
                self.root.after(0, self.log, f"  Stav: {combo['stav_objektu']}")
                self.root.after(0, self.log, f"  Konstrukce: {combo['konstrukce']}")
                self.root.after(0, self.log, f"  Vlastnictví: {combo['vlastnictvi']}")
                self.root.after(0, self.log, f"  Running with {num_workers} parallel workers")
                self.root.after(0, self.log, f"{'='*50}")
                
                self.scraper_processes = []
                
                # Launch workers with staggered start
                for i in range(1, num_workers + 1):
                    cmd = [sys.executable, 'scraper.py', 
                           '--config', config_file,
                           '--chunk', f'{i}/{num_workers}',
                           '--filter-combo', json.dumps(combo)]
                    
                    if self.headless_var.get():
                        cmd.append('--headless')
                    
                    if self.test_var.get():
                        cmd.append('--test')
                    
                    self.root.after(0, self.log, f"[Worker {i}/{num_workers}] Starting...")
                    
                    proc = subprocess.Popen(
                        cmd,
                        stdout=subprocess.PIPE,
                        stderr=subprocess.STDOUT,
                        text=True,
                        bufsize=1
                    )
                    self.scraper_processes.append((i, proc))
                    
                    # Stagger starts
                    if i < num_workers:
                        self.root.after(0, self.log, f"Waiting 30 seconds before starting next worker...")
                        time_module.sleep(30)
                
                # Monitor all processes for this combo
                active_procs = list(self.scraper_processes)
                worker_progress = {i: (0, 1) for i in range(1, num_workers + 1)}  # (current, total)
                
                # Reset progress for this combo
                self.root.after(0, lambda: self.progress_var.set(0))
                self.root.after(0, lambda c=combo_idx, t=total_combos: self.progress_label.config(
                    text=f"Combo {c}/{t}: Running {num_workers} workers..."))
                
                while active_procs and self.is_running:
                    still_active = []
                    for worker_id, proc in active_procs:
                        line = proc.stdout.readline()
                        if line:
                            self.root.after(0, self.log, f"[W{worker_id}] {line.strip()}")
                            # Parse progress from this worker
                            self.parse_worker_progress(line, worker_id, worker_progress, 
                                                       combo_idx, total_combos, num_workers)
                        
                        if proc.poll() is None:
                            still_active.append((worker_id, proc))
                        else:
                            self.root.after(0, self.log, 
                                f"[Worker {worker_id}] Finished with code {proc.returncode}")
                    
                    active_procs = still_active
                    if active_procs:
                        time_module.sleep(0.1)
                
                # Merge results for this combo
                self.root.after(0, self.log, f"Merging results for combo {combo_idx}...")
                self.merge_parallel_outputs(num_workers, combo)
            
            # Mark complete
            self.root.after(0, lambda: self.progress_var.set(100))
            self.root.after(0, lambda: self.progress_label.config(text="Complete!"))
            
            self.root.after(0, self.log, f"\n✓ All {total_combos} filter combinations completed!")
            self.root.after(0, lambda: messagebox.showinfo("Complete", 
                f"Parallel scraping finished!\n{total_combos} filter combination(s) with {num_workers} workers each.\nCheck the output folder for results."))
        
        except Exception as e:
            self.root.after(0, self.log, f"Error in parallel execution: {e}")
        
        finally:
            self.is_running = False
            self.scraper_processes = []
            self.root.after(0, self.update_status)
    
    def run_scraper_single(self, cmd):
        """Run a single scraper process (legacy, kept for compatibility)."""
        try:
            self.scraper_process = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                bufsize=1
            )
            
            # Read output
            for line in self.scraper_process.stdout:
                self.root.after(0, self.log, line.strip())
            
            self.scraper_process.wait()
            
            if self.scraper_process.returncode == 0:
                self.root.after(0, self.log, "Scraping completed successfully!")
                self.root.after(0, lambda: messagebox.showinfo("Complete", 
                    "Scraping finished! Check the output folder for results."))
            else:
                self.root.after(0, self.log, f"Scraper exited with code {self.scraper_process.returncode}")
        
        except Exception as e:
            self.root.after(0, self.log, f"Error: {e}")
        
        finally:
            self.is_running = False
            self.root.after(0, self.update_status)
    
    def merge_parallel_outputs(self, num_workers, combo=None):
        """Merge CSV outputs from parallel workers into single files."""
        import glob
        from datetime import datetime
        
        # Use output directory from config
        output_dir = Path(self.config.get('output', {}).get('directory', 'output'))
        timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
        
        # Generate filter suffix if combo provided
        def sanitize(s):
            return s.lower().replace(' ', '').replace('í', 'i').replace('á', 'a').replace('ý', 'y').replace('ě', 'e').replace('ž', 'z').replace('š', 's').replace('č', 'c').replace('ř', 'r').replace('ů', 'u').replace('ú', 'u')
        
        filter_suffix = ""
        if combo:
            filter_suffix = f"_{sanitize(combo.get('stav_objektu', ''))}_{sanitize(combo.get('konstrukce', ''))}_{sanitize(combo.get('vlastnictvi', ''))}"
        
        self.root.after(0, self.log, f"  DEBUG: filter_suffix = '{filter_suffix}'")
        self.root.after(0, self.log, f"  DEBUG: output_dir = {output_dir.absolute()}")
        
        # List all CSV files in output directory for debugging
        all_csvs = list(output_dir.glob('*.csv'))
        self.root.after(0, self.log, f"  DEBUG: Found {len(all_csvs)} CSV files in output:")
        for csv_file in all_csvs:  # Show all files
            self.root.after(0, self.log, f"    - {csv_file.name}")
        
        if len(all_csvs) == 0:
            self.root.after(0, self.log, f"  WARNING: No CSV files found! Workers may not have finished.")
        
        # Find all metric types
        metrics = [
            'prumerna_cena_prodej', 'prumerna_cena_pronajem',
            'prumerna_doba_inzerce_prodej', 'prumerna_doba_inzerce_pronajem',
            'aktivnich_nabidek_prodej', 'aktivnich_nabidek_pronajem',
            'pocet_novych_nabidek_prodej', 'pocet_novych_nabidek_pronajem',
            'prumerny_pocet_zobrazeni_prodej', 'prumerny_pocet_zobrazeni_pronajem',
        ]
        
        merged_count = 0
        for metric in metrics:
            # Find chunk files for this metric (with timestamp and filter suffix)
            chunk_files = []
            for i in range(1, num_workers + 1):
                # Pattern includes filter suffix if present
                pattern = str(output_dir / f'sreality_data{filter_suffix}_chunk{i}_{metric}_*.csv')
                self.root.after(0, self.log, f"  DEBUG: Searching pattern: {pattern}")
                files = glob.glob(pattern)
                self.root.after(0, self.log, f"  DEBUG: Found {len(files)} files")
                chunk_files.extend(files)
            
            if not chunk_files:
                continue
            
            self.root.after(0, self.log, f"  Merging {len(chunk_files)} files for {metric}...")
            
            # Filter columns that should be preserved (not merged as dates)
            filter_columns = {'City', 'city', 'Kategorie', 'Typ', 'Stav objektu', 'Konstrukce', 
                              'Vlastnictví', 'Užitná plocha od', 'Užitná plocha do', 'V okolí', ''}
            
            # Read and merge all chunk files
            all_data = {}  # city -> {'filters': {...}, 'dates': {date -> value}}
            all_dates = set()
            
            for filepath in chunk_files:
                try:
                    self.root.after(0, self.log, f"    DEBUG: Reading {Path(filepath).name}")
                    with open(filepath, 'r', encoding='utf-8-sig') as f:
                        reader = csv.DictReader(f)
                        fieldnames = reader.fieldnames
                        self.root.after(0, self.log, f"    DEBUG: Columns: {fieldnames}")
                        
                        # First column contains city names
                        first_col = fieldnames[0] if fieldnames else None
                        
                        row_count = 0
                        for row in reader:
                            row_count += 1
                            # Try multiple ways to get city name
                            city = row.get('City') or row.get('city') or row.get(first_col) or row.get('')
                            if not city:
                                self.root.after(0, self.log, f"    DEBUG: Row {row_count} has no City: {dict(row)}")
                                continue
                            
                            if city not in all_data:
                                all_data[city] = {'filters': {}, 'dates': {}}
                                # Store filter values from this row
                                for col in filter_columns:
                                    if col in row and row[col]:
                                        all_data[city]['filters'][col] = row[col]
                            
                            # Add date values (non-filter columns)
                            for key, value in row.items():
                                # Skip filter columns
                                if key in filter_columns or key == first_col:
                                    continue
                                if value:
                                    all_data[city]['dates'][key] = value
                                    all_dates.add(key)
                        self.root.after(0, self.log, f"    DEBUG: Read {row_count} rows, cities so far: {len(all_data)}")
                except Exception as e:
                    self.root.after(0, self.log, f"    ERROR reading {filepath}: {e}")
                    import traceback
                    self.root.after(0, self.log, f"    {traceback.format_exc()}")
            
            self.root.after(0, self.log, f"    DEBUG: Total cities: {len(all_data)}, dates: {len(all_dates)}")
            
            if not all_data:
                self.root.after(0, self.log, f"    WARNING: No data extracted from files!")
                continue
            
            # Write merged file (include filter suffix)
            merged_path = output_dir / f'sreality_data{filter_suffix}_{metric}_{timestamp}.csv'
            sorted_dates = sorted(all_dates)
            
            # Define column order: filter columns first, then dates
            filter_col_order = ['City', 'Kategorie', 'Typ', 'Stav objektu', 'Konstrukce', 
                               'Vlastnictví', 'Užitná plocha od', 'Užitná plocha do', 'V okolí']
            
            with open(merged_path, 'w', newline='', encoding='utf-8-sig') as f:
                writer = csv.writer(f)
                writer.writerow(filter_col_order + sorted_dates)
                for city in sorted(all_data.keys()):
                    city_info = all_data[city]
                    # Build filter values row
                    filter_vals = [city_info['filters'].get(col, city if col == 'City' else '') 
                                   for col in filter_col_order]
                    # Add date values
                    date_vals = [city_info['dates'].get(d, '') for d in sorted_dates]
                    writer.writerow(filter_vals + date_vals)
            
            self.root.after(0, self.log, f"    ✓ Created {merged_path.name}")
            merged_count += 1
            
            # Delete chunk files only after SUCCESSFUL merge
            for filepath in chunk_files:
                try:
                    os.remove(filepath)
                    self.root.after(0, self.log, f"    Deleted: {Path(filepath).name}")
                except:
                    pass
        
        # Only clean up remaining chunk files if at least some files were merged
        if merged_count > 0:
            # Clean up any remaining chunk progress files (without timestamp)
            for i in range(1, num_workers + 1):
                pattern = str(output_dir / f'sreality_data{filter_suffix}_chunk{i}_*.csv')
                remaining = glob.glob(pattern)
                for f in remaining:
                    # Don't delete files with timestamps - those are final outputs
                    try:
                        os.remove(f)
                        self.root.after(0, self.log, f"    Cleaned up: {Path(f).name}")
                    except:
                        pass
        
        if merged_count == 0:
            self.root.after(0, self.log, f"  WARNING: No files were merged! Check file patterns above.")
            self.root.after(0, self.log, f"  NOTE: Chunk files were NOT deleted - you can find them in the output folder.")
        
        self.root.after(0, self.log, "  Merge complete!")
    
    def stop_scraping(self):
        """Stop the scraping process(es)."""
        self.log("Stopping scraper(s)...")
        
        # Stop single process
        if self.scraper_process:
            self.scraper_process.terminate()
        
        # Stop parallel processes
        if self.scraper_processes:
            for worker_id, proc in self.scraper_processes:
                try:
                    proc.terminate()
                    self.log(f"  Stopped worker {worker_id}")
                except:
                    pass
            self.scraper_processes = []
        
        self.is_running = False
        self.update_status()
    
    def open_output_folder(self):
        """Open the output folder in file explorer."""
        # Use output directory from config
        output_dir = Path(self.config.get('output', {}).get('directory', 'output'))
        output_dir.mkdir(exist_ok=True)
        
        if sys.platform == 'win32':
            os.startfile(output_dir)
        elif sys.platform == 'darwin':
            subprocess.run(['open', output_dir])
        else:
            subprocess.run(['xdg-open', output_dir])


def main():
    root = tk.Tk()
    
    # Set icon if available
    try:
        root.iconbitmap('icon.ico')
    except:
        pass
    
    app = ScraperGUI(root)
    root.mainloop()


if __name__ == '__main__':
    main()
