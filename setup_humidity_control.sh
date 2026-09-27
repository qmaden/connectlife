#!/bin/bash
#
# Setup script for humidity control automation.
#
# This script helps you:
# 1. Set up environment variables
# 2. Test the humidity controller
# 3. Install the cron job
#

SCRIPT_DIR="/home/maden/connectlife"
VENV_PATH="$SCRIPT_DIR/venv"
CONTROL_SCRIPT="$SCRIPT_DIR/humidity_control.py"

echo "=== ConnectLife Humidity Control Setup ==="
echo

# Check if virtual environment exists
if [ ! -d "$VENV_PATH" ]; then
    echo "Error: Virtual environment not found at $VENV_PATH"
    echo "Please run: python3 -m venv venv && source venv/bin/activate && pip install -e ."
    exit 1
fi

# Function to create environment file
create_env_file() {
    echo "Creating environment file..."
    read -p "Enter your ConnectLife username: " username
    read -s -p "Enter your ConnectLife password: " password
    echo
    read -p "Enter your device type code (default '007' for dehumidifier, press enter to use): " device_type
    device_type=${device_type:-"007"}

    cat > "$SCRIPT_DIR/.env" << EOF
# ConnectLife credentials for humidity control
export CONNECTLIFE_USERNAME="$username"
export CONNECTLIFE_PASSWORD="$password"
export DEVICE_TYPE_CODE="$device_type"
EOF

    chmod 600 "$SCRIPT_DIR/.env"
    echo "Environment file created at $SCRIPT_DIR/.env (permissions set to owner-only)"
    echo "Device type code set to: $device_type"
    echo
}

# Function to test the controller
test_controller() {
    echo "Testing humidity controller..."
    echo "Loading environment..."

    if [ -f "$SCRIPT_DIR/.env" ]; then
        source "$SCRIPT_DIR/.env"
    else
        echo "No .env file found. Please create one first."
        return 1
    fi

    cd "$SCRIPT_DIR"
    source "$VENV_PATH/bin/activate"
    python3 humidity_control.py

    echo "Test completed. Check the output above for any errors."
    echo
}

# Function to install cron job
install_cron() {
    echo "Installing cron job..."

    # Create wrapper script that sources environment
    cat > "$SCRIPT_DIR/run_humidity_control.sh" << 'RUNEOF'
#!/bin/bash
set -euo pipefail

cd "/home/maden/connectlife"
source "/home/maden/connectlife/.env"
source "/home/maden/connectlife/venv/bin/activate"
python3 humidity_control.py
RUNEOF

    chmod +x "$SCRIPT_DIR/run_humidity_control.sh"

    # Add to cron (runs every 10 minutes)
    CRON_JOB="*/10 * * * * $SCRIPT_DIR/run_humidity_control.sh"

    # Check if cron job already exists
    if crontab -l 2>/dev/null | grep -q "run_humidity_control.sh"; then
        echo "Cron job already exists."
    else
        # Add cron job
        (crontab -l 2>/dev/null; echo "$CRON_JOB") | crontab -
        echo "Cron job installed: runs every 10 minutes"
        echo "To view: crontab -l"
        echo "To remove: crontab -e (then delete the line)"
    fi

    echo "Wrapper script created at: $SCRIPT_DIR/run_humidity_control.sh"
    echo
}

# Function to view logs
view_logs() {
    echo "Recent humidity control logs:"
    echo "============================="
    tail -20 "$SCRIPT_DIR/humidity_control.log" 2>/dev/null || echo "No logs found yet."
    echo
}

# Menu
while true; do
    echo "Choose an option:"
    echo "1. Create environment file (.env)"
    echo "2. Test humidity controller"
    echo "3. Install cron job (runs every 10 minutes)"
    echo "4. View recent logs"
    echo "5. Exit"
    echo
    read -p "Enter choice (1-5): " choice

    case $choice in
        1)
            create_env_file
            ;;
        2)
            test_controller
            ;;
        3)
            install_cron
            ;;
        4)
            view_logs
            ;;
        5)
            echo "Setup completed!"
            exit 0
            ;;
        *)
            echo "Invalid choice. Please enter 1-5."
            ;;
    esac
done
