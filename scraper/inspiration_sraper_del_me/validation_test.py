#!/usr/bin/env python3
"""
Sreality Scraper Validation Test
================================
Tests scraping reliability by running twice:
1. Randomized order (standard algorithm)
2. Sequential order (city by city, month by month)

Then compares results to verify consistency.
"""

import json
import time
import random
import logging
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Tuple, Any
from playwright.sync_api import sync_playwright, Page

# Setup logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

# Test configuration
TEST_CITIES = ["Praha", "Brno", "Ostrava"]  # 3 major cities for reliable data
TEST_MONTHS = [(2024, 6), (2024, 7), (2024, 8), (2024, 9), (2024, 10)]  # 5 months

class ValidationTest:
    def __init__(self, config_path: str = "config.json"):
        with open(config_path, 'r', encoding='utf-8') as f:
            self.config = json.load(f)
        
        self.results_random = {}
        self.results_sequential = {}
        self.page = None
        self.browser = None
        
    def start_browser(self, headless: bool = False):
        """Start browser with fresh state."""
        logger.info(f"Starting browser (headless={headless})...")
        self.playwright = sync_playwright().start()
        self.browser = self.playwright.chromium.launch(
            headless=headless,
            args=['--disable-blink-features=AutomationControlled']
        )
        self.context = self.browser.new_context(
            viewport={'width': 1920, 'height': 1080},
            locale='cs-CZ'
        )
        self.page = self.context.new_page()
        # Set default timeout to 2 minutes (120 seconds) instead of 30 minutes
        self.page.set_default_timeout(120000)
        logger.info("Browser started")
        
    def close_browser(self):
        """Close browser."""
        if self.browser:
            self.browser.close()
        if self.playwright:
            self.playwright.stop()
        logger.info("Browser closed")
        
    def login(self):
        """Login to sreality.cz."""
        logger.info("Logging in...")
        self.page.goto("https://www.sreality.cz/")
        time.sleep(3)
        
        # Handle cookie consent
        try:
            consent = self.page.locator('button:has-text("Souhlasím")').first
            if consent.is_visible(timeout=3000):
                consent.click()
                time.sleep(1)
        except:
            pass
        
        # Find login button first
        login_btn = None
        try:
            login_btn = self.page.locator('a:has-text("Přihlásit"), button:has-text("Přihlásit")').first
            if not login_btn.is_visible(timeout=3000):
                logger.warning("Login button not found!")
                login_btn = None
        except:
            pass
        
        # Click login and capture popup in one action
        popup = None
        if login_btn:
            try:
                with self.page.expect_popup(timeout=10000) as popup_info:
                    login_btn.click()
                popup = popup_info.value
                logger.info("Login popup detected")
                popup.wait_for_load_state('domcontentloaded')
                time.sleep(1)
            except Exception as e:
                logger.warning(f"No login popup detected: {e}, trying main page")
                popup = self.page
        else:
            popup = self.page
            
        # Handle consent in popup
        try:
            consent = popup.locator('button:has-text("Souhlasím")').first
            if consent.is_visible(timeout=3000):
                consent.click()
                time.sleep(1)
        except:
            pass
        
        # Enter credentials
        try:
            email_field = popup.locator('#login-username')
            if email_field.is_visible(timeout=5000):
                email_field.fill(self.config['credentials']['email'])
                time.sleep(0.5)
                
                # Click continue
                popup.locator('button[type="submit"]').first.click()
                time.sleep(2)
                
                # Enter password
                pwd_field = popup.locator('#login-password')
                if pwd_field.is_visible(timeout=5000):
                    pwd_field.fill(self.config['credentials']['password'])
                    time.sleep(0.5)
                    
                    # Click login
                    popup.locator('button[type="submit"]').first.click()
                    time.sleep(3)
                else:
                    logger.error("Password field not visible!")
            else:
                logger.error("Email field not visible!")
                    
        except Exception as e:
            logger.error(f"Login error: {e}")
            
        # Wait for popup to close
        try:
            if popup != self.page:
                popup.wait_for_close(timeout=15000)
        except:
            pass
        
        # IMPORTANT: Wait for main page to finish redirecting after login
        logger.info("Waiting for post-login redirect to complete...")
        time.sleep(5)
        
        # Wait for page to be stable
        try:
            self.page.wait_for_load_state("networkidle", timeout=10000)
        except:
            pass
            
        time.sleep(2)
        
        # Verify login by checking we're still on sreality
        current_url = self.page.url
        if "sreality.cz" not in current_url:
            logger.warning(f"After login, unexpected URL: {current_url}")
            # Try to go back to sreality
            self.page.goto("https://www.sreality.cz/")
            time.sleep(3)
            
        logger.info("Login complete")
        
    def navigate_to_prices(self):
        """Navigate to prices page."""
        logger.info("Navigating to prices page...")
        
        # Try multiple times in case of redirect issues
        max_attempts = 5
        for attempt in range(max_attempts):
            try:
                self.page.goto("https://www.sreality.cz/ceny-nemovitosti", wait_until="domcontentloaded", timeout=30000)
                time.sleep(3)
                
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
                                # Wait for redirect
                                time.sleep(4)
                                break
                        except:
                            continue
                    
                    if not clicked:
                        logger.warning("Could not find consent button")
                        # Try pressing Enter as fallback
                        self.page.keyboard.press('Enter')
                        time.sleep(2)
                    
                    continue
                
                # Verify we're on the right page
                if "ceny-nemovitosti" in current_url and "sreality.cz" in current_url:
                    logger.info(f"Successfully navigated to: {current_url}")
                    return True
                else:
                    logger.warning(f"Unexpected URL: {current_url}, retrying...")
                    time.sleep(2)
                    
            except Exception as e:
                logger.warning(f"Navigation attempt {attempt + 1} failed: {e}")
                time.sleep(3)
                
        logger.error("Failed to navigate to prices page after multiple attempts")
        return False
        
    def set_all_filters(self):
        """Set ALL base filters and verify them."""
        logger.info("Setting and verifying all base filters...")
        
        # First verify we're on the right page
        if "ceny-nemovitosti" not in self.page.url or "sreality.cz" not in self.page.url:
            logger.error(f"Not on prices page! Current URL: {self.page.url}")
            # Try to navigate
            if not self.navigate_to_prices():
                logger.error("Could not get to prices page!")
                return False
        
        filters = self.config['filters']
        
        # 1. TYP: Byty (radio button)
        try:
            byty = self.page.locator('label:has-text("Byty"), span:has-text("Byty")').first
            if byty.is_visible(timeout=2000):
                byty.click()
                time.sleep(0.5)
                logger.info("  [OK] Typ: Byty")
        except Exception as e:
            logger.warning(f"  [FAIL] Typ: Byty - {e}")
            
        # 2. KATEGORIE: Prodej (radio button)
        try:
            prodej = self.page.locator('label:has-text("Prodej"), span:has-text("Prodej")').first
            if prodej.is_visible(timeout=2000):
                prodej.click()
                time.sleep(0.5)
                logger.info("  [OK] Kategorie: Prodej")
        except Exception as e:
            logger.warning(f"  [FAIL] Kategorie: Prodej - {e}")
            
        # 3. STAV OBJEKTU: Velmi dobrý (checkbox)
        try:
            stav_label = self.page.locator('text="Velmi dobrý"').first
            if stav_label.is_visible(timeout=2000):
                stav_label.click()
                time.sleep(0.5)
                logger.info("  [OK] Stav objektu: Velmi dobrý")
        except Exception as e:
            logger.warning(f"  [FAIL] Stav objektu: Velmi dobrý - {e}")
            
        # 4. UŽITNÁ PLOCHA: Od/Do
        try:
            area_section = self.page.locator('text="Užitná plocha"').first
            if area_section.is_visible(timeout=2000):
                od_input = self.page.locator('input[name="usable_area_from"], input#usable_area_from').first
                do_input = self.page.locator('input[name="usable_area_to"], input#usable_area_to').first
                
                if od_input.is_visible(timeout=1000):
                    od_input.fill(str(filters.get('uzitna_plocha_od', 30)))
                    time.sleep(0.3)
                    od_input.press('Tab')
                    time.sleep(0.3)
                    logger.info(f"  [OK] Užitná plocha Od: {filters.get('uzitna_plocha_od', 30)}")
                    
                if do_input.is_visible(timeout=1000):
                    do_input.fill(str(filters.get('uzitna_plocha_do', 90)))
                    time.sleep(0.3)
                    do_input.press('Tab')
                    time.sleep(0.3)
                    logger.info(f"  [OK] Užitná plocha Do: {filters.get('uzitna_plocha_do', 90)}")
        except Exception as e:
            logger.warning(f"  [FAIL] Užitná plocha - {e}")
            
        # 5. KONSTRUKCE: Panel (checkbox)
        try:
            panel = self.page.locator('text="Panel"').first
            if panel.is_visible(timeout=2000):
                panel.click()
                time.sleep(0.5)
                logger.info("  [OK] Konstrukce: Panel")
        except Exception as e:
            logger.warning(f"  [FAIL] Konstrukce: Panel - {e}")
            
        # 6. VLASTNICTVÍ: Osobní (checkbox)
        try:
            osobni = self.page.locator('text="Osobní"').first
            if osobni.is_visible(timeout=2000):
                osobni.click()
                time.sleep(0.5)
                logger.info("  [OK] Vlastnictví: Osobní")
        except Exception as e:
            logger.warning(f"  [FAIL] Vlastnictví: Osobní - {e}")
            
        # 7. V OKOLÍ: Always explicitly set to ensure consistency
        try:
            v_okoli_value = filters.get('v_okoli', 'Nezadáno')
            okoli_btn = self.page.locator('#downshift-2-toggle-button').first
            if okoli_btn.is_visible(timeout=2000):
                okoli_btn.click()
                time.sleep(0.5)
                
                # Find and click the option
                option = self.page.locator(f'#downshift-2-menu [role="option"]:has-text("{v_okoli_value}"), #downshift-2-menu li:has-text("{v_okoli_value}")').first
                if option.is_visible(timeout=2000):
                    option.click()
                    time.sleep(0.5)
                    logger.info(f"  [OK] V okolí: {v_okoli_value}")
                else:
                    # Click elsewhere to close dropdown
                    self.page.mouse.click(100, 100)
                    time.sleep(0.3)
                    logger.warning(f"  [WARN] V okolí option '{v_okoli_value}' not found")
            else:
                logger.warning("  [WARN] V okolí button not visible")
        except Exception as e:
            logger.warning(f"  [FAIL] V okolí - {e}")
            
        # Wait for filters to apply
        time.sleep(2)
        logger.info("Base filters set")
        
    def set_city(self, city: str) -> bool:
        """Set city filter."""
        logger.info(f"  Setting city: {city}")
        
        try:
            # First, close any open date picker by clicking elsewhere
            try:
                picker = self.page.locator('.ob-c-date-range-input__dropdown').first
                if picker.is_visible(timeout=500):
                    self.page.mouse.click(100, 100)
                    time.sleep(0.5)
            except:
                pass
            
            location_input = self.page.locator('#downshift-0-input')
            
            if not location_input.is_visible(timeout=5000):
                logger.error(f"    Location input not found!")
                return False
            
            # Check current value
            current = location_input.input_value()
            if city.lower() in current.lower():
                logger.info(f"    City already set: {current}")
                return True
            
            # Clear and type
            location_input.click(force=True, timeout=5000)
            time.sleep(0.3)
            location_input.fill("")
            time.sleep(0.2)
            location_input.type(city, delay=50)
            time.sleep(2)  # Wait for autocomplete
            
            # Click first suggestion
            suggestion = self.page.locator(f'li[role="option"]:has-text("{city}")').first
            if suggestion.is_visible(timeout=3000):
                suggestion.click(force=True, timeout=5000)
                logger.info(f"    Selected: {city}")
            else:
                # Try Enter
                location_input.press('Enter')
                logger.info(f"    Pressed Enter for: {city}")
            
            # IMPORTANT: Wait longer for page to update after city change
            logger.info(f"    Waiting for page to update after city change...")
            time.sleep(4)  # Critical wait!
            
            return True
                
        except Exception as e:
            logger.error(f"    Error setting city: {e}")
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
                logger.debug("    Closing existing date picker...")
                self.page.mouse.click(100, 100)
                time.sleep(1)
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
                logger.debug(f"    Current year in picker: {current_year}")
                
                if current_year == year:
                    return True
                elif current_year > year:
                    prev_arrow = self.page.locator('.ob-c-date-range-input__nav-button--prev').first
                    if prev_arrow.is_visible(timeout=1000):
                        prev_arrow.click(force=True, timeout=3000)
                        time.sleep(0.5)
                    else:
                        return False
                else:
                    next_arrow = self.page.locator('.ob-c-date-range-input__nav-button:not(.ob-c-date-range-input__nav-button--prev)').first
                    if next_arrow.is_visible(timeout=1000):
                        next_arrow.click(force=True, timeout=3000)
                        time.sleep(0.5)
                    else:
                        return False
            except Exception as e:
                logger.warning(f"    Year navigation issue: {e}")
                return False
        return False
    
    def _click_month_cell(self, year: int, month: int, triple_click: bool = True) -> bool:
        """Click on a month cell in the date picker."""
        month_names = self._get_month_names()
        month_name = month_names.get(month, "")
        
        time.sleep(0.5)
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
                    logger.debug(f"    Click {i+1}/{click_count}: {month_name} {year}")
                    time.sleep(0.5)
                
                logger.info(f"    Clicked month: {month_name} {year} ({click_count}x)")
                return True
            else:
                logger.warning(f"    Could not get bounding box for month cell")
                return False
        else:
            logger.warning(f"    Month cell '{month_name}' not found")
            return False
    
    def _click_potvrdit(self) -> bool:
        """Click the Potvrdit (confirm) button."""
        potvrdit = self.page.locator('.ob-c-date-range-input__submit-button').first
        if potvrdit.is_visible(timeout=2000):
            potvrdit.click(force=True, timeout=5000)
            logger.info("    Clicked Potvrdit")
            time.sleep(1)
            return True
        else:
            logger.warning("    Potvrdit button not found")
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
            logger.info(f"    Date inputs - FROM: {from_val}, TO: {to_val} (expected: {expected})")
            
            from_matches = (from_val == expected)
            to_matches = (to_val == expected)
            both_match = from_matches and to_matches
            
            return (both_match, from_matches, to_matches)
        except Exception as e:
            logger.debug(f"    Could not verify dates: {e}")
            return (False, False, False)
    
    def _fix_single_date(self, which: str, year: int, month: int) -> bool:
        """
        Fix a single date (FROM or TO) by clicking it directly and selecting the correct date.
        which: 'from' or 'to'
        """
        logger.info(f"    Attempting to fix {which.upper()} date directly...")
        
        try:
            # Click the appropriate input
            input_id = '#default_from' if which == 'from' else '#default_to'
            date_input = self.page.locator(input_id).first
            
            if not date_input.is_visible(timeout=2000):
                logger.error(f"    {which.upper()} date input not visible")
                return False
            
            date_input.click(force=True, timeout=5000)
            time.sleep(1.5)
            
            # Verify picker opened
            picker = self.page.locator('.ob-c-date-range-input__dropdown').first
            if not picker.is_visible(timeout=3000):
                logger.error("    Date picker did not open for single date fix")
                return False
            
            # Navigate to correct year
            if not self._navigate_to_year(year):
                logger.error(f"    Could not navigate to year {year}")
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
            logger.error(f"    Error fixing single date: {e}")
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
        logger.info(f"  Setting date: {month:02d}/{year}")
        
        # ==================== STRATEGY 1: Triple-click ====================
        for attempt in range(2):  # Try triple-click twice
            attempt_label = "first" if attempt == 0 else "second (retry)"
            logger.info(f"    Triple-click strategy - {attempt_label} attempt")
            
            try:
                self._close_date_picker()
                
                # Click on FROM date input to open picker
                from_input = self.page.locator('#default_from').first
                if not from_input.is_visible(timeout=3000):
                    logger.error("    FROM date input not visible")
                    continue
                
                from_input.click(force=True, timeout=5000)
                time.sleep(1.5)
                
                # Verify picker opened
                picker = self.page.locator('.ob-c-date-range-input__dropdown').first
                if not picker.is_visible(timeout=3000):
                    logger.error("    Date picker did not open")
                    continue
                
                logger.info("    Date picker opened")
                
                # Navigate to correct year
                if not self._navigate_to_year(year):
                    logger.error(f"    Could not navigate to year {year}")
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
                    logger.info("    ✓ Dates verified successfully!")
                    time.sleep(3)  # Wait for data to reload
                    return True
                else:
                    logger.warning(f"    Date mismatch after {attempt_label} triple-click attempt")
                    
            except Exception as e:
                logger.error(f"    Error in triple-click attempt: {e}")
                self._close_date_picker()
        
        # ==================== STRATEGY 2: Fix individual dates ====================
        logger.info("    Triple-click failed twice, trying to fix individual dates...")
        
        # Check which dates are wrong
        both_match, from_ok, to_ok = self._verify_dates(year, month)
        
        if both_match:
            # Dates are now correct (maybe delayed update)
            logger.info("    ✓ Dates are now correct!")
            time.sleep(3)
            return True
        
        # Fix FROM if needed
        if not from_ok:
            logger.info("    FROM date is incorrect, fixing...")
            if self._fix_single_date('from', year, month):
                # Recheck
                both_match, from_ok, to_ok = self._verify_dates(year, month)
                if both_match:
                    logger.info("    ✓ Dates verified after FROM fix!")
                    time.sleep(3)
                    return True
            else:
                logger.error("    Failed to fix FROM date")
        
        # Fix TO if needed  
        if not to_ok:
            logger.info("    TO date is incorrect, fixing...")
            if self._fix_single_date('to', year, month):
                # Final check
                both_match, from_ok, to_ok = self._verify_dates(year, month)
                if both_match:
                    logger.info("    ✓ Dates verified after TO fix!")
                    time.sleep(3)
                    return True
            else:
                logger.error("    Failed to fix TO date")
        
        # ==================== ALL STRATEGIES FAILED ====================
        logger.error(f"    ❌ All date setting strategies failed for {month:02d}/{year} - SKIPPING this record")
        self._close_date_picker()
        return False
    
    def switch_kategorie(self, kategorie: str) -> bool:
        """Switch between Prodej and Pronájem category.
        
        Args:
            kategorie: Either 'Prodej' or 'Pronájem'
        """
        logger.info(f"    Switching to kategorie: {kategorie}")
        try:
            selector = self.page.locator(f'label:has-text("{kategorie}"), span:has-text("{kategorie}")').first
            if selector.is_visible(timeout=3000):
                selector.click(force=True)
                time.sleep(2)  # Wait for data to reload
                logger.info(f"    [OK] Switched to: {kategorie}")
                return True
            else:
                logger.warning(f"    [WARN] Kategorie selector not visible: {kategorie}")
                return False
        except Exception as e:
            logger.warning(f"    [WARN] Could not switch kategorie to {kategorie}: {e}")
            return False
            
    def extract_data(self) -> Dict[str, Any]:
        """Extract data from page - fast version with short timeouts."""
        data = {
            'prumerna_cena': None,
            'prumerna_doba_inzerce': None,
            'aktivnich_nabidek': None,
            'pocet_novych_nabidek': None,
            'prumerny_pocet_zobrazeni': None
        }
        
        try:
            # Short wait for data to settle
            time.sleep(1)
            
            # Find data cards - use more specific selector
            cards = self.page.locator('div.sds-surface.sds-surface--03').all()
            
            # Only check first 10 cards max to avoid long waits
            for card in cards[:10]:
                try:
                    # Very short timeouts - 1 second each
                    label = card.locator('p').first.inner_text(timeout=1000).strip()
                    value_text = card.locator('b').first.inner_text(timeout=1000).strip()
                    
                    # Clean value
                    value_clean = value_text.replace('\xa0', '').replace(' ', '').replace('\u00a0', '')
                    try:
                        value = float(value_clean)
                    except:
                        continue
                        
                    if 'Průměrná cena' in label:
                        data['prumerna_cena'] = value
                    elif 'Průměrná doba inzerce' in label:
                        data['prumerna_doba_inzerce'] = value
                    elif 'Aktivních nabídek' in label:
                        data['aktivnich_nabidek'] = value
                    elif 'Počet nových nabídek' in label:
                        data['pocet_novych_nabidek'] = value
                    elif 'Průměrný počet zobrazení' in label:
                        data['prumerny_pocet_zobrazeni'] = value
                        
                except:
                    continue
                    
            logger.info(f"    Extracted: cena={data['prumerna_cena']}, doba={data['prumerna_doba_inzerce']}, nabidek={data['aktivnich_nabidek']}, novych={data['pocet_novych_nabidek']}, zobrazeni={data['prumerny_pocet_zobrazeni']}")
            return data
            
        except Exception as e:
            logger.error(f"    Extract error: {e}")
            return data
            
    def run_test_random(self) -> Dict:
        """Run test in randomized order."""
        logger.info("\n" + "="*60)
        logger.info("RUN 1: RANDOMIZED ORDER")
        logger.info("="*60)
        
        results = {}
        
        # Generate random order
        tasks = [(city, year, month) for city in TEST_CITIES for year, month in TEST_MONTHS]
        random.shuffle(tasks)
        
        logger.info(f"Tasks in random order: {[(t[0], f'{t[2]:02d}/{t[1]}') for t in tasks]}")
        
        try:
            # Start fresh
            self.start_browser(headless=False)
            self.login()
            self.navigate_to_prices()
            self.set_all_filters()
            
            for i, (city, year, month) in enumerate(tasks):
                try:
                    logger.info(f"\n[{i+1}/{len(tasks)}] {city} - {month:02d}/{year}")
                    
                    # Set filters
                    self.set_city(city)
                    self.set_date(year, month)
                    
                    # Extract data
                    data = self.extract_data()
                    
                    key = f"{city}_{year}-{month:02d}"
                    results[key] = data
                    
                    # Small delay between tasks
                    time.sleep(random.uniform(1, 2))
                    
                except Exception as e:
                    logger.error(f"Error scraping {city} {month:02d}/{year}: {e}")
                    key = f"{city}_{year}-{month:02d}"
                    results[key] = {'prumerna_cena': None, 'error': str(e)}
                    
        except Exception as e:
            logger.error(f"Run 1 fatal error: {e}")
        finally:
            try:
                self.close_browser()
            except:
                pass
                
        return results
        
    def run_test_sequential(self) -> Dict:
        """Run test in sequential order (city by city, month by month)."""
        logger.info("\n" + "="*60)
        logger.info("RUN 2: SEQUENTIAL ORDER")
        logger.info("="*60)
        
        results = {}
        
        try:
            # Start fresh
            self.start_browser(headless=False)
            self.login()
            self.navigate_to_prices()
            self.set_all_filters()
            
            task_num = 0
            total_tasks = len(TEST_CITIES) * len(TEST_MONTHS)
            
            for city in TEST_CITIES:
                logger.info(f"\n--- Processing city: {city} ---")
                self.set_city(city)
                
                for year, month in TEST_MONTHS:
                    try:
                        task_num += 1
                        logger.info(f"\n[{task_num}/{total_tasks}] {city} - {month:02d}/{year}")
                        
                        # Set date
                        self.set_date(year, month)
                        
                        # Extract data
                        data = self.extract_data()
                        
                        key = f"{city}_{year}-{month:02d}"
                        results[key] = data
                        
                        # Small delay
                        time.sleep(random.uniform(0.5, 1))
                        
                    except Exception as e:
                        logger.error(f"Error scraping {city} {month:02d}/{year}: {e}")
                        key = f"{city}_{year}-{month:02d}"
                        results[key] = {'prumerna_cena': None, 'error': str(e)}
                        
        except Exception as e:
            logger.error(f"Run 2 fatal error: {e}")
        finally:
            try:
                self.close_browser()
            except:
                pass
                
        return results
        
    def compare_results(self) -> Dict:
        """Compare results from both runs."""
        logger.info("\n" + "="*60)
        logger.info("COMPARING RESULTS")
        logger.info("="*60)
        
        comparison = {
            'matches': 0,
            'mismatches': 0,
            'details': []
        }
        
        all_keys = set(self.results_random.keys()) | set(self.results_sequential.keys())
        
        for key in sorted(all_keys):
            rand_val = self.results_random.get(key, {}).get('prumerna_cena')
            seq_val = self.results_sequential.get(key, {}).get('prumerna_cena')
            
            match = rand_val == seq_val
            if match:
                comparison['matches'] += 1
            else:
                comparison['mismatches'] += 1
                
            comparison['details'].append({
                'key': key,
                'random_cena': rand_val,
                'sequential_cena': seq_val,
                'match': match
            })
            
            status = "✓ MATCH" if match else "✗ MISMATCH"
            logger.info(f"  {key}: Random={rand_val}, Sequential={seq_val} -> {status}")
            
        total = comparison['matches'] + comparison['mismatches']
        match_rate = (comparison['matches'] / total * 100) if total > 0 else 0
        
        logger.info(f"\n{'='*60}")
        logger.info(f"SUMMARY: {comparison['matches']}/{total} matches ({match_rate:.1f}%)")
        logger.info(f"{'='*60}")
        
        return comparison
        
    def run_full_test(self):
        """Run the complete validation test."""
        logger.info("Starting Sreality Scraper Validation Test")
        logger.info(f"Testing {len(TEST_CITIES)} cities x {len(TEST_MONTHS)} months = {len(TEST_CITIES) * len(TEST_MONTHS)} data points")
        
        # Run both tests
        self.results_random = self.run_test_random()
        
        logger.info("\nWaiting 10 seconds between runs...")
        time.sleep(10)
        
        self.results_sequential = self.run_test_sequential()
        
        # Compare
        comparison = self.compare_results()
        
        # Save results
        output = {
            'timestamp': datetime.now().isoformat(),
            'test_cities': TEST_CITIES,
            'test_months': TEST_MONTHS,
            'results_random': self.results_random,
            'results_sequential': self.results_sequential,
            'comparison': comparison
        }
        
        output_file = f"validation_test_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json"
        with open(output_file, 'w', encoding='utf-8') as f:
            json.dump(output, f, ensure_ascii=False, indent=2)
            
        logger.info(f"\nResults saved to: {output_file}")
        
        return output


if __name__ == '__main__':
    test = ValidationTest()
    test.run_full_test()
