# Sreality.cz Real Estate Price Scraper v2.0

A user-friendly scraper with **human-like behavior patterns** to avoid detection.

## 🤖 Human-Like Behavior Features

This scraper mimics human browsing patterns to avoid bot detection:

### Randomized Scraping Order
- **7 different strategies**: years_first, cities_chunks, random_walk, time_periods, reverse_chrono, interest_based, mixed
- Never scrapes data sequentially (too robotic!)
- Randomly selects a strategy at startup
- Occasionally "revisits" earlier data points

### Natural Mouse Movements
- Uses **Bezier curves** for realistic mouse paths
- Moves to random positions while "thinking"
- Variable movement speed (faster in middle, slower at edges)

### Human-Like Typing
- Variable typing speed (faster for common letters)
- Slower after punctuation and capital letters
- **Occasional typos** that get corrected (like real typing!)
- Uses nearby keyboard keys for realistic mistakes

### Scrolling Behavior
- Scrolls in chunks (like a mouse wheel)
- More likely to scroll down than up
- Variable scroll amounts
- Scrolls while "reading" content

### Breaks and Pauses
- ☕ **Coffee breaks**: 1-5 minutes after 25-50 actions
- 📱 **Short breaks**: 15-45 seconds (checking phone, stretching)
- 🤔 **Thinking pauses**: 1.5-5 seconds (reading content)
- 😴 **Distraction pauses**: Occasional longer delays (10% chance)
- **Micro pauses**: 50-400ms between actions (reaction time)

### Session Management
- Refreshes page after breaks
- Saves progress periodically (resume on crash)
- Randomized save intervals

## 📋 Quick Start

### Windows
1. Extract the ZIP file
2. Double-click `launch_gui.bat` (GUI) or `run_scraper.bat` (command line)
3. First run installs dependencies automatically

### macOS / Linux
```bash
chmod +x run_scraper.sh
./run_scraper.sh
```

## 📊 Data Collected

For each city × date combination, the scraper collects data for **both Prodej (Sale) and Pronájem (Rental)**:

| Field | Description |
|-------|-------------|
| Průměrná cena | Average price per m² |
| Průměrná doba inzerce | Average listing duration (days) |
| Aktivních nabídek | Number of active offers |
| Počet nových nabídek | Number of new offers |
| Průměrný počet zobrazení za den | Average daily views |

## 📁 Output

Results in the `output` folder (10 data files + errors):

**Prodej (Sale) files:**
- `sreality_data_prumerna_cena_prodej_TIMESTAMP.csv`
- `sreality_data_prumerna_doba_inzerce_prodej_TIMESTAMP.csv`
- `sreality_data_aktivnich_nabidek_prodej_TIMESTAMP.csv`
- `sreality_data_pocet_novych_nabidek_prodej_TIMESTAMP.csv`
- `sreality_data_prumerny_pocet_zobrazeni_prodej_TIMESTAMP.csv`

**Pronájem (Rental) files:**
- `sreality_data_prumerna_cena_pronajem_TIMESTAMP.csv`
- `sreality_data_prumerna_doba_inzerce_pronajem_TIMESTAMP.csv`
- `sreality_data_aktivnich_nabidek_pronajem_TIMESTAMP.csv`
- `sreality_data_pocet_novych_nabidek_pronajem_TIMESTAMP.csv`
- `sreality_data_prumerny_pocet_zobrazeni_pronajem_TIMESTAMP.csv`

**Other:**
- `sreality_data_errors_TIMESTAMP.csv` (if any)
- `progress.json` (for resume capability)

Format: Cities in rows, dates (2015-01 to 2026-01) in columns.

## ⚙️ Configuration

Edit `config.json`:

### Credentials
```json
"credentials": {
    "email": "your@email.com",
    "password": "your_password"
}
```

### Filters
```json
"filters": {
    "typ": "Byty",
    "kategorie": "Prodej",
    "stav_objektu": "velmi dobrý",
    "uzitna_plocha_od": 30,
    "uzitna_plocha_do": 80,
    "konstrukce": "Panel",
    "vlastnictvi": "Osobní",
    "v_okoli": "0.5"
}
```

### Scraping Speed
Adjust to balance speed vs. detection risk:
```json
"scraping": {
    "min_delay_seconds": 3,     // Minimum wait between actions
    "max_delay_seconds": 7,     // Maximum wait between actions
    "page_load_timeout_seconds": 30,
    "retry_attempts": 3,
    "retry_delay_seconds": 10
}
```

**Tips:**
- Lower delays = faster but higher detection risk
- Higher delays = slower but safer
- Recommended: 3-7 seconds (default)

## 🎯 Scraping Strategies

The scraper randomly picks one of these:

| Strategy | Description |
|----------|-------------|
| `years_first` | All cities for each year, then next year |
| `cities_chunks` | Process 2-5 cities at a time, interleaved |
| `random_walk` | Semi-random with clustering near previous task |
| `time_periods` | Random 2-4 year chunks |
| `reverse_chrono` | Start from recent data, go backwards |
| `interest_based` | Prioritize random "interesting" cities |
| `mixed` | Combine multiple strategies |

## 🛡️ Run Modes

| Mode | Command | Description |
|------|---------|-------------|
| Normal | `python scraper.py` | Browser visible (for debugging) |
| Headless | `python scraper.py --headless` | No browser window (faster) |
| Test | `python scraper.py --test` | 2 cities, 1 year (quick test) |

## ⏱️ Estimated Time

With default settings (29 cities × ~130 months = ~3,770 tasks):
- **With breaks**: 25-35 hours
- **Headless + lower delays**: 12-18 hours

The scraper saves progress, so you can safely interrupt and resume.

## 📝 Resume Capability

If interrupted, the scraper automatically resumes:
1. Progress saved to `output/progress.json`
2. Already-completed tasks are skipped
3. Data preserved across sessions

## 🔍 Troubleshooting

### "Login failed"
- Check credentials in `config.json`
- Run in Normal mode to see the browser
- Website may have changed login flow

### "Location not found"
- Check city spelling
- Try exact name from sreality.cz

### "Blocked / Access denied"
- Increase delays in config
- Wait a few hours before retrying
- Try different time of day

### "Timeout errors"
- Increase `page_load_timeout_seconds`
- Check internet connection
- Website may be slow

## 📊 Working with Output

CSV files work with:
- Microsoft Excel
- Google Sheets
- LibreOffice Calc
- Python/Pandas

```python
import pandas as pd
df = pd.read_csv('sreality_data_prumerna_cena_*.csv', index_col=0)
print(df.describe())
```

## 🔐 Security

- Credentials stored in `config.json` (keep private!)
- Don't share or commit config file
- Consider changing password after use

## 📄 Files

| File | Purpose |
|------|---------|
| `scraper.py` | Main scraper with human behavior |
| `scraper_gui.py` | Graphical interface |
| `config.json` | Settings and credentials |
| `run_scraper.bat` | Windows launcher |
| `run_scraper.sh` | Mac/Linux launcher |
| `launch_gui.bat` | GUI launcher (Windows) |
| `requirements.txt` | Python dependencies |

---

For personal use only. Respect sreality.cz terms of service.
