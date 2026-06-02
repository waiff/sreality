#!/bin/bash

# Colors
RED='\033[0;31m'
GREEN='\033[0;32m'
BLUE='\033[0;34m'
CYAN='\033[0;36m'
YELLOW='\033[1;33m'
NC='\033[0m'

clear
echo ""
echo -e "${CYAN}╔════════════════════════════════════════════════════════════╗${NC}"
echo -e "${CYAN}║${NC}      ${GREEN}Sreality.cz Real Estate Price Scraper v2.0${NC}           ${CYAN}║${NC}"
echo -e "${CYAN}╚════════════════════════════════════════════════════════════╝${NC}"
echo ""

# Check Python
if ! command -v python3 &> /dev/null; then
    echo -e "${RED}ERROR: Python 3 is not installed.${NC}"
    echo "  macOS: brew install python3"
    echo "  Ubuntu: sudo apt install python3 python3-pip python3-venv"
    exit 1
fi

# Get script directory
SCRIPT_DIR="$( cd "$( dirname "${BASH_SOURCE[0]}" )" && pwd )"
cd "$SCRIPT_DIR"

# Setup venv if needed
if [ ! -d "venv" ]; then
    echo -e "${YELLOW}First time setup - installing dependencies...${NC}"
    python3 -m venv venv
    source venv/bin/activate
    pip install -r requirements.txt
    playwright install chromium
    echo -e "${GREEN}Setup complete!${NC}"
else
    source venv/bin/activate
fi

run_batch() {
    local config=$1
    echo ""
    echo -e "${BLUE}Select run mode:${NC}"
    echo "  [1] Normal (browser visible)"
    echo "  [2] Headless (no browser - faster)"
    echo ""
    read -p "Enter mode (1 or 2): " mode
    
    if [ "$mode" == "2" ]; then
        python3 scraper.py --config "$config" --headless
    else
        python3 scraper.py --config "$config"
    fi
}

while true; do
    echo ""
    echo -e "${CYAN}═══════════════════════════════════════════════════════════════${NC}"
    echo -e "  ${GREEN}SELECT BATCH TO RUN:${NC}"
    echo -e "${CYAN}═══════════════════════════════════════════════════════════════${NC}"
    echo ""
    echo -e "  ${GREEN}[1]${NC} Batch 1: Teplice, Most, Ústí nad Labem, Chomutov,"
    echo "               Sokolov, Beroun"
    echo ""
    echo -e "  ${GREEN}[2]${NC} Batch 2: Králův Dvůr, Kladno, Hořovice, Mariánské Lázně,"
    echo "               Tachov, Cheb"
    echo ""
    echo -e "  ${GREEN}[3]${NC} Batch 3: Ostrov, Klatovy, Plzeň, Pardubice,"
    echo "               Hradec Králové, Rychnov nad Kněžnou"
    echo ""
    echo -e "  ${GREEN}[4]${NC} Batch 4: Chrudim, Jihlava, Havlíčkův Brod, Humpolec,"
    echo "               Poděbrady, Nymburk"
    echo ""
    echo -e "  ${GREEN}[5]${NC} Batch 5: Liberec, Česká Lípa, České Budějovice,"
    echo "               Písek, Mladá Boleslav"
    echo ""
    echo -e "  ${GREEN}[A]${NC} Run ALL batches (full config.json)"
    echo ""
    echo -e "  ${GREEN}[T]${NC} Test mode (2 cities, 1 year)"
    echo ""
    echo -e "  ${GREEN}[0]${NC} Exit"
    echo ""
    echo -e "${CYAN}═══════════════════════════════════════════════════════════════${NC}"
    
    read -p "Enter your choice: " batch
    
    case $batch in
        1)
            echo -e "\n${BLUE}Starting Batch 1...${NC}"
            run_batch "config_batch1.json"
            ;;
        2)
            echo -e "\n${BLUE}Starting Batch 2...${NC}"
            run_batch "config_batch2.json"
            ;;
        3)
            echo -e "\n${BLUE}Starting Batch 3...${NC}"
            run_batch "config_batch3.json"
            ;;
        4)
            echo -e "\n${BLUE}Starting Batch 4...${NC}"
            run_batch "config_batch4.json"
            ;;
        5)
            echo -e "\n${BLUE}Starting Batch 5...${NC}"
            run_batch "config_batch5.json"
            ;;
        [Aa])
            echo -e "\n${BLUE}Starting ALL cities...${NC}"
            run_batch "config.json"
            ;;
        [Tt])
            echo -e "\n${BLUE}Starting TEST mode...${NC}"
            python3 scraper.py --config config_batch1.json --test
            ;;
        0)
            echo "Goodbye!"
            exit 0
            ;;
        *)
            echo -e "${RED}Invalid choice. Please try again.${NC}"
            ;;
    esac
    
    echo ""
    echo -e "${CYAN}═══════════════════════════════════════════════════════════════${NC}"
    echo -e "${GREEN}Batch finished! Check the output folder for results.${NC}"
    echo -e "${CYAN}═══════════════════════════════════════════════════════════════${NC}"
    echo ""
    read -p "Press Enter to continue..."
done
