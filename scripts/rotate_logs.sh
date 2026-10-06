#!/bin/bash
# Log rotation script for gateway logs
# Can be run via cron: 0 0 * * * /path/to/rotate_logs.sh

set -e

LOG_DIR="${LOG_DIR:-./logs}"
RETENTION_DAYS="${RETENTION_DAYS:-30}"
MAX_SIZE="${MAX_SIZE:-100M}"

echo "📦 Rotating logs in $LOG_DIR..."

# Create log directory if it doesn't exist
mkdir -p "$LOG_DIR"

# Rotate logs using logrotate if available, otherwise manual rotation
if command -v logrotate &> /dev/null; then
    echo "✅ Using logrotate..."
    
    # Create logrotate config
    cat > /tmp/gateway_logrotate.conf <<EOF
$LOG_DIR/*.log {
    daily
    rotate $RETENTION_DAYS
    compress
    delaycompress
    missingok
    notifempty
    create 0644 root root
    maxsize $MAX_SIZE
    dateext
    dateformat -%Y%m%d
    sharedscripts
    postrotate
        # Reload gateway if needed (uncomment if using systemd)
        # systemctl reload mcp-gateway || true
    endscript
}
EOF
    
    logrotate -f /tmp/gateway_logrotate.conf
    rm /tmp/gateway_logrotate.conf
else
    echo "⚠️  logrotate not found, using manual rotation..."
    
    # Manual rotation
    for logfile in "$LOG_DIR"/*.log; do
        if [ -f "$logfile" ]; then
            # Check file size (in MB)
            size=$(du -m "$logfile" | cut -f1)
            max_size_mb=$(echo "$MAX_SIZE" | sed 's/M//')
            
            if [ "$size" -gt "$max_size_mb" ]; then
                timestamp=$(date +%Y%m%d_%H%M%S)
                mv "$logfile" "${logfile}.${timestamp}"
                echo "   Rotated: $logfile -> ${logfile}.${timestamp}"
            fi
        fi
    done
    
    # Remove old logs
    find "$LOG_DIR" -name "*.log.*" -type f -mtime +$RETENTION_DAYS -delete
    echo "   Removed logs older than $RETENTION_DAYS days"
fi

echo "✅ Log rotation complete!"


