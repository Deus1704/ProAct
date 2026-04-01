#!/bin/bash

# Proactive Assistant Data Clearing Script
# This script clears all application data for a clean restart

echo "Clearing Proactive Assistant data..."

# Remove SQLite database file
if [ -f "proactive_assistant_app/assistant.db" ]; then
    echo "Removing database file: proactive_assistant_app/assistant.db"
    rm -f proactive_assistant_app/assistant.db
else
    echo "Database file not found (proactive_assistant_app/assistant.db)"
fi

# Remove browser session directory
if [ -d "proactive_assistant_app/browser_session" ]; then
    echo "Removing browser session directory: proactive_assistant_app/browser_session/"
    rm -rf proactive_assistant_app/browser_session/
else
    echo "Browser session directory not found (proactive_assistant_app/browser_session/)"
fi

# Remove application log file
if [ -f "app.log" ]; then
    echo "Removing log file: app.log"
    rm -f app.log
else
    echo "Log file not found (app.log)"
fi

echo "Data clearing complete!"
echo ""
echo "Next steps:"
echo "1. Run the application: python run.py"
echo "2. The application will recreate fresh data files on startup"
echo ""
echo "Note: This will remove all stored ride history, food patterns, and session data."