#!/bin/bash
# Setup script for MQTT broker (Mosquitto)

echo "Setting up MQTT broker..."

# Check if mosquitto is installed
if ! command -v mosquitto &> /dev/null; then
    echo "Mosquitto not found. Installing..."
    
    # Detect OS
    if [[ "$OSTYPE" == "linux-gnu"* ]]; then
        sudo apt-get update
        sudo apt-get install -y mosquitto mosquitto-clients
    elif [[ "$OSTYPE" == "darwin"* ]]; then
        brew install mosquitto
    else
        echo "Please install Mosquitto manually: https://mosquitto.org/download/"
        exit 1
    fi
fi

# Create config directory
mkdir -p mqtt_config

# Create mosquitto config
cat > mqtt_config/mosquitto.conf <<EOF
# MQTT Broker Configuration for Gateway Testing
listener 1883
allow_anonymous true
persistence true
persistence_location ./mqtt_data
log_dest file ./mqtt_data/mosquitto.log
log_type all
EOF

# Create data directory
mkdir -p mqtt_data

echo "MQTT broker configured!"
echo ""
echo "To start the broker:"
echo "  mosquitto -c mqtt_config/mosquitto.conf"
echo ""
echo "To test publishing:"
echo "  mosquitto_pub -h localhost -t test/topic -m 'Hello MQTT'"
echo ""
echo "To test subscribing:"
echo "  mosquitto_sub -h localhost -t test/topic"


