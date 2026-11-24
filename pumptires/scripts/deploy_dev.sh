#!/bin/bash
#=======================================================================
# PumpTires Pending Monitor Development Deployment Script
#=======================================================================
# 
# This script starts the PumpTires Pending Monitor in development mode
# using tmux for process management.
#
# Components started:
# 1. Mempool Listener - PulseChain WebSocket listener for createToken events
# 2. Log Monitor - Real-time log monitoring
# 3. Event Monitor - JSONL event file monitoring
#
# No hardcoded paths - automatically detects project root
#=======================================================================

set -e  # Exit immediately if a command exits with a non-zero status

# Define colors for output
BOLD="\033[1m"
GREEN="\033[0;32m"
YELLOW="\033[1;33m"
RED="\033[0;31m"
BLUE="\033[0;34m"
CYAN="\033[0;36m"
RESET="\033[0m"

# Print header
echo -e "${BLUE}=======================================================${RESET}"
echo -e "${BLUE}   PumpTires Pending Monitor - Development Mode       ${RESET}"
echo -e "${BLUE}=======================================================${RESET}"

# Get project directory (script is in scripts/ subdirectory)
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
cd "$PROJECT_DIR"

echo -e "${GREEN}Project directory: $PROJECT_DIR${RESET}"

# Check if we're in the project root directory
if [ ! -f "pyproject.toml" ] || [ ! -d "app" ]; then
    echo -e "${RED}Error: Invalid project structure. Missing pyproject.toml or app/ directory${RESET}"
    echo -e "${RED}Current directory: $(pwd)${RESET}"
    exit 1
fi

# Create data directory if it doesn't exist
if [ ! -d "data" ]; then
    echo -e "${YELLOW}Creating data directory...${RESET}"
    mkdir -p data
fi

# Create logs directory if it doesn't exist
if [ ! -d "logs" ]; then
    echo -e "${YELLOW}Creating logs directory...${RESET}"
    mkdir -p logs
fi

echo -e "${GREEN}✓ Directory structure verified${RESET}"

# Clean Python cache
echo ""
echo -e "${CYAN}Cleaning Python cache...${RESET}"
find . -type d -name "__pycache__" -exec rm -rf {} + 2>/dev/null || true
find . -type f -name "*.pyc" -delete 2>/dev/null || true
echo -e "${GREEN}  ✓ Python cache cleaned${RESET}"

# Optionally remove old event file for clean start
if [ "$1" = "--clean" ]; then
    if [ -f "data/pending_create_token.jsonl" ]; then
        rm -f data/pending_create_token.jsonl
        echo -e "${GREEN}  ✓ Removed old events file (clean start)${RESET}"
    fi
fi

echo -e "${GREEN}✓ Cleanup complete${RESET}"

# Function to check if a command exists
command_exists() {
    command -v "$1" >/dev/null 2>&1
}

# Check for required commands
echo -e "\n${BOLD}Checking prerequisites...${RESET}"
MISSING_PREREQS=0

if ! command_exists python3; then
    echo -e "${RED}Error: Python3 not found${RESET}"
    MISSING_PREREQS=1
fi

if ! command_exists poetry; then
    echo -e "${RED}Error: Poetry not found. Please install Poetry first.${RESET}"
    echo -e "${YELLOW}Install with: curl -sSL https://install.python-poetry.org | python3 -${RESET}"
    MISSING_PREREQS=1
fi

if ! command_exists tmux; then
    echo -e "${RED}Error: tmux not found. tmux is required for development deployment.${RESET}"
    echo -e "${YELLOW}Install with: sudo apt-get install tmux${RESET}"
    MISSING_PREREQS=1
fi

if [ $MISSING_PREREQS -eq 1 ]; then
    echo -e "${RED}Please install missing prerequisites and try again.${RESET}"
    exit 1
fi

echo -e "${GREEN}✓ All prerequisites satisfied${RESET}"

# Check for .env file
if [ ! -f ".env" ]; then
    echo -e "${YELLOW}Warning: .env file not found${RESET}"
    if [ -f ".env.example" ]; then
        echo -e "${CYAN}Creating .env from .env.example...${RESET}"
        cp .env.example .env
        echo -e "${GREEN}✓ .env file created from template${RESET}"
        echo -e "${CYAN}Using default settings (no API keys needed for PulseChain public RPC)${RESET}"
    else
        echo -e "${RED}Error: No .env or .env.example file found${RESET}"
        exit 1
    fi
fi

echo -e "${GREEN}✓ Configuration verified${RESET}"

# Install dependencies
echo -e "\n${BOLD}Installing/updating Python dependencies with Poetry...${RESET}"
poetry install --no-interaction --no-ansi
echo -e "${GREEN}✓ Dependencies installed${RESET}"

# Run linters
echo -e "\n${BOLD}Running code quality checks...${RESET}"
echo -e "${CYAN}  Running mypy...${RESET}"
if poetry run mypy app 2>&1 | tee logs/mypy.log; then
    echo -e "${GREEN}  ✓ mypy passed${RESET}"
else
    echo -e "${YELLOW}  ⚠️  mypy found issues (see logs/mypy.log)${RESET}"
fi

echo -e "${GREEN}✓ Code quality checks complete${RESET}"

# Function to start a component in a new tmux window
start_component_tmux() {
    local component_name=$1
    local command=$2
    
    echo -e "  Starting ${component_name}..."
    tmux new-window -t pumptires -n "$component_name" "cd '$PROJECT_DIR' && $command; echo 'Process exited. Press Enter to close.'; read"
}

# Function to check if tmux session exists
tmux_session_exists() {
    tmux has-session -t pumptires 2>/dev/null
}

# Kill existing tmux session if it exists
if tmux_session_exists; then
    echo -e "\n${YELLOW}Existing tmux session 'pumptires' found. Killing it...${RESET}"
    tmux kill-session -t pumptires
    sleep 2
    echo -e "${GREEN}✓ Old session terminated${RESET}"
fi

# Start Services
echo -e "\n${BOLD}Starting PumpTires Monitor service...${RESET}"

# Create a new tmux session
echo -e "\n${BLUE}Creating tmux session 'pumptires'...${RESET}"
tmux new-session -d -s pumptires -n "monitor"

# 1. Start Mempool Listener
echo -e "\n${BOLD}[1/3] Starting Mempool Listener...${RESET}"
echo -e "${CYAN}This service monitors PulseChain mempool for createToken transactions${RESET}"
# Create log file with timestamp for this session
LOG_FILE="logs/console_$(date +%Y%m%d_%H%M%S).log"
echo -e "${CYAN}Logging to: $LOG_FILE${RESET}"
tmux send-keys -t pumptires:monitor "cd '$PROJECT_DIR' && poetry run python -m app.main 2>&1 | tee '$LOG_FILE'" C-m
echo -e "${YELLOW}Waiting for mempool listener to initialize (10s)...${RESET}"
sleep 10

# 2. Create a log monitoring window (tail the main process logs, don't start another app instance)
echo -e "\n${BOLD}[2/3] Starting Console Log Monitor...${RESET}"
# Find the most recent console log file or wait for it to be created
start_component_tmux "console-logs" "echo 'Waiting for main process log...' && sleep 2 && LOG_FILE=\$(ls -t logs/console_*.log 2>/dev/null | head -1) && if [ -n \"\$LOG_FILE\" ]; then tail -f \"\$LOG_FILE\"; else echo 'Main process will create log via tmux logging. Attach to monitor window (Ctrl+b, 0) to see live output.'; tail -f /dev/null; fi"

# 3. Create an event monitoring window
echo -e "\n${BOLD}[3/3] Starting Event File Monitor...${RESET}"
start_component_tmux "event-logs" "echo 'Monitoring: data/pending_create_token.jsonl' && echo 'Waiting for events...' && tail -f data/pending_create_token.jsonl 2>/dev/null | while read line; do echo \$line | python3 -m json.tool 2>/dev/null || echo \$line; done || (echo 'No events yet. File will be created when first event is detected.' && tail -f /dev/null)"

echo -e "\n${GREEN}✓ All services started${RESET}"

# Check if listener is running
echo -e "\n${BOLD}Verifying listener status...${RESET}"
sleep 2

# Check if the Python process is running
if pgrep -f "app.main" > /dev/null; then
    echo -e "${GREEN}✓ Mempool listener process is running${RESET}"
else
    echo -e "${YELLOW}⚠️  Mempool listener process not detected (may need more time)${RESET}"
fi

# Display information
echo -e "\n${GREEN}${BOLD}PumpTires Monitor started successfully!${RESET}"
echo -e "\n${CYAN}═══════════════════════════════════════════════════════${RESET}"
echo -e "${BOLD}Service Information:${RESET}"
echo -e "${CYAN}═══════════════════════════════════════════════════════${RESET}"
echo -e "${BOLD}Mempool Listener:${RESET}"
echo -e "  Network: ${GREEN}PulseChain Mainnet${RESET}"
echo -e "  RPC: ${GREEN}wss://pulsechain-rpc.publicnode.com${RESET}"
echo -e "  Factory: ${GREEN}0x6538A83a81d855B965983161AF6a83e616D16fD5${RESET}"
echo -e "  Function: ${GREEN}createToken(string,string)${RESET}"
echo -e ""
echo -e "${BOLD}Output Files:${RESET}"
echo -e "  Events: ${CYAN}data/pending_create_token.jsonl${RESET}"
echo -e "  Logs: ${CYAN}logs/console_*.log${RESET}"
echo -e "${CYAN}═══════════════════════════════════════════════════════${RESET}"

echo -e "\n${YELLOW}${BOLD}Useful Commands:${RESET}"
echo -e "${CYAN}───────────────────────────────────────────────────────${RESET}"
echo -e "Attach to tmux session:"
echo -e "  ${GREEN}tmux attach -t pumptires${RESET}"
echo -e ""
echo -e "Switch between windows:"
echo -e "  ${GREEN}Ctrl+b, then 0${RESET} - Main mempool listener"
echo -e "  ${GREEN}Ctrl+b, then 1${RESET} - Console logs"
echo -e "  ${GREEN}Ctrl+b, then 2${RESET} - Event file monitor"
echo -e ""
echo -e "Detach from tmux:"
echo -e "  ${GREEN}Ctrl+b, then d${RESET}"
echo -e ""
echo -e "Kill session:"
echo -e "  ${GREEN}tmux kill-session -t pumptires${RESET}"
echo -e ""
echo -e "Monitor events in real-time:"
echo -e "  ${GREEN}tail -f data/pending_create_token.jsonl | jq .${RESET}"
echo -e ""
echo -e "Count detected events:"
echo -e "  ${GREEN}wc -l data/pending_create_token.jsonl${RESET}"
echo -e ""
echo -e "View latest event:"
echo -e "  ${GREEN}tail -1 data/pending_create_token.jsonl | jq .${RESET}"
echo -e ""
echo -e "Check system status:"
echo -e "  ${GREEN}ps aux | grep 'app.main'${RESET}"
echo -e ""
echo -e "Run with clean start (removes old events):"
echo -e "  ${GREEN}./scripts/deploy_dev.sh --clean${RESET}"
echo -e "${CYAN}───────────────────────────────────────────────────────${RESET}"

echo -e "\n${GREEN}${BOLD}Development environment is ready!${RESET}"
echo -e "${CYAN}The monitor is now listening to PulseChain mempool.${RESET}"
echo -e "${CYAN}Events will be logged to: ${YELLOW}data/pending_create_token.jsonl${RESET}\n"

