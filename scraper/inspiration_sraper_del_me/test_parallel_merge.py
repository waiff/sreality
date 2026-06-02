#!/usr/bin/env python3
"""
Parallel Merge Test
===================
Tests the CSV merge functionality for parallel workers.

This test:
1. Creates fake chunk CSV files (simulating parallel worker output)
2. Calls the merge function
3. Verifies merged output exists with correct data
4. Tests various filter combination scenarios

No browser required - pure unit test.
"""

import os
import csv
import json
import glob
import shutil
import tempfile
from pathlib import Path
from datetime import datetime
import sys

# Test configuration
TEST_OUTPUT_DIR = Path('output_test_merge')


def sanitize(s):
    """Sanitize string for filename (must match scraper.py and scraper_gui.py)."""
    return s.lower().replace(' ', '').replace('í', 'i').replace('á', 'a').replace('ý', 'y').replace('ě', 'e').replace('ž', 'z').replace('š', 's').replace('č', 'c').replace('ř', 'r').replace('ů', 'u').replace('ú', 'u')


def create_chunk_csv(output_dir: Path, filter_suffix: str, chunk_num: int, metric: str, 
                     cities: list, dates: list, base_value: float, 
                     kategorie: str = 'Prodej', typ: str = 'Byty',
                     stav: str = 'Velmi dobrý', konst: str = 'Panel', vlast: str = 'Osobní'):
    """Create a fake chunk CSV file with filter columns."""
    timestamp = datetime.now().strftime('%Y%m%d')
    filename = f'sreality_data{filter_suffix}_chunk{chunk_num}_{metric}_{timestamp}.csv'
    filepath = output_dir / filename
    
    filter_cols = ['City', 'Kategorie', 'Typ', 'Stav objektu', 'Konstrukce', 
                   'Vlastnictví', 'Užitná plocha od', 'Užitná plocha do', 'V okolí']
    
    with open(filepath, 'w', newline='', encoding='utf-8-sig') as f:
        writer = csv.writer(f)
        writer.writerow(filter_cols + dates)
        for i, city in enumerate(cities):
            # Filter values + date values
            filter_vals = [city, kategorie, typ, stav, konst, vlast, 30, 80, 'nezadáno']
            date_vals = [str(base_value + i * 100 + j * 10) for j in range(len(dates))]
            writer.writerow(filter_vals + date_vals)
    
    print(f"  Created: {filename}")
    return filepath


def create_chunk_csv_pandas_format(output_dir: Path, filter_suffix: str, chunk_num: int, metric: str, 
                                    cities: list, dates: list, base_value: float):
    """Create a fake chunk CSV file in pandas format (empty first column header) - legacy format."""
    timestamp = datetime.now().strftime('%Y%m%d')
    filename = f'sreality_data{filter_suffix}_chunk{chunk_num}_{metric}_{timestamp}.csv'
    filepath = output_dir / filename
    
    # Old format without filter columns - just City + dates
    with open(filepath, 'w', newline='', encoding='utf-8-sig') as f:
        writer = csv.writer(f)
        writer.writerow(['City'] + dates)
        for i, city in enumerate(cities):
            row = [city] + [str(base_value + i * 100 + j * 10) for j in range(len(dates))]
            writer.writerow(row)
    
    print(f"  Created (legacy format): {filename}")
    return filepath


def merge_parallel_outputs(output_dir: Path, num_workers: int, combo: dict = None):
    """
    Merge function copied from scraper_gui.py for testing.
    Returns dict with merge results.
    """
    timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
    
    # Generate filter suffix if combo provided
    filter_suffix = ""
    if combo:
        filter_suffix = f"_{sanitize(combo.get('stav_objektu', ''))}_{sanitize(combo.get('konstrukce', ''))}_{sanitize(combo.get('vlastnictvi', ''))}"
    
    print(f"\n  Merge Debug:")
    print(f"    filter_suffix = '{filter_suffix}'")
    print(f"    output_dir = {output_dir}")
    
    # List all CSV files
    all_csvs = list(output_dir.glob('*.csv'))
    print(f"    Found {len(all_csvs)} CSV files:")
    for csv_file in all_csvs:
        print(f"      - {csv_file.name}")
    
    metrics = [
        'prumerna_cena_prodej', 'prumerna_cena_pronajem',
        'prumerna_doba_inzerce_prodej', 'prumerna_doba_inzerce_pronajem',
        'aktivnich_nabidek_prodej', 'aktivnich_nabidek_pronajem',
        'pocet_novych_nabidek_prodej', 'pocet_novych_nabidek_pronajem',
        'prumerny_pocet_zobrazeni_prodej', 'prumerny_pocet_zobrazeni_pronajem',
    ]
    
    results = {
        'merged_files': [],
        'metrics_processed': 0,
        'total_chunks_merged': 0,
    }
    
    for metric in metrics:
        chunk_files = []
        for i in range(1, num_workers + 1):
            pattern = str(output_dir / f'sreality_data{filter_suffix}_chunk{i}_{metric}_*.csv')
            print(f"    Searching: {pattern}")
            files = glob.glob(pattern)
            print(f"    Found: {len(files)} files")
            chunk_files.extend(files)
        
        if not chunk_files:
            continue
        
        print(f"\n  Merging {len(chunk_files)} files for {metric}...")
        
        # Filter columns that should be preserved (not merged as dates)
        filter_columns = {'City', 'city', 'Kategorie', 'Typ', 'Stav objektu', 'Konstrukce', 
                          'Vlastnictví', 'Užitná plocha od', 'Užitná plocha do', 'V okolí', ''}
        
        # Read and merge
        all_data = {}  # city -> {'filters': {...}, 'dates': {date -> value}}
        all_dates = set()
        
        for filepath in chunk_files:
            try:
                with open(filepath, 'r', encoding='utf-8-sig') as f:
                    reader = csv.DictReader(f)
                    fieldnames = reader.fieldnames
                    first_col = fieldnames[0] if fieldnames else None
                    
                    for row in reader:
                        # Try multiple ways to get city name
                        city = row.get('City') or row.get('city') or row.get(first_col) or row.get('')
                        if not city:
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
            except Exception as e:
                print(f"    Warning: Could not read {filepath}: {e}")
        
        if not all_data:
            continue
        
        # Write merged file
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
        
        print(f"    ✓ Created: {merged_path.name}")
        results['merged_files'].append(merged_path.name)
        results['metrics_processed'] += 1
        results['total_chunks_merged'] += len(chunk_files)
        
        # Delete chunk files
        for filepath in chunk_files:
            try:
                os.remove(filepath)
            except:
                pass
    
    return results


def verify_merged_file(output_dir: Path, filter_suffix: str, metric: str, 
                       expected_cities: list, expected_dates: list):
    """Verify a merged file has correct content."""
    # Find the merged file
    pattern = str(output_dir / f'sreality_data{filter_suffix}_{metric}_*.csv')
    files = glob.glob(pattern)
    
    if not files:
        print(f"    ✗ FAIL: No merged file found for pattern {pattern}")
        return False
    
    filepath = files[0]
    print(f"    Checking: {Path(filepath).name}")
    
    with open(filepath, 'r', encoding='utf-8-sig') as f:
        reader = csv.DictReader(f)
        rows = list(reader)
    
    # Check cities
    found_cities = [row['City'] for row in rows]
    if sorted(found_cities) != sorted(expected_cities):
        print(f"    ✗ FAIL: Cities mismatch. Expected {expected_cities}, got {found_cities}")
        return False
    
    # Filter columns to exclude from date check
    filter_columns = {'City', 'Kategorie', 'Typ', 'Stav objektu', 'Konstrukce', 
                      'Vlastnictví', 'Užitná plocha od', 'Užitná plocha do', 'V okolí'}
    
    # Check dates (columns) - exclude filter columns
    headers = list(rows[0].keys()) if rows else []
    found_dates = [h for h in headers if h not in filter_columns]
    if sorted(found_dates) != sorted(expected_dates):
        print(f"    ✗ FAIL: Dates mismatch. Expected {expected_dates}, got {found_dates}")
        return False
    
    print(f"    ✓ PASS: {len(found_cities)} cities, {len(found_dates)} dates")
    return True


def test_scenario_1_basic_merge():
    """Test basic merge with 2 workers, no filter combo."""
    print("\n" + "="*60)
    print("TEST 1: Basic merge (2 workers, no filter combo)")
    print("="*60)
    
    # Setup
    output_dir = TEST_OUTPUT_DIR / 'test1'
    output_dir.mkdir(parents=True, exist_ok=True)
    
    cities_chunk1 = ['Praha', 'Brno']
    cities_chunk2 = ['Ostrava', 'Plzeň']
    dates = ['2024-06', '2024-07', '2024-08']
    metric = 'prumerna_cena_prodej'
    
    print("\n1. Creating chunk files...")
    create_chunk_csv(output_dir, '', 1, metric, cities_chunk1, dates, 50000)
    create_chunk_csv(output_dir, '', 2, metric, cities_chunk2, dates, 40000)
    
    print("\n2. Running merge...")
    results = merge_parallel_outputs(output_dir, num_workers=2, combo=None)
    
    print("\n3. Verifying results...")
    success = results['metrics_processed'] > 0
    if success:
        success = verify_merged_file(output_dir, '', metric, 
                                     cities_chunk1 + cities_chunk2, dates)
    
    # Cleanup
    shutil.rmtree(output_dir)
    
    return success


def test_scenario_2_filter_combo():
    """Test merge with filter combination."""
    print("\n" + "="*60)
    print("TEST 2: Merge with filter combo (3 workers)")
    print("="*60)
    
    # Setup
    output_dir = TEST_OUTPUT_DIR / 'test2'
    output_dir.mkdir(parents=True, exist_ok=True)
    
    combo = {
        'stav_objektu': 'Velmi dobrý',
        'konstrukce': 'Cihla',
        'vlastnictvi': 'Osobní'
    }
    filter_suffix = f"_{sanitize(combo['stav_objektu'])}_{sanitize(combo['konstrukce'])}_{sanitize(combo['vlastnictvi'])}"
    
    print(f"\n  Filter combo: {combo}")
    print(f"  Expected suffix: '{filter_suffix}'")
    
    cities_all = ['Praha', 'Brno', 'Ostrava', 'Plzeň', 'Liberec', 'Olomouc']
    dates = ['2024-01', '2024-02']
    metric = 'prumerna_cena_prodej'
    
    print("\n1. Creating chunk files...")
    create_chunk_csv(output_dir, filter_suffix, 1, metric, cities_all[:2], dates, 60000)
    create_chunk_csv(output_dir, filter_suffix, 2, metric, cities_all[2:4], dates, 50000)
    create_chunk_csv(output_dir, filter_suffix, 3, metric, cities_all[4:], dates, 45000)
    
    print("\n2. Running merge...")
    results = merge_parallel_outputs(output_dir, num_workers=3, combo=combo)
    
    print("\n3. Verifying results...")
    success = results['metrics_processed'] > 0
    if success:
        success = verify_merged_file(output_dir, filter_suffix, metric, cities_all, dates)
    
    # Cleanup
    shutil.rmtree(output_dir)
    
    return success


def test_scenario_3_multiple_metrics():
    """Test merge with multiple metrics."""
    print("\n" + "="*60)
    print("TEST 3: Multiple metrics (2 workers)")
    print("="*60)
    
    # Setup
    output_dir = TEST_OUTPUT_DIR / 'test3'
    output_dir.mkdir(parents=True, exist_ok=True)
    
    cities_chunk1 = ['Praha', 'Brno']
    cities_chunk2 = ['Ostrava']
    dates = ['2024-06', '2024-07']
    metrics = ['prumerna_cena_prodej', 'prumerna_doba_inzerce_prodej', 'aktivnich_nabidek_prodej']
    
    print("\n1. Creating chunk files for 3 metrics...")
    for metric in metrics:
        create_chunk_csv(output_dir, '', 1, metric, cities_chunk1, dates, 50000)
        create_chunk_csv(output_dir, '', 2, metric, cities_chunk2, dates, 40000)
    
    print("\n2. Running merge...")
    results = merge_parallel_outputs(output_dir, num_workers=2, combo=None)
    
    print("\n3. Verifying results...")
    success = results['metrics_processed'] == 3
    print(f"  Metrics processed: {results['metrics_processed']} (expected 3)")
    
    if success:
        for metric in metrics:
            if not verify_merged_file(output_dir, '', metric, 
                                      cities_chunk1 + cities_chunk2, dates):
                success = False
                break
    
    # Cleanup
    shutil.rmtree(output_dir)
    
    return success


def test_scenario_4_czech_characters():
    """Test with Czech characters in filter values."""
    print("\n" + "="*60)
    print("TEST 4: Czech characters in filters")
    print("="*60)
    
    # Setup
    output_dir = TEST_OUTPUT_DIR / 'test4'
    output_dir.mkdir(parents=True, exist_ok=True)
    
    combo = {
        'stav_objektu': 'Před rekonstrukcí',
        'konstrukce': 'Ostatní',
        'vlastnictvi': 'Družstevní'
    }
    filter_suffix = f"_{sanitize(combo['stav_objektu'])}_{sanitize(combo['konstrukce'])}_{sanitize(combo['vlastnictvi'])}"
    
    print(f"\n  Filter combo: {combo}")
    print(f"  Sanitized suffix: '{filter_suffix}'")
    
    # Verify sanitization
    expected_suffix = '_predrekonstrukci_ostatni_druzstevni'
    if filter_suffix != expected_suffix:
        print(f"  ✗ FAIL: Sanitization mismatch. Expected '{expected_suffix}', got '{filter_suffix}'")
        shutil.rmtree(output_dir)
        return False
    print(f"  ✓ Sanitization correct")
    
    cities = ['Praha', 'Brno']
    dates = ['2024-06']
    metric = 'prumerna_cena_prodej'
    
    print("\n1. Creating chunk files...")
    create_chunk_csv(output_dir, filter_suffix, 1, metric, cities[:1], dates, 60000)
    create_chunk_csv(output_dir, filter_suffix, 2, metric, cities[1:], dates, 50000)
    
    print("\n2. Running merge...")
    results = merge_parallel_outputs(output_dir, num_workers=2, combo=combo)
    
    print("\n3. Verifying results...")
    success = results['metrics_processed'] > 0
    if success:
        success = verify_merged_file(output_dir, filter_suffix, metric, cities, dates)
    
    # Cleanup
    shutil.rmtree(output_dir)
    
    return success


def test_scenario_5_missing_chunks():
    """Test behavior when some chunk files are missing."""
    print("\n" + "="*60)
    print("TEST 5: Missing chunk files (3 workers, only 2 have output)")
    print("="*60)
    
    # Setup
    output_dir = TEST_OUTPUT_DIR / 'test5'
    output_dir.mkdir(parents=True, exist_ok=True)
    
    cities_chunk1 = ['Praha', 'Brno']
    cities_chunk3 = ['Plzeň']  # Note: chunk 2 is missing
    dates = ['2024-06']
    metric = 'prumerna_cena_prodej'
    
    print("\n1. Creating chunk files (chunk 2 missing)...")
    create_chunk_csv(output_dir, '', 1, metric, cities_chunk1, dates, 50000)
    # Deliberately NOT creating chunk 2
    create_chunk_csv(output_dir, '', 3, metric, cities_chunk3, dates, 40000)
    
    print("\n2. Running merge (expecting 3 workers)...")
    results = merge_parallel_outputs(output_dir, num_workers=3, combo=None)
    
    print("\n3. Verifying results...")
    # Should still merge what's available
    success = results['metrics_processed'] > 0
    if success:
        success = verify_merged_file(output_dir, '', metric, 
                                     cities_chunk1 + cities_chunk3, dates)
    
    # Cleanup
    shutil.rmtree(output_dir)
    
    return success


def test_scenario_6_full_timestamp():
    """Test with full timestamp format (YYYYMMDD_HHMMSS) matching real scraper."""
    print("\n" + "="*60)
    print("TEST 6: Full timestamp format (YYYYMMDD_HHMMSS)")
    print("="*60)
    
    # Setup
    output_dir = TEST_OUTPUT_DIR / 'test6'
    output_dir.mkdir(parents=True, exist_ok=True)
    
    # Use exact timestamp format from scraper.py export_final()
    timestamp_full = datetime.now().strftime('%Y%m%d_%H%M%S')
    
    combo = {
        'stav_objektu': 'Novostavba',
        'konstrukce': 'Cihla',
        'vlastnictvi': 'Osobní'
    }
    filter_suffix = f"_{sanitize(combo['stav_objektu'])}_{sanitize(combo['konstrukce'])}_{sanitize(combo['vlastnictvi'])}"
    
    print(f"\n  Using full timestamp: {timestamp_full}")
    print(f"  Filter suffix: '{filter_suffix}'")
    
    cities = ['Praha', 'Brno', 'Ostrava']
    dates = ['2024-06', '2024-07']
    metric = 'prumerna_cena_prodej'
    
    # Create files with FULL timestamp (like real scraper does)
    print("\n1. Creating chunk files with full timestamp...")
    for chunk_num in [1, 2]:
        chunk_cities = cities[:2] if chunk_num == 1 else cities[2:]
        filename = f'sreality_data{filter_suffix}_chunk{chunk_num}_{metric}_{timestamp_full}.csv'
        filepath = output_dir / filename
        
        with open(filepath, 'w', newline='', encoding='utf-8-sig') as f:
            writer = csv.writer(f)
            writer.writerow(['City'] + dates)
            for i, city in enumerate(chunk_cities):
                row = [city] + [str(50000 + i * 100 + j * 10) for j in range(len(dates))]
                writer.writerow(row)
        print(f"  Created: {filename}")
    
    print("\n2. Running merge...")
    results = merge_parallel_outputs(output_dir, num_workers=2, combo=combo)
    
    print("\n3. Verifying results...")
    success = results['metrics_processed'] > 0
    if success:
        success = verify_merged_file(output_dir, filter_suffix, metric, cities, dates)
    
    # Cleanup
    shutil.rmtree(output_dir)
    
    return success


def test_scenario_7_pandas_format():
    """Test merge with pandas format CSV (empty first column header)."""
    print("\n" + "="*60)
    print("TEST 7: Pandas format CSV (empty first column header)")
    print("="*60)
    
    # Setup
    output_dir = TEST_OUTPUT_DIR / 'test7'
    output_dir.mkdir(parents=True, exist_ok=True)
    
    cities_chunk1 = ['Praha', 'Brno']
    cities_chunk2 = ['Ostrava', 'Plzeň']
    dates = ['2024-06', '2024-07', '2024-08']
    metric = 'prumerna_cena_prodej'
    
    print("\n1. Creating chunk files in PANDAS format (empty first column)...")
    create_chunk_csv_pandas_format(output_dir, '', 1, metric, cities_chunk1, dates, 50000)
    create_chunk_csv_pandas_format(output_dir, '', 2, metric, cities_chunk2, dates, 40000)
    
    print("\n2. Running merge...")
    results = merge_parallel_outputs(output_dir, num_workers=2, combo=None)
    
    print("\n3. Verifying results...")
    success = results['metrics_processed'] > 0
    if success:
        success = verify_merged_file(output_dir, '', metric, 
                                     cities_chunk1 + cities_chunk2, dates)
    
    # Cleanup
    shutil.rmtree(output_dir)
    
    return success


def run_all_tests():
    """Run all test scenarios."""
    print("\n" + "="*60)
    print("PARALLEL MERGE TEST SUITE")
    print("="*60)
    
    # Clean up any previous test output
    if TEST_OUTPUT_DIR.exists():
        shutil.rmtree(TEST_OUTPUT_DIR)
    TEST_OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    
    results = []
    
    tests = [
        ("Basic merge (2 workers)", test_scenario_1_basic_merge),
        ("Filter combo (3 workers)", test_scenario_2_filter_combo),
        ("Multiple metrics", test_scenario_3_multiple_metrics),
        ("Czech characters", test_scenario_4_czech_characters),
        ("Missing chunks", test_scenario_5_missing_chunks),
        ("Full timestamp format", test_scenario_6_full_timestamp),
        ("Pandas format (empty header)", test_scenario_7_pandas_format),
    ]
    
    for name, test_func in tests:
        try:
            success = test_func()
            results.append((name, success))
        except Exception as e:
            print(f"\n  ✗ EXCEPTION: {e}")
            import traceback
            traceback.print_exc()
            results.append((name, False))
    
    # Final cleanup
    if TEST_OUTPUT_DIR.exists():
        shutil.rmtree(TEST_OUTPUT_DIR)
    
    # Summary
    print("\n" + "="*60)
    print("TEST SUMMARY")
    print("="*60)
    
    passed = sum(1 for _, s in results if s)
    total = len(results)
    
    for name, success in results:
        status = "✓ PASS" if success else "✗ FAIL"
        print(f"  {status}: {name}")
    
    print(f"\n  Total: {passed}/{total} tests passed")
    print("="*60)
    
    return passed == total


if __name__ == '__main__':
    success = run_all_tests()
    sys.exit(0 if success else 1)
