# City Batches Overview

## Batch 1 (6 cities) → config_batch1.json → output_batch1/
1. Teplice
2. Most
3. Ústí nad Labem
4. Chomutov
5. Sokolov
6. Beroun

## Batch 2 (6 cities) → config_batch2.json → output_batch2/
7. Králův Dvůr
8. Kladno
9. Hořovice
10. Mariánské Lázně
11. Tachov
12. Cheb

## Batch 3 (6 cities) → config_batch3.json → output_batch3/
13. Ostrov
14. Klatovy
15. Plzeň
16. Pardubice
17. Hradec Králové
18. Rychnov nad Kněžnou

## Batch 4 (6 cities) → config_batch4.json → output_batch4/
19. Chrudim
20. Jihlava
21. Havlíčkův Brod
22. Humpolec
23. Poděbrady
24. Nymburk

## Batch 5 (5 cities) → config_batch5.json → output_batch5/
25. Liberec
26. Česká Lípa
27. České Budějovice
28. Písek
29. Mladá Boleslav

---

## Estimated Time per Batch
- Each batch: ~6 cities × ~130 months = ~780 tasks
- With default delays: 4-6 hours per batch
- Headless mode: 2-4 hours per batch

## Running a Batch

### Windows
```
run_scraper.bat
→ Select batch number (1-5)
→ Select mode (Normal/Headless)
```

### Mac/Linux
```bash
./run_scraper.sh
→ Select batch number (1-5)
→ Select mode (Normal/Headless)
```

### Direct Command Line
```bash
# Run specific batch
python scraper.py --config config_batch1.json

# Run in headless mode
python scraper.py --config config_batch2.json --headless

# Test mode (2 cities, 1 year only)
python scraper.py --config config_batch1.json --test
```

## Output Folders
Each batch saves to its own folder:
- `output_batch1/` - Batch 1 results
- `output_batch2/` - Batch 2 results
- `output_batch3/` - Batch 3 results
- `output_batch4/` - Batch 4 results
- `output_batch5/` - Batch 5 results

## Resume Capability
Each batch has its own `progress.json` in its output folder.
If interrupted, just run the same batch again - it will skip completed tasks.
