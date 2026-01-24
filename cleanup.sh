#!/bin/bash
# Cleanup unnecessary files from GPS tracker project

echo "🧹 Cleaning up unnecessary files..."
echo ""

# Remove old battery monitor (binary protocol, doesn't work with this device)
if [ -f "battery_monitor.py" ]; then
    echo "✓ Removing battery_monitor.py (old binary protocol version)"
    rm battery_monitor.py
fi

# Remove raw monitor (not needed anymore, we have device_info.py)
if [ -f "raw_monitor.py" ]; then
    echo "✓ Removing raw_monitor.py (functionality included in device_info.py)"
    rm raw_monitor.py
fi

# Remove captured data file from analysis
if [ -f "captured_data.bin" ]; then
    echo "✓ Removing captured_data.bin (temporary analysis file)"
    rm captured_data.bin
fi

# Remove Windows-only drivers (not needed on Linux)
if [ -d "USB Driver CH341SER" ]; then
    echo "✓ Removing USB Driver CH341SER/ (Windows drivers, not needed on Linux)"
    rm -rf "USB Driver CH341SER"
fi

# Remove Windows serial tool (not needed on Linux)
if [ -d "sscom5.13.1" ]; then
    echo "✓ Removing sscom5.13.1/ (Windows tool, not needed on Linux)"
    rm -rf "sscom5.13.1"
fi

# Remove Windows serial tool archive
if [ -f "sscom5.13.1.rar" ]; then
    echo "✓ Removing sscom5.13.1.rar (Windows tool archive)"
    rm sscom5.13.1.rar
fi

# Remove LibreOffice lock files
rm -f .~lock.*.docx# 2>/dev/null

echo ""
echo "✅ Cleanup complete!"
echo ""
echo "📦 Remaining files:"
echo "   - device_info.py          (Device status monitor)"
echo "   - test_connection.py      (Connection tester)"
echo "   - gps_config.py           (Configuration tool)"
echo "   - analyze_protocol.py     (Protocol debugging tool)"
echo "   - docs/                   (Documentation)"
echo "   - *.docx                  (Configuration reference documents)"
