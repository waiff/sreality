#!/usr/bin/env python3
"""
Sreality.cz Real Estate Price Scraper v2.0
==========================================
Scrapes real estate price data from sreality.cz/ceny-nemovitosti
with human-like behavior patterns to avoid detection.

Features:
- Randomized scraping order (not sequential)
- Variable delays and micro-pauses
- Human-like mouse movements and scrolling
- Occasional typing mistakes and corrections
- Coffee breaks during long sessions
- Multiple scraping strategies
- Resume capability

Requirements: pip install playwright pandas
Setup: playwright install chromium
"""

import json
import csv
import os
import sys
import time
import random
import logging
import math
import traceback
import re
from datetime import datetime
from pathlib import Path
from typing import Optional, Dict, List, Any, Tuple
from collections import defaultdict

try:
    from playwright.sync_api import sync_playwright, Page, Browser, TimeoutError as PlaywrightTimeout
    import pandas as pd
except ImportError:
    print("Missing dependencies. Please run:")
    print("  pip install playwright pandas")
    print("  playwright install chromium")
    sys.exit(1)


# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler('scraper.log', encoding='utf-8'),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger(__name__)


class HumanBehavior:
    """Simulates human-like behavior patterns to avoid bot detection."""
    
    # Shared break file for synchronized breaks across parallel workers
    BREAK_FILE = "output/synchronized_break.json"
    
    def __init__(self, page: Page, fast_mode: bool = False, worker_id: int = 0):
        self.page = page
        self.fast_mode = fast_mode
        self.worker_id = worker_id  # 0 = single worker, 1-N for parallel
        self.actions_since_break = 0
        self.session_start = time.time()
        self.last_action_time = time.time()
        
        # Ensure output directory exists for break file
        os.makedirs("output", exist_ok=True)
    
    def _write_break_schedule(self, break_until: float, break_type: str):
        """Write a synchronized break schedule for all workers."""
        try:
            break_data = {
                'break_until': break_until,
                'break_type': break_type,
                'initiated_by': self.worker_id,
                'initiated_at': time.time()
            }
            with open(self.BREAK_FILE, 'w') as f:
                json.dump(break_data, f)
            logger.debug(f"[Worker {self.worker_id}] Wrote break schedule until {break_until}")
        except Exception as e:
            logger.warning(f"Could not write break schedule: {e}")
    
    def _check_break_schedule(self) -> tuple:
        """Check if a synchronized break is active.
        Returns: (is_active, seconds_remaining, break_type)
        """
        try:
            if not os.path.exists(self.BREAK_FILE):
                return (False, 0, None)
            
            with open(self.BREAK_FILE, 'r') as f:
                break_data = json.load(f)
            
            break_until = break_data.get('break_until', 0)
            break_type = break_data.get('break_type', 'unknown')
            now = time.time()
            
            if now < break_until:
                return (True, break_until - now, break_type)
            else:
                return (False, 0, None)
        except Exception as e:
            logger.debug(f"Could not read break schedule: {e}")
            return (False, 0, None)
    
    def _clear_break_schedule(self):
        """Clear the break schedule file."""
        try:
            if os.path.exists(self.BREAK_FILE):
                os.remove(self.BREAK_FILE)
        except:
            pass
        
    def random_delay(self, min_sec: float = 1.0, max_sec: float = 3.0):
        """Random delay with occasional longer pauses."""
        if self.fast_mode:
            # Fast mode: minimal delay (10-20% of normal)
            time.sleep(random.uniform(min_sec * 0.1, max_sec * 0.2))
            return
            
        # Sometimes humans get distracted (10% chance of longer pause)
        if random.random() < 0.1:
            delay = random.uniform(max_sec * 2, max_sec * 4)
            logger.debug(f"Distraction pause: {delay:.1f}s")
        # Sometimes they're quick (15% chance of shorter pause)
        elif random.random() < 0.15:
            delay = random.uniform(min_sec * 0.5, min_sec)
        else:
            delay = random.uniform(min_sec, max_sec)
        
        time.sleep(delay)
        self.last_action_time = time.time()
    
    def micro_pause(self):
        """Very short pause between actions (like reaction time)."""
        if self.fast_mode:
            time.sleep(0.05)
            return
        time.sleep(random.uniform(0.05, 0.4))
    
    def thinking_pause(self):
        """Pause like human is reading/thinking."""
        if self.fast_mode:
            time.sleep(0.2)
            return
        time.sleep(random.uniform(1.5, 5.0))
    
    def coffee_break(self, synchronized: bool = True):
        """Longer break (1-5 minutes) - humans take breaks!"""
        if self.fast_mode:
            logger.info("[FAST MODE] Skipping coffee break")
            return
        duration = random.uniform(60, 300)
        
        if synchronized and self.worker_id > 0:
            # Write break schedule for other workers
            break_until = time.time() + duration
            self._write_break_schedule(break_until, 'coffee')
            logger.info(f"[Worker {self.worker_id}] ☕ SYNCHRONIZED coffee break: {duration/60:.1f} minutes (all workers pausing)...")
        else:
            logger.info(f"[BREAK] Taking a coffee break: {duration/60:.1f} minutes...")
        
        time.sleep(duration)
        self._clear_break_schedule()
        self.session_start = time.time()
        self.actions_since_break = 0
    
    def short_break(self, synchronized: bool = True):
        """Short break (15-45 seconds) - stretching, checking phone."""
        if self.fast_mode:
            logger.info("[FAST MODE] Skipping short break")
            return
        duration = random.uniform(15, 45)
        
        if synchronized and self.worker_id > 0:
            # Write break schedule for other workers
            break_until = time.time() + duration
            self._write_break_schedule(break_until, 'short')
            logger.info(f"[Worker {self.worker_id}] 🔄 SYNCHRONIZED short break: {duration:.0f}s (all workers pausing)...")
        else:
            logger.info(f"Taking a short break: {duration:.0f}s")
        
        time.sleep(duration)
        self._clear_break_schedule()
    
    def _wait_for_synchronized_break(self) -> bool:
        """Check if another worker initiated a break and wait for it.
        Returns True if we waited for a break.
        """
        is_active, remaining, break_type = self._check_break_schedule()
        
        if is_active and remaining > 1:  # Only wait if more than 1 second remaining
            logger.info(f"[Worker {self.worker_id}] ⏸️ Joining synchronized {break_type} break ({remaining:.0f}s remaining)...")
            time.sleep(remaining + random.uniform(0.5, 2))  # Small random offset to avoid all resuming at exact same moment
            self.actions_since_break = 0  # Reset counter since we took a break
            return True
        
        return False
    
    def should_take_break(self) -> bool:
        """Decide if it's time for a break. Supports synchronized breaks for parallel workers."""
        if self.fast_mode:
            return False  # No breaks in fast mode
        
        # First, check if another worker initiated a break
        if self.worker_id > 0:
            if self._wait_for_synchronized_break():
                return True
            
        self.actions_since_break += 1
        
        # Random threshold for breaks (25-50 actions)
        break_threshold = random.randint(25, 50)
        
        # Definite break after threshold
        if self.actions_since_break >= break_threshold:
            # 70% short break, 30% coffee break
            if random.random() < 0.7:
                self.short_break(synchronized=(self.worker_id > 0))
            else:
                self.coffee_break(synchronized=(self.worker_id > 0))
            self.actions_since_break = 0
            return True
        
        # Random chance of spontaneous break (2%)
        if random.random() < 0.02:
            self.short_break(synchronized=(self.worker_id > 0))
            return True
        
        return False
    
    def bezier_curve_points(self, start: Tuple[float, float], end: Tuple[float, float], 
                           control_points: int = 2) -> List[Tuple[float, float]]:
        """Generate points along a bezier curve for natural mouse movement."""
        points = [start]
        
        # Generate random control points
        controls = []
        for _ in range(control_points):
            cx = start[0] + (end[0] - start[0]) * random.uniform(0.2, 0.8) + random.uniform(-50, 50)
            cy = start[1] + (end[1] - start[1]) * random.uniform(0.2, 0.8) + random.uniform(-50, 50)
            controls.append((cx, cy))
        
        # Generate curve points
        steps = random.randint(10, 25)
        for i in range(1, steps + 1):
            t = i / steps
            # Quadratic bezier if 1 control point, cubic if 2
            if len(controls) == 1:
                x = (1-t)**2 * start[0] + 2*(1-t)*t * controls[0][0] + t**2 * end[0]
                y = (1-t)**2 * start[1] + 2*(1-t)*t * controls[0][1] + t**2 * end[1]
            else:
                x = (1-t)**3 * start[0] + 3*(1-t)**2*t * controls[0][0] + 3*(1-t)*t**2 * controls[1][0] + t**3 * end[0]
                y = (1-t)**3 * start[1] + 3*(1-t)**2*t * controls[0][1] + 3*(1-t)*t**2 * controls[1][1] + t**3 * end[1]
            points.append((x, y))
        
        return points
    
    def human_mouse_move(self, x: int, y: int):
        """Move mouse in a human-like curved path."""
        if self.fast_mode:
            return  # Skip mouse movements in fast mode
            
        try:
            viewport = self.page.viewport_size
            if not viewport:
                return
            
            # Clamp to viewport
            x = max(10, min(x, viewport['width'] - 10))
            y = max(10, min(y, viewport['height'] - 10))
            
            # Get current position (approximate)
            current_x = random.randint(100, viewport['width'] - 100)
            current_y = random.randint(100, viewport['height'] - 100)
            
            # Generate curved path
            points = self.bezier_curve_points((current_x, current_y), (x, y))
            
            # Move through points with variable speed
            for px, py in points:
                self.page.mouse.move(px, py)
                # Variable speed - faster in middle, slower at ends
                time.sleep(random.uniform(0.01, 0.05))
            
        except Exception as e:
            logger.debug(f"Mouse move error: {e}")
    
    def random_mouse_movement(self):
        """Move mouse to random position naturally."""
        if self.fast_mode:
            return  # Skip in fast mode
            
        try:
            viewport = self.page.viewport_size
            if not viewport:
                return
            
            x = random.randint(100, viewport['width'] - 100)
            y = random.randint(100, viewport['height'] - 100)
            
            self.human_mouse_move(x, y)
            self.micro_pause()
        except:
            pass
    
    def random_scroll(self):
        """Scroll like a human browsing."""
        if self.fast_mode:
            return  # Skip in fast mode
            
        try:
            # Humans scroll down more than up
            direction = random.choices(['down', 'up'], weights=[0.75, 0.25])[0]
            
            # Variable scroll amount
            if random.random() < 0.3:
                # Big scroll
                amount = random.randint(300, 600)
            else:
                # Small scroll
                amount = random.randint(50, 200)
            
            if direction == 'up':
                amount = -amount
            
            # Scroll in chunks (like mouse wheel)
            chunks = random.randint(2, 5)
            chunk_amount = amount // chunks
            
            for _ in range(chunks):
                self.page.mouse.wheel(0, chunk_amount)
                time.sleep(random.uniform(0.05, 0.15))
            
            self.micro_pause()
        except:
            pass
    
    def human_type(self, element, text: str, clear_first: bool = True):
        """Type text with human-like patterns including typos."""
        try:
            if clear_first:
                element.click()
                self.micro_pause()
                # Select all and delete (more human than .clear())
                element.press('Control+a')
                self.micro_pause()
                element.press('Backspace')
                self.micro_pause()
            
            i = 0
            while i < len(text):
                char = text[i]
                
                # Variable typing speed based on character
                if char in ' .,':
                    delay = random.randint(80, 200)  # Slower after punctuation
                elif char.isupper():
                    delay = random.randint(100, 180)  # Slower for caps
                else:
                    delay = random.randint(30, 120)
                
                # Occasional pause (thinking)
                if random.random() < 0.03:
                    time.sleep(random.uniform(0.3, 0.8))
                
                # Occasional typo (3% chance, not on last char)
                if random.random() < 0.03 and i < len(text) - 1:
                    # Type wrong character
                    if char.isalpha():
                        # Nearby key on keyboard
                        keyboard_neighbors = {
                            'a': 'sqwz', 'b': 'vghn', 'c': 'xdfv', 'd': 'serfcx',
                            'e': 'wsdfr', 'f': 'drtgvc', 'g': 'ftyhbv', 'h': 'gyujnb',
                            'i': 'ujklo', 'j': 'huiknm', 'k': 'jiolm', 'l': 'kop',
                            'm': 'njk', 'n': 'bhjm', 'o': 'iklp', 'p': 'ol',
                            'q': 'wa', 'r': 'edft', 's': 'awedxz', 't': 'rfgy',
                            'u': 'yhjik', 'v': 'cfgb', 'w': 'qase', 'x': 'zsdc',
                            'y': 'tghu', 'z': 'asx'
                        }
                        neighbors = keyboard_neighbors.get(char.lower(), 'abcdefghijklmnopqrstuvwxyz')
                        wrong_char = random.choice(neighbors)
                        if char.isupper():
                            wrong_char = wrong_char.upper()
                    else:
                        wrong_char = random.choice('abcdefghijklmnopqrstuvwxyz')
                    
                    element.type(wrong_char, delay=delay)
                    time.sleep(random.uniform(0.15, 0.4))  # Notice mistake
                    element.press('Backspace')
                    time.sleep(random.uniform(0.1, 0.25))
                
                # Type the actual character
                element.type(char, delay=delay)
                i += 1
            
        except Exception as e:
            logger.debug(f"Typing error: {e}")
            # Fallback to simple fill
            try:
                element.fill(text)
            except:
                pass
    
    def do_random_action(self):
        """Occasionally do random human-like actions."""
        action_roll = random.random()
        
        if action_roll < 0.25:
            # Mouse movement
            self.random_mouse_movement()
        elif action_roll < 0.40:
            # Scroll
            self.random_scroll()
        elif action_roll < 0.45:
            # Thinking pause
            self.thinking_pause()
        elif action_roll < 0.48:
            # Multiple scrolls (reading)
            for _ in range(random.randint(2, 4)):
                self.random_scroll()
                time.sleep(random.uniform(0.5, 1.5))
        # else: do nothing (52%)
    
    def simulate_reading(self, element=None):
        """Simulate reading content on page."""
        if element:
            try:
                element.scroll_into_view_if_needed()
            except:
                pass
        
        # Reading time varies
        read_time = random.uniform(1.0, 3.5)
        time.sleep(read_time)
        
        # Maybe scroll while reading
        if random.random() < 0.4:
            self.random_scroll()


class ScrapingStrategy:
    """
    Manages scraping order to appear more human.
    
    Humans don't scrape data sequentially - they might:
    - Start with cities they're interested in
    - Jump between years randomly
    - Do some months, switch to another city
    - Come back to fill in gaps
    """
    
    STRATEGIES = [
        'years_first',      # Do all cities for each year, then move to next year
        'cities_chunks',    # Do chunks of cities at a time
        'random_walk',      # Semi-random with clustering
        'time_periods',     # Random time period chunks
        'reverse_chrono',   # Start from recent, go back
        'interest_based',   # Simulate "interested" in certain cities
        'mixed'             # Combine strategies
    ]
    
    def __init__(self, cities: List[str], start_year: int, end_year: int):
        self.cities = cities
        self.start_year = start_year
        self.end_year = end_year
        self.current_strategy = random.choice(self.STRATEGIES)
        logger.info(f"Selected scraping strategy: {self.current_strategy}")
        
    def generate_scraping_order(self) -> List[Tuple[str, int, int]]:
        """Generate human-like scraping order."""
        all_tasks = []
        
        for city in self.cities:
            for year in range(self.start_year, self.end_year + 1):
                for month in range(1, 13):
                    # Skip future dates
                    if year == datetime.now().year and month > datetime.now().month:
                        continue
                    if year > datetime.now().year:
                        continue
                    all_tasks.append((city, year, month))
        
        strategy_methods = {
            'years_first': self._years_first,
            'cities_chunks': self._cities_chunks,
            'random_walk': self._random_walk,
            'time_periods': self._time_periods,
            'reverse_chrono': self._reverse_chrono,
            'interest_based': self._interest_based,
            'mixed': self._mixed
        }
        
        return strategy_methods[self.current_strategy](all_tasks)
    
    def _years_first(self, tasks: List) -> List:
        """Process by years - all January data first, then February, etc."""
        result = []
        
        # Group by (year, month)
        by_period = defaultdict(list)
        for task in tasks:
            city, year, month = task
            by_period[(year, month)].append(task)
        
        # Get all periods and shuffle their order
        periods = list(by_period.keys())
        
        # Don't fully randomize - do years in random order, but months somewhat sequential
        years = list(set(p[0] for p in periods))
        random.shuffle(years)
        
        for year in years:
            # Get months for this year, shuffle them
            year_periods = [p for p in periods if p[0] == year]
            random.shuffle(year_periods)
            
            for period in year_periods:
                period_tasks = by_period[period]
                random.shuffle(period_tasks)
                result.extend(period_tasks)
        
        return result
    
    def _cities_chunks(self, tasks: List) -> List:
        """Process cities in random chunks."""
        result = []
        
        # Group by city
        by_city = defaultdict(list)
        for task in tasks:
            by_city[task[0]].append(task)
        
        # Shuffle cities
        city_order = list(by_city.keys())
        random.shuffle(city_order)
        
        # Process in chunks of 2-5 cities
        i = 0
        while i < len(city_order):
            chunk_size = random.randint(2, 5)
            chunk_cities = city_order[i:i + chunk_size]
            
            # Interleave tasks from chunk cities
            chunk_tasks = []
            for city in chunk_cities:
                city_tasks = by_city[city]
                # Within city, randomize order somewhat
                random.shuffle(city_tasks)
                chunk_tasks.extend(city_tasks)
            
            # Shuffle within chunk
            random.shuffle(chunk_tasks)
            result.extend(chunk_tasks)
            
            i += chunk_size
        
        return result
    
    def _random_walk(self, tasks: List) -> List:
        """Semi-random with tendency to stay near previous task."""
        tasks = tasks.copy()
        result = []
        remaining = list(range(len(tasks)))
        
        # Start random
        idx = random.choice(remaining)
        remaining.remove(idx)
        result.append(tasks[idx])
        
        while remaining:
            last = result[-1]
            last_city, last_year, last_month = last
            
            # 65% chance to pick something "nearby"
            if random.random() < 0.65 and remaining:
                nearby = []
                for idx in remaining:
                    city, year, month = tasks[idx]
                    # Same city or within 2 years
                    if city == last_city or abs(year - last_year) <= 2:
                        nearby.append(idx)
                
                if nearby:
                    idx = random.choice(nearby)
                else:
                    idx = random.choice(remaining)
            else:
                idx = random.choice(remaining)
            
            remaining.remove(idx)
            result.append(tasks[idx])
        
        return result
    
    def _time_periods(self, tasks: List) -> List:
        """Process in random time period chunks (e.g., 2020-2022, then 2015-2017)."""
        result = []
        
        years = list(range(self.start_year, self.end_year + 1))
        random.shuffle(years)
        
        # Group into 2-4 year chunks
        chunks = []
        chunk_size = random.randint(2, 4)
        for i in range(0, len(years), chunk_size):
            chunk_years = set(years[i:i + chunk_size])
            chunk_tasks = [t for t in tasks if t[1] in chunk_years]
            random.shuffle(chunk_tasks)
            chunks.append(chunk_tasks)
        
        random.shuffle(chunks)
        
        for chunk in chunks:
            result.extend(chunk)
        
        return result
    
    def _reverse_chrono(self, tasks: List) -> List:
        """Start from recent data and go backwards."""
        # Sort by date descending
        sorted_tasks = sorted(tasks, key=lambda x: (x[1], x[2]), reverse=True)
        
        # Add some randomization
        result = []
        window_size = random.randint(20, 40)
        
        i = 0
        while i < len(sorted_tasks):
            window = sorted_tasks[i:i + window_size]
            random.shuffle(window)
            result.extend(window)
            i += window_size
        
        return result
    
    def _interest_based(self, tasks: List) -> List:
        """Simulate being more interested in certain cities."""
        # Pick 3-5 "priority" cities
        priority_cities = random.sample(self.cities, min(random.randint(3, 5), len(self.cities)))
        
        # Split tasks
        priority_tasks = [t for t in tasks if t[0] in priority_cities]
        other_tasks = [t for t in tasks if t[0] not in priority_cities]
        
        random.shuffle(priority_tasks)
        random.shuffle(other_tasks)
        
        # Interleave - do more priority tasks first
        result = []
        pi, oi = 0, 0
        
        while pi < len(priority_tasks) or oi < len(other_tasks):
            # 70% priority, 30% other
            if pi < len(priority_tasks) and (oi >= len(other_tasks) or random.random() < 0.7):
                # Add 1-3 priority tasks
                batch = random.randint(1, 3)
                for _ in range(batch):
                    if pi < len(priority_tasks):
                        result.append(priority_tasks[pi])
                        pi += 1
            else:
                # Add 1-2 other tasks
                batch = random.randint(1, 2)
                for _ in range(batch):
                    if oi < len(other_tasks):
                        result.append(other_tasks[oi])
                        oi += 1
        
        return result
    
    def _mixed(self, tasks: List) -> List:
        """Combine multiple strategies for unpredictability."""
        result = []
        remaining = tasks.copy()
        
        while remaining:
            # Pick random strategy for this batch
            batch_strategy = random.choice(['sequential', 'random', 'reverse', 'city_focus', 'year_focus'])
            batch_size = random.randint(15, 60)
            batch = remaining[:batch_size]
            remaining = remaining[batch_size:]
            
            if batch_strategy == 'sequential':
                pass
            elif batch_strategy == 'random':
                random.shuffle(batch)
            elif batch_strategy == 'reverse':
                batch.reverse()
            elif batch_strategy == 'city_focus':
                batch.sort(key=lambda x: (x[0], x[1], x[2]))
                # Randomize within city
                by_city = defaultdict(list)
                for t in batch:
                    by_city[t[0]].append(t)
                batch = []
                cities = list(by_city.keys())
                random.shuffle(cities)
                for city in cities:
                    random.shuffle(by_city[city])
                    batch.extend(by_city[city])
            elif batch_strategy == 'year_focus':
                batch.sort(key=lambda x: (x[1], x[0], x[2]))
            
            result.extend(batch)
            
            # Occasionally "revisit" earlier task
            if random.random() < 0.05 and len(result) > 20:
                revisit_idx = random.randint(0, len(result) - 15)
                task = result.pop(revisit_idx)
                result.append(task)
        
        return result


class SrealityScraper:
    """Main scraper with human-like behavior."""
    
    BASE_URL = "https://www.sreality.cz"
    PRICES_URL = "https://www.sreality.cz/ceny-nemovitosti"
    
    def __init__(self, config_path: str = "config.json", fast_mode: bool = False, worker_id: int = 0):
        self.config = self._load_config(config_path)
        self.fast_mode = fast_mode
        self.worker_id = worker_id  # 0 = single worker, 1-N for parallel
        self.browser: Optional[Browser] = None
        self.page: Optional[Page] = None
        self.playwright = None
        self.human: Optional[HumanBehavior] = None
        
        if fast_mode:
            logger.info("[FAST MODE] Running with reduced delays")
        
        if worker_id > 0:
            logger.info(f"[Worker {worker_id}] Synchronized breaks enabled")
        
        # Data storage
        self.data_prumerna_cena: Dict[str, Dict[str, Any]] = {}
        self.data_prumerna_doba: Dict[str, Dict[str, Any]] = {}
        self.data_aktivnich_nabidek: Dict[str, Dict[str, Any]] = {}
        self.data_pocet_novych: Dict[str, Dict[str, Any]] = {}
        self.data_prumerny_zobrazeni: Dict[str, Dict[str, Any]] = {}
        
        # Pronájem data (separate dictionaries)
        self.data_prumerna_cena_pronajem: Dict[str, Dict[str, Any]] = {}
        self.data_prumerna_doba_pronajem: Dict[str, Dict[str, Any]] = {}
        self.data_aktivnich_nabidek_pronajem: Dict[str, Dict[str, Any]] = {}
        self.data_pocet_novych_pronajem: Dict[str, Dict[str, Any]] = {}
        self.data_prumerny_zobrazeni_pronajem: Dict[str, Dict[str, Any]] = {}
        
        # Error tracking
        self.errors: List[Dict[str, str]] = []
        
        # Progress tracking
        self.completed_tasks = set()
        self.load_progress()
        
        os.makedirs(self.config['output']['directory'], exist_ok=True)
    
    def _load_config(self, config_path: str) -> dict:
        try:
            with open(config_path, 'r', encoding='utf-8') as f:
                return json.load(f)
        except FileNotFoundError:
            logger.error(f"Configuration file not found: {config_path}")
            sys.exit(1)
        except json.JSONDecodeError as e:
            logger.error(f"Invalid JSON: {e}")
            sys.exit(1)
    
    def load_progress(self):
        """Load previous progress for resume."""
        progress_file = Path(self.config['output']['directory']) / 'progress.json'
        if progress_file.exists():
            try:
                with open(progress_file, 'r') as f:
                    data = json.load(f)
                    self.completed_tasks = set(tuple(t) for t in data.get('completed', []))
                    # Prodej data
                    self.data_prumerna_cena = data.get('prumerna_cena', {})
                    self.data_prumerna_doba = data.get('prumerna_doba', {})
                    self.data_aktivnich_nabidek = data.get('aktivnich_nabidek', {})
                    self.data_pocet_novych = data.get('pocet_novych', {})
                    self.data_prumerny_zobrazeni = data.get('prumerny_zobrazeni', {})
                    # Pronájem data
                    self.data_prumerna_cena_pronajem = data.get('prumerna_cena_pronajem', {})
                    self.data_prumerna_doba_pronajem = data.get('prumerna_doba_pronajem', {})
                    self.data_aktivnich_nabidek_pronajem = data.get('aktivnich_nabidek_pronajem', {})
                    self.data_pocet_novych_pronajem = data.get('pocet_novych_pronajem', {})
                    self.data_prumerny_zobrazeni_pronajem = data.get('prumerny_zobrazeni_pronajem', {})
                    logger.info(f"Resumed: {len(self.completed_tasks)} tasks already completed")
            except:
                pass
    
    def save_progress(self):
        """Save current progress."""
        progress_file = Path(self.config['output']['directory']) / 'progress.json'
        try:
            with open(progress_file, 'w', encoding='utf-8') as f:
                json.dump({
                    'completed': list(self.completed_tasks),
                    # Prodej data
                    'prumerna_cena': self.data_prumerna_cena,
                    'prumerna_doba': self.data_prumerna_doba,
                    'aktivnich_nabidek': self.data_aktivnich_nabidek,
                    'pocet_novych': self.data_pocet_novych,
                    'prumerny_zobrazeni': self.data_prumerny_zobrazeni,
                    # Pronájem data
                    'prumerna_cena_pronajem': self.data_prumerna_cena_pronajem,
                    'prumerna_doba_pronajem': self.data_prumerna_doba_pronajem,
                    'aktivnich_nabidek_pronajem': self.data_aktivnich_nabidek_pronajem,
                    'pocet_novych_pronajem': self.data_pocet_novych_pronajem,
                    'prumerny_zobrazeni_pronajem': self.data_prumerny_zobrazeni_pronajem,
                    'last_updated': datetime.now().isoformat()
                }, f, ensure_ascii=False, indent=2)
            logger.debug("Progress saved")
        except Exception as e:
            logger.warning(f"Could not save progress: {e}")
    
    def start_browser(self, headless: bool = False):
        """Start browser with randomized settings."""
        logger.info("Starting browser...")
        self.playwright = sync_playwright().start()
        
        # Randomize viewport (different monitors)
        viewports = [
            (1920, 1080), (1366, 768), (1536, 864), 
            (1440, 900), (1280, 720), (1600, 900)
        ]
        width, height = random.choice(viewports)
        
        # Add slight variation
        width += random.randint(-50, 50)
        height += random.randint(-30, 30)
        
        # Random user agents
        user_agents = [
            'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/121.0.0.0 Safari/537.36',
            'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
            'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/121.0.0.0 Safari/537.36',
            'Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:122.0) Gecko/20100101 Firefox/122.0',
            'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.2 Safari/605.1.15',
        ]
        
        # In fast mode, no slow_mo
        slow_mo = 0 if self.fast_mode else random.randint(30, 100)
        
        self.browser = self.playwright.chromium.launch(
            headless=headless,
            slow_mo=slow_mo
        )
        
        self.page = self.browser.new_page(
            viewport={'width': width, 'height': height},
            user_agent=random.choice(user_agents),
            locale='cs-CZ',
            timezone_id='Europe/Prague'
        )
        
        self.page.set_default_timeout(self.config['scraping']['page_load_timeout_seconds'] * 1000)
        self.human = HumanBehavior(self.page, fast_mode=self.fast_mode, worker_id=self.worker_id)
        
        logger.info(f"Browser started: {width}x{height}" + (" [FAST MODE]" if self.fast_mode else "") + (f" [Worker {self.worker_id}]" if self.worker_id > 0 else ""))
    
    def close_browser(self):
        if self.browser:
            self.browser.close()
        if self.playwright:
            self.playwright.stop()
        logger.info("Browser closed.")
    
    def handle_cookies(self):
        """Accept cookies if prompted."""
        try:
            self.human.random_delay(1, 2)
            
            selectors = [
                'button:has-text("Souhlasím")',
                'button:has-text("Přijmout")',
                'button:has-text("Přijmout vše")',
                'button:has-text("Accept")',
                '[data-testid="cookie-accept"]',
            ]
            
            for selector in selectors:
                try:
                    button = self.page.locator(selector).first
                    if button.is_visible(timeout=1000):
                        self.human.random_mouse_movement()
                        self.human.micro_pause()
                        button.click()
                        logger.info("Cookies accepted.")
                        self.human.random_delay(0.5, 1)
                        return True
                except:
                    continue
            return False
        except:
            return False
    
    def login(self) -> bool:
        """Login with human-like behavior - handles Seznam.cz two-step popup login."""
        logger.info("Logging in...")
        
        try:
            self.page.goto(self.BASE_URL)
            self.human.random_delay(2, 4)
            self.human.random_mouse_movement()
            
            self.handle_cookies()
            
            # Look around first (human behavior)
            self.human.random_scroll()
            self.human.thinking_pause()
            
            # Find login button
            login_selectors = [
                'a:has-text("Přihlásit")',
                'button:has-text("Přihlásit")',
                'a[href*="login"]',
                'a[href*="prihlaseni"]',
            ]
            
            # Prepare to catch popup window
            login_popup = None
            
            for selector in login_selectors:
                try:
                    btn = self.page.locator(selector).first
                    if btn.is_visible(timeout=2000):
                        self.human.random_mouse_movement()
                        self.human.micro_pause()
                        
                        # Try to catch popup when clicking login
                        try:
                            with self.page.expect_popup(timeout=10000) as popup_info:
                                btn.click()
                            login_popup = popup_info.value
                            logger.info("Login opened in popup window - switching to it")
                        except:
                            btn.click()
                        break
                except:
                    continue
            
            # If login opened in popup, switch to that page
            if login_popup:
                login_page = login_popup
                login_page.wait_for_load_state('domcontentloaded')
                self.human.random_delay(2, 3)
                try:
                    login_page.wait_for_load_state('networkidle', timeout=10000)
                except:
                    pass
            else:
                login_page = self.page
            
            self.human.random_delay(1, 2)
            
            # Handle cookies/consent on login page
            try:
                cookie_selectors = [
                    'button:has-text("Souhlasím")',
                    'button:has-text("Přijmout")',
                    'button:has-text("OK")',
                ]
                for sel in cookie_selectors:
                    try:
                        btn = login_page.locator(sel).first
                        if btn.is_visible(timeout=2000):
                            btn.click()
                            logger.info(f"Clicked consent button on login page")
                            self.human.random_delay(1, 2)
                            break
                    except:
                        continue
            except:
                pass
            
            email = self.config['credentials']['email']
            password = self.config['credentials']['password']
            
            logger.info(f"Login page URL: {login_page.url}")
            
            # ============ STEP 1: ENTER EMAIL ============
            logger.info("Step 1: Entering email...")
            
            email_selector = '#login-username'
            try:
                email_input = login_page.locator(email_selector)
                email_input.wait_for(state='visible', timeout=10000)
                logger.info(f"Email field found")
                
                # Click with force to bypass floating label overlay
                email_input.click(force=True)
                self.human.random_delay(0.2, 0.5)
                
                # Clear and type email
                email_input.fill(email)
                logger.info("Email entered")
                
            except Exception as e:
                logger.error(f"Failed to enter email: {e}")
                try:
                    login_page.screenshot(path="login_debug_email.png")
                    logger.info("Saved debug screenshot to login_debug_email.png")
                except:
                    pass
                if login_popup:
                    login_popup.close()
                return False
            
            self.human.random_delay(0.5, 1.0)
            
            # ============ STEP 2: CLICK CONTINUE/NEXT BUTTON ============
            logger.info("Step 2: Clicking continue button...")
            
            continue_selectors = [
                'button[type="submit"]',
                'button:has-text("Pokračovat")',
                'button:has-text("Další")',
                'button:has-text("Continue")',
                'button:has-text("Next")',
                'input[type="submit"]',
                'form button',
            ]
            
            continue_clicked = False
            for selector in continue_selectors:
                try:
                    btn = login_page.locator(selector).first
                    if btn.is_visible(timeout=1000):
                        self.human.micro_pause()
                        btn.click()
                        logger.info(f"Clicked continue button: {selector}")
                        continue_clicked = True
                        break
                except:
                    continue
            
            if not continue_clicked:
                # Try pressing Enter as fallback
                try:
                    email_input.press('Enter')
                    logger.info("Pressed Enter to continue")
                    continue_clicked = True
                except:
                    pass
            
            # Wait for password page to load
            self.human.random_delay(2, 4)
            
            # ============ STEP 3: ENTER PASSWORD ============
            logger.info("Step 3: Entering password...")
            
            password_selectors = [
                '#login-password',
                'input[type="password"]',
                'input[name="password"]',
                'input[autocomplete="current-password"]',
            ]
            
            pass_filled = False
            for selector in password_selectors:
                try:
                    pass_input = login_page.locator(selector).first
                    if pass_input.is_visible(timeout=5000):
                        logger.info(f"Password field found: {selector}")
                        
                        # Click with force to bypass any overlay
                        pass_input.click(force=True)
                        self.human.random_delay(0.2, 0.5)
                        
                        # Fill password
                        pass_input.fill(password)
                        logger.info("Password entered")
                        pass_filled = True
                        break
                except Exception as e:
                    logger.debug(f"Password selector {selector} failed: {e}")
                    continue
            
            if not pass_filled:
                logger.error("Could not find password field")
                try:
                    login_page.screenshot(path="login_debug_password.png")
                    logger.info("Saved debug screenshot to login_debug_password.png")
                except:
                    pass
                if login_popup:
                    login_popup.close()
                return False
            
            self.human.random_delay(0.5, 1.0)
            
            # ============ STEP 4: CLICK LOGIN BUTTON ============
            logger.info("Step 4: Clicking login button...")
            
            submit_selectors = [
                'button[type="submit"]',
                'button:has-text("Přihlásit")',
                'button:has-text("Přihlásit se")',
                'input[type="submit"]',
                'form button',
            ]
            
            for selector in submit_selectors:
                try:
                    btn = login_page.locator(selector).first
                    if btn.is_visible(timeout=1000):
                        self.human.micro_pause()
                        btn.click()
                        logger.info("Login button clicked")
                        break
                except:
                    continue
            
            self.human.random_delay(3, 5)
            
            # Handle popup closing
            if login_popup:
                try:
                    login_popup.wait_for_close(timeout=15000)
                    logger.info("Login popup closed")
                except:
                    try:
                        login_popup.close()
                    except:
                        pass
                
                self.human.random_delay(1, 2)
                self.page.reload()
                self.human.random_delay(2, 3)
            
            # Verify login on main page
            indicators = [
                'a:has-text("Můj účet")',
                'a:has-text("Odhlásit")',
                'a:has-text("Moje")',
            ]
            for ind in indicators:
                try:
                    if self.page.locator(ind).first.is_visible(timeout=3000):
                        logger.info("Login successful!")
                        return True
                except:
                    continue
            
            logger.warning("Could not verify login, continuing anyway...")
            return True
            
        except Exception as e:
            logger.error(f"Login error: {e}")
            logger.error(traceback.format_exc())
            return False
    
    def navigate_to_prices_page(self):
        """Go to prices page with consent page handling."""
        logger.info(f"Navigating to {self.PRICES_URL}")
        self.human.random_mouse_movement()
        
        max_attempts = 5
        for attempt in range(max_attempts):
            try:
                self.page.goto(self.PRICES_URL, wait_until="domcontentloaded")
                self.human.random_delay(2, 3)
                
                current_url = self.page.url
                
                # Check if redirected to consent page
                if "cmp.seznam.cz" in current_url or "nastaveni-souhlasu" in current_url:
                    logger.warning(f"Attempt {attempt + 1}: Redirected to consent page")
                    
                    # Try multiple consent button selectors
                    consent_selectors = [
                        'button:has-text("Souhlasím")',
                        'button:has-text("Přijmout vše")',
                        'button:has-text("Přijmout")',
                        'button:has-text("Povolit vše")',
                        'button:has-text("OK")',
                        'button.consent-give',
                        '[data-testid="cw-consent-submit"]',
                    ]
                    
                    clicked = False
                    for selector in consent_selectors:
                        try:
                            btn = self.page.locator(selector).first
                            if btn.is_visible(timeout=1500):
                                btn.click()
                                logger.info(f"Clicked consent button: {selector}")
                                clicked = True
                                self.human.random_delay(3, 4)
                                break
                        except:
                            continue
                    
                    if not clicked:
                        logger.warning("Could not find consent button")
                        self.page.keyboard.press('Enter')
                        self.human.random_delay(2, 3)
                    
                    continue
                
                # Verify we're on the right page
                if "ceny-nemovitosti" in current_url and "sreality.cz" in current_url:
                    logger.info(f"Successfully on prices page: {current_url}")
                    self.handle_cookies()
                    self.human.simulate_reading()
                    self.human.random_scroll()
                    return True
                else:
                    logger.warning(f"Unexpected URL: {current_url}, retrying...")
                    self.human.random_delay(1, 2)
                    
            except Exception as e:
                logger.warning(f"Navigation attempt {attempt + 1} failed: {e}")
                self.human.random_delay(2, 3)
        
        logger.error("Failed to navigate to prices page after multiple attempts")
        return False
    
    def set_base_filters(self):
        """Set base filters with verification - matches the actual sreality.cz filter UI."""
        logger.info("Setting base filters...")
        filters = self.config['filters']
        
        try:
            self.human.random_delay(1, 2)
            
            # Take screenshot before setting filters
            self.page.screenshot(path="debug_before_filters.png")
            
            # 1. TYP: Byty (radio button)
            try:
                byty = self.page.locator('label:has-text("Byty"), span:has-text("Byty")').first
                if byty.is_visible(timeout=3000):
                    byty.click(force=True)
                    logger.info("  [OK] Typ: Byty")
                    self.human.random_delay(0.5, 1)
            except Exception as e:
                logger.warning(f"  [WARN] Typ: Byty - {e}")
            
            # 2. KATEGORIE: Set based on selection (Prodej by default)
            # Get kategorie selection
            kategorie_sel = self.config.get('filters', {}).get('kategorie_selection', {'prodej': True, 'pronajem': True})
            if isinstance(kategorie_sel, str):
                kategorie_sel = {'prodej': True, 'pronajem': True}
            
            # Set initial kategorie - Prodej if selected, otherwise Pronájem
            initial_kategorie = 'Prodej' if kategorie_sel.get('prodej', True) else 'Pronájem'
            try:
                kat_selector = self.page.locator(f'label:has-text("{initial_kategorie}"), span:has-text("{initial_kategorie}")').first
                if kat_selector.is_visible(timeout=3000):
                    kat_selector.click(force=True)
                    logger.info(f"  [OK] Kategorie: {initial_kategorie}")
                    self.human.random_delay(0.5, 1)
            except Exception as e:
                logger.warning(f"  [WARN] Kategorie: {initial_kategorie} - {e}")
            
            # 3. STAV OBJEKTU: Velmi dobrý (checkbox)
            try:
                stav = self.page.locator('label:has-text("Velmi dobrý"), span:has-text("Velmi dobrý")').first
                if stav.is_visible(timeout=3000):
                    stav.click(force=True)
                    logger.info("  [OK] Stav objektu: Velmi dobrý")
                    self.human.random_delay(0.5, 1)
            except Exception as e:
                logger.warning(f"  [WARN] Stav objektu: Velmi dobrý - {e}")
            
            # 4. UŽITNÁ PLOCHA: Od/Do - use specific IDs
            try:
                od_input = self.page.locator('#usable_area_from, input[name="usable_area_from"]').first
                do_input = self.page.locator('#usable_area_to, input[name="usable_area_to"]').first
                
                if od_input.is_visible(timeout=2000):
                    od_input.click(force=True)
                    od_input.fill(str(filters.get('uzitna_plocha_od', 30)))
                    od_input.press('Tab')
                    logger.info(f"  [OK] Užitná plocha Od: {filters.get('uzitna_plocha_od', 30)}")
                    self.human.random_delay(0.3, 0.6)
                
                if do_input.is_visible(timeout=2000):
                    do_input.click(force=True)
                    do_input.fill(str(filters.get('uzitna_plocha_do', 90)))
                    do_input.press('Tab')
                    logger.info(f"  [OK] Užitná plocha Do: {filters.get('uzitna_plocha_do', 90)}")
                    self.human.random_delay(0.3, 0.6)
            except Exception as e:
                logger.warning(f"  [WARN] Užitná plocha - {e}")
            
            # 5. KONSTRUKCE: Panel (checkbox)
            try:
                panel = self.page.locator('label:has-text("Panel"), span:has-text("Panel")').first
                if panel.is_visible(timeout=3000):
                    panel.click(force=True)
                    logger.info("  [OK] Konstrukce: Panel")
                    self.human.random_delay(0.5, 1)
            except Exception as e:
                logger.warning(f"  [WARN] Konstrukce: Panel - {e}")
            
            # 6. VLASTNICTVÍ: Osobní (checkbox)
            try:
                osobni = self.page.locator('label:has-text("Osobní"), span:has-text("Osobní")').first
                if osobni.is_visible(timeout=3000):
                    osobni.click(force=True)
                    logger.info("  [OK] Vlastnictví: Osobní")
                    self.human.random_delay(0.5, 1)
            except Exception as e:
                logger.warning(f"  [WARN] Vlastnictví: Osobní - {e}")
            
            # 7. V OKOLÍ: Set dropdown using correct selector
            try:
                # V okolí dropdown has a specific button ID
                okoli_btn = self.page.locator('#downshift-2-toggle-button').first
                if okoli_btn.is_visible(timeout=2000):
                    okoli_btn.click()
                    self.human.random_delay(0.5, 1)
                    
                    # Get target value - default to "Nezadáno" (not specified)
                    v_okoli_value = filters.get("v_okoli", "Nezadáno")
                    
                    # Look for the option in dropdown
                    option = self.page.locator(f'#downshift-2-menu [role="option"]:has-text("{v_okoli_value}"), #downshift-2-menu li:has-text("{v_okoli_value}")').first
                    if option.is_visible(timeout=2000):
                        option.click()
                        logger.info(f"  [OK] V okolí: {v_okoli_value}")
                        self.human.random_delay(0.5, 1)
                    else:
                        # Click elsewhere to close dropdown
                        self.page.mouse.click(100, 100)
                        logger.warning(f"  [WARN] V okolí option '{v_okoli_value}' not found")
                else:
                    logger.warning("  [WARN] V okolí button not visible")
            except Exception as e:
                logger.warning(f"  [WARN] V okolí - {e}")
            
            # Wait for filters to apply
            self.human.random_delay(2, 3)
            
            # Take screenshot after setting filters
            self.page.screenshot(path="debug_after_filters.png")
            
            logger.info("Base filters set - screenshots saved for verification")
            
        except Exception as e:
            logger.error(f"Error setting filters: {e}")
            logger.error(traceback.format_exc())
    
    def _select_filter(self, label: str, value: str):
        """Select a filter value."""
        try:
            selectors = [
                f'label:has-text("{label}")',
                f'div:has-text("{label}")',
                f'span:has-text("{label}")'
            ]
            
            for selector in selectors:
                try:
                    elem = self.page.locator(selector).first
                    if elem.is_visible(timeout=2000):
                        parent = elem.locator('xpath=..').first
                        
                        sel = parent.locator('select').first
                        if sel.is_visible(timeout=1000):
                            self.human.random_mouse_movement()
                            sel.select_option(label=value)
                            logger.debug(f"Selected {value} for {label}")
                            return
                        
                        self.human.micro_pause()
                        parent.click()
                        self.human.micro_pause()
                        
                        opt = self.page.locator(f'li:has-text("{value}"), div:has-text("{value}")').first
                        if opt.is_visible(timeout=2000):
                            self.human.random_mouse_movement()
                            opt.click()
                            return
                except:
                    continue
            
            # Direct click fallback
            try:
                elem = self.page.locator(f'text="{value}"').first
                if elem.is_visible(timeout=2000):
                    elem.click()
            except:
                pass
                
        except Exception as e:
            logger.warning(f"Could not set {label}: {e}")
    
    def _set_range_filter(self, label: str, min_val: int, max_val: int):
        """Set range filter."""
        try:
            label_elem = self.page.locator(f'text="{label}"').first
            parent = label_elem.locator('xpath=ancestor::div[contains(@class, "filter")]').first
            
            min_inp = parent.locator('input[placeholder*="od"], input[name*="min"]').first
            if min_inp.is_visible(timeout=1000):
                self.human.random_mouse_movement()
                self.human.human_type(min_inp, str(min_val))
            
            self.human.micro_pause()
            
            max_inp = parent.locator('input[placeholder*="do"], input[name*="max"]').first
            if max_inp.is_visible(timeout=1000):
                self.human.random_mouse_movement()
                self.human.human_type(max_inp, str(max_val))
                
        except Exception as e:
            logger.warning(f"Error setting range {label}: {e}")
    
    def wait_for_data_load(self, timeout: int = 5):
        """Wait for data cards to update after filter change."""
        try:
            # Wait for the price card to be visible
            self.page.locator('div.sds-surface.sds-surface--03:has(p:has-text("Průměrná cena"))').wait_for(timeout=timeout * 1000)
            # Additional wait for data to stabilize
            self.human.random_delay(1, 1.5)
            return True
        except:
            return False

    def switch_kategorie(self, kategorie: str) -> bool:
        """Switch between Prodej and Pronájem category.
        
        Args:
            kategorie: Either 'Prodej' or 'Pronájem'
        """
        logger.info(f"Switching to kategorie: {kategorie}")
        try:
            selector = self.page.locator(f'label:has-text("{kategorie}"), span:has-text("{kategorie}")').first
            if selector.is_visible(timeout=3000):
                selector.click(force=True)
                self.human.random_delay(1.5, 2.5)  # Wait for data to reload
                self.wait_for_data_load(timeout=10)
                logger.info(f"  [OK] Switched to: {kategorie}")
                return True
            else:
                logger.warning(f"  [WARN] Kategorie selector not visible: {kategorie}")
                return False
        except Exception as e:
            logger.warning(f"  [WARN] Could not switch kategorie to {kategorie}: {e}")
            return False

    def set_city(self, city: str) -> bool:
        """Set city filter on sreality prices page."""
        logger.info(f"Setting city: {city}")
        
        try:
            # First, close any open date picker
            try:
                picker = self.page.locator('.ob-c-date-range-input__dropdown').first
                if picker.is_visible(timeout=500):
                    logger.info("Closing existing date picker before city change...")
                    self.page.mouse.click(100, 100)
                    self.human.random_delay(0.5, 1)
            except:
                pass
            
            # Exact selector from page HTML
            location_input = self.page.locator('#downshift-0-input')
            
            if not location_input.is_visible(timeout=5000):
                logger.error("Location input #downshift-0-input not visible")
                self.page.screenshot(path="debug_city_page.png")
                return False
            
            # Check if city is already set
            current_value = location_input.input_value()
            if city.lower() in current_value.lower():
                logger.debug(f"City already set to: {current_value}")
                return True
            
            logger.info(f"Found location input, current value: {current_value}")
            
            # Click to focus
            location_input.click(force=True)
            self.human.random_delay(0.3, 0.5)
            
            # Clear existing text
            location_input.fill("")
            self.human.micro_pause()
            
            # Type city name
            location_input.type(city, delay=random.randint(50, 100))
            logger.info(f"Typed city: {city}")
            
            # Wait for autocomplete dropdown
            self.human.random_delay(1.5, 2.5)
            
            # Click on first suggestion from the dropdown
            suggestion_selectors = [
                f'#downshift-1-menu li[role="option"]:has-text("{city}")',
                f'li[role="option"]:has-text("{city}")',
                f'[role="listbox"] li:has-text("{city}")',
                f'ul[role="listbox"] li:first-child',
            ]
            
            suggestion_clicked = False
            for sel in suggestion_selectors:
                try:
                    sugg = self.page.locator(sel).first
                    if sugg.is_visible(timeout=2000):
                        self.human.micro_pause()
                        sugg.click()
                        logger.info(f"Clicked suggestion: {city}")
                        suggestion_clicked = True
                        break
                except:
                    continue
            
            if not suggestion_clicked:
                logger.warning("No suggestion found, pressing Enter")
                location_input.press('Enter')
            
            # IMPORTANT: Wait longer for data to reload after city change
            logger.info("Waiting for page to update after city change...")
            self.human.random_delay(3, 5)  # Increased wait time
            self.wait_for_data_load(timeout=10)
            
            return True
            
        except Exception as e:
            logger.error(f"Error setting city {city}: {e}")
            logger.error(traceback.format_exc())
            return False
    
    def _get_month_names(self) -> Dict[int, str]:
        """Return Czech month names."""
        return {
            1: "Leden", 2: "Únor", 3: "Březen", 4: "Duben",
            5: "Květen", 6: "Červen", 7: "Červenec", 8: "Srpen",
            9: "Září", 10: "Říjen", 11: "Listopad", 12: "Prosinec"
        }
    
    def _close_date_picker(self):
        """Close any open date picker by clicking outside."""
        try:
            picker = self.page.locator('.ob-c-date-range-input__dropdown').first
            if picker.is_visible(timeout=500):
                logger.debug("Closing existing date picker...")
                self.page.mouse.click(100, 100)
                self.human.random_delay(0.8, 1.2)
        except:
            pass
    
    def _navigate_to_year(self, year: int) -> bool:
        """Navigate the date picker to the specified year."""
        max_year_clicks = 15
        
        for _ in range(max_year_clicks):
            try:
                year_element = self.page.locator('.ob-c-date-range-input__caption div').first
                if not year_element.is_visible(timeout=2000):
                    return False
                
                current_year_text = year_element.inner_text().strip()
                current_year = int(current_year_text)
                logger.debug(f"Current year in picker: {current_year}")
                
                if current_year == year:
                    return True
                elif current_year > year:
                    prev_arrow = self.page.locator('.ob-c-date-range-input__nav-button--prev').first
                    if prev_arrow.is_visible(timeout=1000):
                        prev_arrow.click()
                        self.human.random_delay(0.3, 0.5)
                    else:
                        return False
                else:
                    next_arrow = self.page.locator('.ob-c-date-range-input__nav-button:not(.ob-c-date-range-input__nav-button--prev)').first
                    if next_arrow.is_visible(timeout=1000):
                        next_arrow.click()
                        self.human.random_delay(0.3, 0.5)
                    else:
                        return False
            except Exception as e:
                logger.warning(f"Year navigation issue: {e}")
                return False
        return False
    
    def _click_month_cell(self, year: int, month: int, triple_click: bool = True) -> bool:
        """Click on a month cell in the date picker."""
        month_names = self._get_month_names()
        month_name = month_names.get(month, "")
        
        self.human.random_delay(0.3, 0.5)
        aria_label = f"{month_name} {year}"
        month_cell = self.page.locator(f'.ob-c-date-range-input__cell[aria-label="{aria_label}"]').first
        
        if not month_cell.is_visible(timeout=2000):
            # Fallback: try by text
            month_cell = self.page.locator(f'.ob-c-date-range-input__cell:has-text("{month_name}")').first
        
        if month_cell.is_visible(timeout=2000):
            box = month_cell.bounding_box()
            if box:
                click_x = box['x'] + box['width'] / 2
                click_y = box['y'] + box['height'] / 2
                
                click_count = 3 if triple_click else 1
                for i in range(click_count):
                    self.page.mouse.click(click_x, click_y)
                    logger.debug(f"Click {i+1}/{click_count}: {month_name} {year}")
                    self.human.random_delay(0.4, 0.6)
                
                logger.info(f"Clicked month: {month_name} {year} ({click_count}x)")
                return True
            else:
                logger.warning(f"Could not get bounding box for month cell")
                return False
        else:
            logger.warning(f"Month cell '{month_name}' not found")
            return False
    
    def _click_potvrdit(self) -> bool:
        """Click the Potvrdit (confirm) button."""
        potvrdit = self.page.locator('.ob-c-date-range-input__submit-button').first
        if potvrdit.is_visible(timeout=2000):
            potvrdit.click()
            logger.info("Clicked Potvrdit")
            self.human.random_delay(0.8, 1.2)
            return True
        else:
            logger.warning("Potvrdit button not found")
            return False
    
    def _verify_dates(self, year: int, month: int) -> Tuple[bool, bool, bool]:
        """
        Verify that both FROM and TO dates match expected value.
        Returns: (both_match, from_matches, to_matches)
        """
        expected = f"{month:02d}. {year}"
        try:
            from_val = self.page.locator('#default_from').first.input_value()
            to_val = self.page.locator('#default_to').first.input_value()
            logger.info(f"Date inputs - FROM: {from_val}, TO: {to_val} (expected: {expected})")
            
            from_matches = (from_val == expected)
            to_matches = (to_val == expected)
            both_match = from_matches and to_matches
            
            return (both_match, from_matches, to_matches)
        except Exception as e:
            logger.debug(f"Could not verify dates: {e}")
            return (False, False, False)
    
    def _fix_single_date(self, which: str, year: int, month: int) -> bool:
        """
        Fix a single date (FROM or TO) by clicking it directly and selecting the correct date.
        which: 'from' or 'to'
        """
        logger.info(f"Attempting to fix {which.upper()} date directly...")
        
        try:
            # Click the appropriate input
            input_id = '#default_from' if which == 'from' else '#default_to'
            date_input = self.page.locator(input_id).first
            
            if not date_input.is_visible(timeout=2000):
                logger.error(f"{which.upper()} date input not visible")
                return False
            
            date_input.click()
            self.human.random_delay(1, 1.5)
            
            # Verify picker opened
            picker = self.page.locator('.ob-c-date-range-input__dropdown').first
            if not picker.is_visible(timeout=3000):
                logger.error("Date picker did not open for single date fix")
                return False
            
            # Navigate to correct year
            if not self._navigate_to_year(year):
                logger.error(f"Could not navigate to year {year}")
                self._close_date_picker()
                return False
            
            # Click the month (single click for direct selection)
            if not self._click_month_cell(year, month, triple_click=False):
                self._close_date_picker()
                return False
            
            # Click Potvrdit
            if not self._click_potvrdit():
                self._close_date_picker()
                return False
            
            return True
            
        except Exception as e:
            logger.error(f"Error fixing single date: {e}")
            self._close_date_picker()
            return False
    
    def set_date(self, year: int, month: int) -> bool:
        """
        Set date filter using the date picker popup with Potvrdit button.
        
        Strategy:
        1. Try triple-click strategy + Potvrdit
        2. If date mismatch, retry triple-click strategy once more
        3. If still mismatch, fix individual mismatched dates directly
        4. If all fails, return False to skip this record
        """
        logger.info(f"Setting date to: {month:02d}/{year}")
        
        # ==================== STRATEGY 1: Triple-click ====================
        for attempt in range(2):  # Try triple-click twice
            attempt_label = "first" if attempt == 0 else "second (retry)"
            logger.info(f"Triple-click strategy - {attempt_label} attempt")
            
            try:
                self._close_date_picker()
                
                # Click on FROM date input to open picker
                from_input = self.page.locator('#default_from').first
                if not from_input.is_visible(timeout=3000):
                    logger.error("FROM date input not visible")
                    continue
                
                from_input.click()
                self.human.random_delay(1, 1.5)
                
                # Verify picker opened
                picker = self.page.locator('.ob-c-date-range-input__dropdown').first
                if not picker.is_visible(timeout=3000):
                    logger.error("Date picker did not open")
                    continue
                
                logger.info("Date picker opened")
                
                # Navigate to correct year
                if not self._navigate_to_year(year):
                    logger.error(f"Could not navigate to year {year}")
                    self._close_date_picker()
                    continue
                
                # Triple-click the month
                if not self._click_month_cell(year, month, triple_click=True):
                    self._close_date_picker()
                    continue
                
                # Click Potvrdit
                if not self._click_potvrdit():
                    continue
                
                # Verify dates
                both_match, from_ok, to_ok = self._verify_dates(year, month)
                
                if both_match:
                    logger.info("✓ Dates verified successfully!")
                    self.human.random_delay(2, 3)
                    self.wait_for_data_load(timeout=10)
                    return True
                else:
                    logger.warning(f"Date mismatch after {attempt_label} triple-click attempt")
                    
            except Exception as e:
                logger.error(f"Error in triple-click attempt: {e}")
                self._close_date_picker()
        
        # ==================== STRATEGY 2: Fix individual dates ====================
        logger.info("Triple-click failed twice, trying to fix individual dates...")
        
        # Check which dates are wrong
        both_match, from_ok, to_ok = self._verify_dates(year, month)
        
        if both_match:
            # Dates are now correct (maybe delayed update)
            logger.info("✓ Dates are now correct!")
            self.human.random_delay(2, 3)
            self.wait_for_data_load(timeout=10)
            return True
        
        # Fix FROM if needed
        if not from_ok:
            logger.info("FROM date is incorrect, fixing...")
            if self._fix_single_date('from', year, month):
                # Recheck
                both_match, from_ok, to_ok = self._verify_dates(year, month)
                if both_match:
                    logger.info("✓ Dates verified after FROM fix!")
                    self.human.random_delay(2, 3)
                    self.wait_for_data_load(timeout=10)
                    return True
            else:
                logger.error("Failed to fix FROM date")
        
        # Fix TO if needed  
        if not to_ok:
            logger.info("TO date is incorrect, fixing...")
            if self._fix_single_date('to', year, month):
                # Final check
                both_match, from_ok, to_ok = self._verify_dates(year, month)
                if both_match:
                    logger.info("✓ Dates verified after TO fix!")
                    self.human.random_delay(2, 3)
                    self.wait_for_data_load(timeout=10)
                    return True
            else:
                logger.error("Failed to fix TO date")
        
        # ==================== ALL STRATEGIES FAILED ====================
        logger.error(f"❌ All date setting strategies failed for {month:02d}/{year} - SKIPPING this record")
        self._close_date_picker()
        return False
    
    def extract_data(self) -> Dict[str, Any]:
        """Extract data from current page using exact sreality.cz selectors."""
        data = {
            'prumerna_cena': None,
            'prumerna_doba_inzerce': None,
            'aktivnich_nabidek': None,
            'pocet_novych_nabidek': None,
            'prumerny_pocet_zobrazeni': None,
            'no_results': False  # Flag for no results
        }
        
        try:
            # Wait for data to load
            self.human.random_delay(1, 2)
            
            # Check for "no results" state FIRST
            # The page shows: "Nenašli jsme žádný výsledek" with empty.svg image
            try:
                no_results_indicators = [
                    'text="Nenašli jsme žádný výsledek"',
                    'img[src*="empty.svg"]',
                    'text="Zkuste změnit nastavení filtrů"',
                ]
                
                for selector in no_results_indicators:
                    try:
                        elem = self.page.locator(selector).first
                        if elem.is_visible(timeout=1000):
                            logger.warning("No results found for this filter combination")
                            data['no_results'] = True
                            return data
                    except:
                        continue
            except:
                pass
            
            # Based on actual HTML structure:
            # <div class="sds-surface sds-surface--03 NmV3jt-jzzv6UaNM0JBL7">
            #   <p>Průměrná cena</p>
            #   <div class="hwyp8tPYJjCNyzzohiGRk">
            #     <b class="_2u6ctjg8TGB7DZ5etle_xk">18 288 </b>
            #     <span>Kč/m2</span>
            #   </div>
            # </div>
            
            # Find all data cards
            cards = self.page.locator('div.sds-surface.sds-surface--03').all()
            logger.debug(f"Found {len(cards)} data cards")
            
            for card in cards:
                try:
                    # Get the label from <p> element
                    label_elem = card.locator('p').first
                    if not label_elem.is_visible(timeout=500):
                        continue
                    label = label_elem.inner_text().strip()
                    
                    # Get the value from <b> element
                    value_elem = card.locator('b').first
                    if not value_elem.is_visible(timeout=500):
                        continue
                    value_text = value_elem.inner_text().strip()
                    
                    # Clean the value - remove spaces, nbsp, etc.
                    value_clean = value_text.replace('\xa0', '').replace(' ', '').replace('\u00a0', '')
                    
                    try:
                        value = float(value_clean)
                    except:
                        logger.debug(f"Could not parse value: {value_text}")
                        continue
                    
                    # Match to our fields
                    if 'Průměrná cena' in label:
                        data['prumerna_cena'] = value
                        logger.info(f"Extracted Průměrná cena: {value}")
                    elif 'Průměrná doba inzerce' in label:
                        data['prumerna_doba_inzerce'] = value
                        logger.info(f"Extracted Průměrná doba inzerce: {value}")
                    elif 'Aktivních nabídek' in label:
                        data['aktivnich_nabidek'] = value
                        logger.info(f"Extracted Aktivních nabídek: {value}")
                    elif 'Počet nových nabídek' in label:
                        data['pocet_novych_nabidek'] = value
                        logger.info(f"Extracted Počet nových nabídek: {value}")
                    elif 'Průměrný počet zobrazení' in label:
                        data['prumerny_pocet_zobrazeni'] = value
                        logger.info(f"Extracted Průměrný počet zobrazení: {value}")
                        
                except Exception as e:
                    logger.debug(f"Error processing card: {e}")
                    continue
            
            # Fallback: try alternative selectors if main method failed
            if not all(data.values()):
                logger.debug("Trying fallback extraction method")
                
                # Try finding by text content
                metrics = [
                    ('Průměrná cena', 'prumerna_cena'),
                    ('Průměrná doba inzerce', 'prumerna_doba_inzerce'),
                    ('Aktivních nabídek', 'aktivnich_nabidek'),
                    ('Počet nových nabídek', 'pocet_novych_nabidek'),
                    ('Průměrný počet zobrazení', 'prumerny_pocet_zobrazeni'),
                ]
                
                for label_text, field_name in metrics:
                    if data[field_name] is not None:
                        continue
                    
                    try:
                        # Find the label, then go to parent and find <b>
                        label_elem = self.page.locator(f'p:has-text("{label_text}")').first
                        if label_elem.is_visible(timeout=1000):
                            # Get the parent div
                            parent = label_elem.locator('xpath=..').first
                            if parent.is_visible(timeout=500):
                                # Find <b> in parent
                                value_elem = parent.locator('b').first
                                if value_elem.is_visible(timeout=500):
                                    value_text = value_elem.inner_text()
                                    value_clean = value_text.replace('\xa0', '').replace(' ', '').replace('\u00a0', '').strip()
                                    try:
                                        data[field_name] = float(value_clean)
                                        logger.info(f"Fallback extracted {label_text}: {data[field_name]}")
                                    except:
                                        pass
                    except:
                        continue
            
            logger.info(f"Extracted data: cena={data['prumerna_cena']}, doba={data['prumerna_doba_inzerce']}, nabidek={data['aktivnich_nabidek']}")
            return data
            
        except Exception as e:
            logger.error(f"Error extracting data: {e}")
            logger.error(traceback.format_exc())
            return data
    
    def _extract_number(self, text: str) -> Optional[float]:
        if not text:
            return None
        import re
        clean = re.sub(r'[^\d,.]', '', text.replace(' ', ''))
        clean = clean.replace(',', '.')
        try:
            return float(clean) if clean else None
        except:
            return None
    
    def scrape_all(self):
        """Main scraping loop with randomized order."""
        cities = self.config['cities']
        start_year = self.config['date_range']['start_year']
        end_year = self.config['date_range']['end_year']
        
        # Get randomized order
        strategy = ScrapingStrategy(cities, start_year, end_year)
        scraping_order = strategy.generate_scraping_order()
        
        # Filter completed
        remaining = [
            t for t in scraping_order
            if (t[0], str(t[1]), str(t[2])) not in self.completed_tasks
        ]
        
        total = len(scraping_order)
        done = total - len(remaining)
        
        logger.info(f"Tasks: {total} total, {done} done, {len(remaining)} remaining")
        
        current_city = None
        
        for i, (city, year, month) in enumerate(remaining):
            task_num = done + i + 1
            date_key = f"{year}-{month:02d}"
            
            logger.info(f"[{task_num}/{total}] {city} - {date_key}")
            
            # Check for breaks
            if self.human.should_take_break():
                self.page.reload()
                self.human.random_delay(2, 4)
                current_city = None
            
            # Set city if changed
            if city != current_city:
                if not self.set_city(city):
                    self.errors.append({'city': city, 'date': date_key, 'error': 'Location not found'})
                    continue
                current_city = city
                self.human.random_delay(1, 2)
            
            self.human.do_random_action()
            
            # Get kategorie selection from config
            kategorie_sel = self.config.get('filters', {}).get('kategorie_selection', {'prodej': True, 'pronajem': True})
            if isinstance(kategorie_sel, str):
                # Legacy config - default to both
                kategorie_sel = {'prodej': True, 'pronajem': True}
            scrape_prodej = kategorie_sel.get('prodej', True)
            scrape_pronajem = kategorie_sel.get('pronajem', True)
            
            # Set date and extract for selected categories
            retry = 0
            max_retry = self.config['scraping']['retry_attempts']
            
            while retry < max_retry:
                try:
                    if not self.set_date(year, month):
                        break
                    
                    self.human.random_delay(
                        self.config['scraping']['min_delay_seconds'],
                        self.config['scraping']['max_delay_seconds']
                    )
                    
                    # --- PRODEJ ---
                    if scrape_prodej:
                        # Make sure we're on Prodej
                        if scrape_pronajem:
                            # If we're scraping both, ensure we start with Prodej
                            pass  # Already default state
                        
                        logger.info(f"  Extracting PRODEJ data...")
                        data_prodej = self.extract_data()
                        
                        # Check for "no results" condition
                        if data_prodej.get('no_results'):
                            logger.warning(f"  No results for {city} {date_key} (Prodej) - filter combination has no data")
                            # Store None values but don't count as error
                            if city not in self.data_prumerna_cena:
                                self.data_prumerna_cena[city] = {}
                            self.data_prumerna_cena[city][date_key] = None
                            # Continue to next task without storing error
                        else:
                            # Store Prodej data
                            if city not in self.data_prumerna_cena:
                                self.data_prumerna_cena[city] = {}
                            if city not in self.data_prumerna_doba:
                                self.data_prumerna_doba[city] = {}
                            if city not in self.data_aktivnich_nabidek:
                                self.data_aktivnich_nabidek[city] = {}
                            if city not in self.data_pocet_novych:
                                self.data_pocet_novych[city] = {}
                            if city not in self.data_prumerny_zobrazeni:
                                self.data_prumerny_zobrazeni[city] = {}
                            
                            self.data_prumerna_cena[city][date_key] = data_prodej['prumerna_cena']
                            self.data_prumerna_doba[city][date_key] = data_prodej['prumerna_doba_inzerce']
                            self.data_aktivnich_nabidek[city][date_key] = data_prodej['aktivnich_nabidek']
                            self.data_pocet_novych[city][date_key] = data_prodej['pocet_novych_nabidek']
                            self.data_prumerny_zobrazeni[city][date_key] = data_prodej['prumerny_pocet_zobrazeni']
                            
                            # Only log as error if we expected data but got none (not due to no_results)
                            if all(v is None for k, v in data_prodej.items() if k != 'no_results'):
                                self.errors.append({'city': city, 'date': date_key, 'error': 'No data extracted (Prodej)'})
                    
                    # --- PRONÁJEM ---
                    if scrape_pronajem:
                        # Switch to Pronájem if needed
                        if scrape_prodej:
                            logger.info(f"  Switching to PRONÁJEM...")
                            if not self.switch_kategorie('Pronájem'):
                                self.errors.append({'city': city, 'date': date_key, 'error': 'Could not switch to Pronájem'})
                                break
                        else:
                            # Only scraping Pronájem - switch from default Prodej
                            logger.info(f"  Switching to PRONÁJEM...")
                            if not self.switch_kategorie('Pronájem'):
                                self.errors.append({'city': city, 'date': date_key, 'error': 'Could not switch to Pronájem'})
                                break
                        
                        logger.info(f"  Extracting PRONÁJEM data...")
                        data_pronajem = self.extract_data()
                        
                        # Check for "no results" condition
                        if data_pronajem.get('no_results'):
                            logger.warning(f"  No results for {city} {date_key} (Pronájem) - filter combination has no data")
                            # Store None values but don't count as error
                            if city not in self.data_prumerna_cena_pronajem:
                                self.data_prumerna_cena_pronajem[city] = {}
                            self.data_prumerna_cena_pronajem[city][date_key] = None
                            # Continue without storing error
                        else:
                            # Store Pronájem data
                            if city not in self.data_prumerna_cena_pronajem:
                                self.data_prumerna_cena_pronajem[city] = {}
                            if city not in self.data_prumerna_doba_pronajem:
                                self.data_prumerna_doba_pronajem[city] = {}
                            if city not in self.data_aktivnich_nabidek_pronajem:
                                self.data_aktivnich_nabidek_pronajem[city] = {}
                            if city not in self.data_pocet_novych_pronajem:
                                self.data_pocet_novych_pronajem[city] = {}
                            if city not in self.data_prumerny_zobrazeni_pronajem:
                                self.data_prumerny_zobrazeni_pronajem[city] = {}
                            
                            self.data_prumerna_cena_pronajem[city][date_key] = data_pronajem['prumerna_cena']
                            self.data_prumerna_doba_pronajem[city][date_key] = data_pronajem['prumerna_doba_inzerce']
                            self.data_aktivnich_nabidek_pronajem[city][date_key] = data_pronajem['aktivnich_nabidek']
                            self.data_pocet_novych_pronajem[city][date_key] = data_pronajem['pocet_novych_nabidek']
                            self.data_prumerny_zobrazeni_pronajem[city][date_key] = data_pronajem['prumerny_pocet_zobrazeni']
                            
                            # Only log as error if we expected data but got none (not due to no_results)
                            if all(v is None for k, v in data_pronajem.items() if k != 'no_results'):
                                self.errors.append({'city': city, 'date': date_key, 'error': 'No data extracted (Pronájem)'})
                        
                        # Switch back to Prodej for next iteration (if we scraped both)
                        if scrape_prodej:
                            logger.info(f"  Switching back to PRODEJ...")
                            self.switch_kategorie('Prodej')
                    
                    self.completed_tasks.add((city, str(year), str(month)))
                    
                    break
                    
                except PlaywrightTimeout:
                    retry += 1
                    if retry < max_retry:
                        self.human.random_delay(
                            self.config['scraping']['retry_delay_seconds'],
                            self.config['scraping']['retry_delay_seconds'] * 2
                        )
                    else:
                        self.errors.append({'city': city, 'date': date_key, 'error': 'Timeout'})
                        
                except Exception as e:
                    retry += 1
                    if retry >= max_retry:
                        self.errors.append({'city': city, 'date': date_key, 'error': str(e)})
            
            # Save progress randomly (~8% of time)
            if random.random() < 0.08:
                self.save_progress()
                self._save_csv_progress()
        
        logger.info(f"\nScraping complete. Errors: {len(self.errors)}")
    
    def _save_csv_progress(self):
        """Save current CSVs for selected categories."""
        out_dir = self.config['output']['directory']
        prefix = self.config['output']['filename_prefix']
        
        # Get kategorie selection
        kategorie_sel = self.config.get('filters', {}).get('kategorie_selection', {'prodej': True, 'pronajem': True})
        if isinstance(kategorie_sel, str):
            kategorie_sel = {'prodej': True, 'pronajem': True}
        
        # Prodej files
        if kategorie_sel.get('prodej', True):
            self._save_csv(self.data_prumerna_cena, f"{out_dir}/{prefix}_prumerna_cena_prodej.csv", 'Prodej')
            self._save_csv(self.data_prumerna_doba, f"{out_dir}/{prefix}_prumerna_doba_inzerce_prodej.csv", 'Prodej')
            self._save_csv(self.data_aktivnich_nabidek, f"{out_dir}/{prefix}_aktivnich_nabidek_prodej.csv", 'Prodej')
            self._save_csv(self.data_pocet_novych, f"{out_dir}/{prefix}_pocet_novych_nabidek_prodej.csv", 'Prodej')
            self._save_csv(self.data_prumerny_zobrazeni, f"{out_dir}/{prefix}_prumerny_pocet_zobrazeni_prodej.csv", 'Prodej')
        
        # Pronájem files
        if kategorie_sel.get('pronajem', True):
            self._save_csv(self.data_prumerna_cena_pronajem, f"{out_dir}/{prefix}_prumerna_cena_pronajem.csv", 'Pronájem')
            self._save_csv(self.data_prumerna_doba_pronajem, f"{out_dir}/{prefix}_prumerna_doba_inzerce_pronajem.csv", 'Pronájem')
            self._save_csv(self.data_aktivnich_nabidek_pronajem, f"{out_dir}/{prefix}_aktivnich_nabidek_pronajem.csv", 'Pronájem')
            self._save_csv(self.data_pocet_novych_pronajem, f"{out_dir}/{prefix}_pocet_novych_nabidek_pronajem.csv", 'Pronájem')
            self._save_csv(self.data_prumerny_zobrazeni_pronajem, f"{out_dir}/{prefix}_prumerny_pocet_zobrazeni_pronajem.csv", 'Pronájem')
    
    def _save_csv(self, data: Dict, filepath: str, kategorie: str = 'Prodej'):
        """Save data dict to CSV with filter columns."""
        if not data:
            return
        
        # Get filter settings from config
        filters = self.config.get('filters', {})
        typ = filters.get('typ', 'Byty')
        stav_objektu = filters.get('stav_objektu', 'Velmi dobrý')
        konstrukce = filters.get('konstrukce', 'Panel')
        vlastnictvi = filters.get('vlastnictvi', 'Osobní')
        uzitna_plocha_od = filters.get('uzitna_plocha_od', 30)
        uzitna_plocha_do = filters.get('uzitna_plocha_do', 80)
        v_okoli = 'nezadáno'  # This is typically not set
        
        # Collect all dates
        all_dates = set()
        for city_data in data.values():
            all_dates.update(city_data.keys())
        
        sorted_dates = sorted(all_dates)
        
        # Build rows with filter columns first, then date columns
        rows = []
        for city in sorted(data.keys()):
            row = {
                'City': city,
                'Kategorie': kategorie,
                'Typ': typ,
                'Stav objektu': stav_objektu,
                'Konstrukce': konstrukce,
                'Vlastnictví': vlastnictvi,
                'Užitná plocha od': uzitna_plocha_od,
                'Užitná plocha do': uzitna_plocha_do,
                'V okolí': v_okoli,
            }
            # Add date values
            city_data = data.get(city, {})
            for date in sorted_dates:
                row[date] = city_data.get(date, '')
            rows.append(row)
        
        # Define column order: filter columns first, then dates
        filter_cols = ['City', 'Kategorie', 'Typ', 'Stav objektu', 'Konstrukce', 
                       'Vlastnictví', 'Užitná plocha od', 'Užitná plocha do', 'V okolí']
        all_cols = filter_cols + sorted_dates
        
        # Write CSV
        with open(filepath, 'w', newline='', encoding='utf-8-sig') as f:
            writer = csv.DictWriter(f, fieldnames=all_cols)
            writer.writeheader()
            writer.writerows(rows)
        
        logger.debug(f"Saved: {filepath}")
    
    def export_final(self):
        """Export final results for selected categories."""
        out_dir = self.config['output']['directory']
        prefix = self.config['output']['filename_prefix']
        ts = datetime.now().strftime('%Y%m%d_%H%M%S')
        
        # Get kategorie selection
        kategorie_sel = self.config.get('filters', {}).get('kategorie_selection', {'prodej': True, 'pronajem': True})
        if isinstance(kategorie_sel, str):
            kategorie_sel = {'prodej': True, 'pronajem': True}
        
        # Prodej files
        if kategorie_sel.get('prodej', True):
            self._save_csv(self.data_prumerna_cena, f"{out_dir}/{prefix}_prumerna_cena_prodej_{ts}.csv", 'Prodej')
            self._save_csv(self.data_prumerna_doba, f"{out_dir}/{prefix}_prumerna_doba_inzerce_prodej_{ts}.csv", 'Prodej')
            self._save_csv(self.data_aktivnich_nabidek, f"{out_dir}/{prefix}_aktivnich_nabidek_prodej_{ts}.csv", 'Prodej')
            self._save_csv(self.data_pocet_novych, f"{out_dir}/{prefix}_pocet_novych_nabidek_prodej_{ts}.csv", 'Prodej')
            self._save_csv(self.data_prumerny_zobrazeni, f"{out_dir}/{prefix}_prumerny_pocet_zobrazeni_prodej_{ts}.csv", 'Prodej')
        
        # Pronájem files
        if kategorie_sel.get('pronajem', True):
            self._save_csv(self.data_prumerna_cena_pronajem, f"{out_dir}/{prefix}_prumerna_cena_pronajem_{ts}.csv", 'Pronájem')
            self._save_csv(self.data_prumerna_doba_pronajem, f"{out_dir}/{prefix}_prumerna_doba_inzerce_pronajem_{ts}.csv", 'Pronájem')
            self._save_csv(self.data_aktivnich_nabidek_pronajem, f"{out_dir}/{prefix}_aktivnich_nabidek_pronajem_{ts}.csv", 'Pronájem')
            self._save_csv(self.data_pocet_novych_pronajem, f"{out_dir}/{prefix}_pocet_novych_nabidek_pronajem_{ts}.csv", 'Pronájem')
            self._save_csv(self.data_prumerny_zobrazeni_pronajem, f"{out_dir}/{prefix}_prumerny_pocet_zobrazeni_pronajem_{ts}.csv", 'Pronájem')
        
        if self.errors:
            with open(f"{out_dir}/{prefix}_errors_{ts}.csv", 'w', newline='', encoding='utf-8-sig') as f:
                writer = csv.DictWriter(f, fieldnames=['city', 'date', 'error'])
                writer.writeheader()
                writer.writerows(self.errors)
        
        logger.info(f"\nResults exported to {out_dir}/")
    
    def _cleanup_progress_files(self):
        """Delete progress files after successful completion."""
        out_dir = self.config['output']['directory']
        prefix = self.config['output']['filename_prefix']
        
        # Get kategorie selection
        kategorie_sel = self.config.get('filters', {}).get('kategorie_selection', {'prodej': True, 'pronajem': True})
        if isinstance(kategorie_sel, str):
            kategorie_sel = {'prodej': True, 'pronajem': True}
        
        progress_files = []
        
        # Prodej progress files
        if kategorie_sel.get('prodej', True):
            progress_files.extend([
                f"{out_dir}/{prefix}_prumerna_cena_prodej.csv",
                f"{out_dir}/{prefix}_prumerna_doba_inzerce_prodej.csv",
                f"{out_dir}/{prefix}_aktivnich_nabidek_prodej.csv",
                f"{out_dir}/{prefix}_pocet_novych_nabidek_prodej.csv",
                f"{out_dir}/{prefix}_prumerny_pocet_zobrazeni_prodej.csv",
            ])
        
        # Pronájem progress files
        if kategorie_sel.get('pronajem', True):
            progress_files.extend([
                f"{out_dir}/{prefix}_prumerna_cena_pronajem.csv",
                f"{out_dir}/{prefix}_prumerna_doba_inzerce_pronajem.csv",
                f"{out_dir}/{prefix}_aktivnich_nabidek_pronajem.csv",
                f"{out_dir}/{prefix}_pocet_novych_nabidek_pronajem.csv",
                f"{out_dir}/{prefix}_prumerny_pocet_zobrazeni_pronajem.csv",
            ])
        
        # Also delete progress.json
        progress_files.append(f"{out_dir}/progress.json")
        
        deleted_count = 0
        for filepath in progress_files:
            try:
                if os.path.exists(filepath):
                    os.remove(filepath)
                    deleted_count += 1
                    logger.debug(f"Deleted progress file: {filepath}")
            except Exception as e:
                logger.warning(f"Could not delete {filepath}: {e}")
        
        if deleted_count > 0:
            logger.info(f"Cleaned up {deleted_count} progress files")
    
    def run(self, headless: bool = False):
        """Main entry point."""
        try:
            self.start_browser(headless=headless)
            
            if not self.login():
                logger.error("Login failed.")
                return
            
            self.navigate_to_prices_page()
            self.set_base_filters()
            self.scrape_all()
            self.export_final()
            self._cleanup_progress_files()  # Clean up after successful completion
            
        except KeyboardInterrupt:
            logger.info("\nInterrupted. Saving progress...")
            self.save_progress()
            self._save_csv_progress()
        
        except Exception as e:
            logger.error(f"Fatal: {e}")
            self.save_progress()
            self._save_csv_progress()
            raise
        
        finally:
            self.close_browser()


def main():
    import argparse
    
    parser = argparse.ArgumentParser(description='Sreality.cz Price Scraper v2.0')
    parser.add_argument('--config', '-c', default='config.json', help='Config file path')
    parser.add_argument('--headless', '-H', action='store_true', help='Run in headless mode')
    parser.add_argument('--test', '-t', action='store_true', help='Test mode (2 cities, 1 year)')
    parser.add_argument('--fast', '-f', action='store_true', help='Fast mode (reduced delays, higher detection risk)')
    parser.add_argument('--chunk', help='Chunk specification: "N/M" where N is chunk number (1-based), M is total chunks. E.g., "1/3" for first of 3 chunks')
    parser.add_argument('--output-suffix', help='Suffix to add to output files (used for parallel runs)')
    parser.add_argument('--filter-combo', help='JSON string with filter combination: {"stav_objektu": "...", "konstrukce": "...", "vlastnictvi": "..."}')
    
    args = parser.parse_args()
    
    # Determine worker_id from chunk argument (for synchronized breaks)
    worker_id = 0
    if args.chunk:
        try:
            chunk_num, _ = map(int, args.chunk.split('/'))
            worker_id = chunk_num
        except:
            pass
    
    mode_str = "[FAST MODE]" if args.fast else "[NORMAL MODE - human-like delays]"
    chunk_str = f"[CHUNK {args.chunk}]" if args.chunk else ""
    sync_str = "[SYNC BREAKS]" if worker_id > 0 else ""
    
    print(f"""
============================================================
       Sreality.cz Real Estate Price Scraper v2.0           
                                                            
  {mode_str} {chunk_str} {sync_str}
                                                            
  Human-like behavior patterns:                          
     - Randomized scraping order                            
     - Natural mouse movements & scrolling                  
     - Variable typing speed with typos                     
     - Coffee breaks & thinking pauses                      
     - Multiple scraping strategies                         
============================================================
    """)
    
    scraper = SrealityScraper(config_path=args.config, fast_mode=args.fast, worker_id=worker_id)
    
    # Handle filter combination
    if args.filter_combo:
        try:
            filter_combo = json.loads(args.filter_combo)
            # Override filter values
            scraper.config['filters']['stav_objektu'] = filter_combo.get('stav_objektu', 'Velmi dobrý')
            scraper.config['filters']['konstrukce'] = filter_combo.get('konstrukce', 'Panel')
            scraper.config['filters']['vlastnictvi'] = filter_combo.get('vlastnictvi', 'Osobní')
            
            # Create suffix for output files
            def sanitize(s):
                return s.lower().replace(' ', '').replace('í', 'i').replace('á', 'a').replace('ý', 'y').replace('ě', 'e').replace('ž', 'z').replace('š', 's').replace('č', 'c').replace('ř', 'r').replace('ů', 'u').replace('ú', 'u')
            
            filter_suffix = f"{sanitize(filter_combo.get('stav_objektu', ''))}_{sanitize(filter_combo.get('konstrukce', ''))}_{sanitize(filter_combo.get('vlastnictvi', ''))}"
            
            original_prefix = scraper.config['output']['filename_prefix']
            scraper.config['output']['filename_prefix'] = f"{original_prefix}_{filter_suffix}"
            
            logger.info(f"FILTER COMBO: stav={filter_combo.get('stav_objektu')}, konstrukce={filter_combo.get('konstrukce')}, vlastnictvi={filter_combo.get('vlastnictvi')}")
            
        except json.JSONDecodeError as e:
            logger.error(f"Invalid filter-combo JSON: {e}")
            sys.exit(1)
    
    # Handle chunk splitting
    if args.chunk:
        try:
            chunk_num, total_chunks = map(int, args.chunk.split('/'))
            if chunk_num < 1 or chunk_num > total_chunks:
                raise ValueError("Chunk number must be between 1 and total chunks")
            
            all_cities = scraper.config['cities']
            total_cities = len(all_cities)
            
            # Calculate chunk boundaries
            chunk_size = math.ceil(total_cities / total_chunks)
            start_idx = (chunk_num - 1) * chunk_size
            end_idx = min(chunk_num * chunk_size, total_cities)
            
            scraper.config['cities'] = all_cities[start_idx:end_idx]
            logger.info(f"CHUNK {chunk_num}/{total_chunks}: Processing cities {start_idx+1}-{end_idx} of {total_cities}")
            logger.info(f"Cities in this chunk: {scraper.config['cities']}")
            
            # Modify output prefix to include chunk number
            original_prefix = scraper.config['output']['filename_prefix']
            scraper.config['output']['filename_prefix'] = f"{original_prefix}_chunk{chunk_num}"
            
        except ValueError as e:
            logger.error(f"Invalid chunk specification '{args.chunk}': {e}")
            logger.error("Use format 'N/M' where N is chunk number (1-based), M is total chunks")
            sys.exit(1)
    
    # Handle output suffix (for parallel runs)
    if args.output_suffix:
        original_prefix = scraper.config['output']['filename_prefix']
        scraper.config['output']['filename_prefix'] = f"{original_prefix}_{args.output_suffix}"
    
    if args.test:
        scraper.config['cities'] = scraper.config['cities'][:2]
        scraper.config['date_range']['start_year'] = 2024
        scraper.config['date_range']['end_year'] = 2024
        logger.info("TEST MODE: 2 cities, 1 year")
    
    scraper.run(headless=args.headless)


if __name__ == '__main__':
    main()
