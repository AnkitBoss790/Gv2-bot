#!/bin/bash
# PowerDev VPS Discord Bot - One Click Installer
# Works on Debian / Ubuntu (Proxmox, VPS, or bare metal)
# Tested with Ubuntu 22.04 / Debian 12

set -e

echo -e "\n🧠 Checking system requirements..."
if [[ $EUID -ne 0 ]]; then
  echo "❌ Please run as root"
  exit 1
fi

# ====== STEP 1: Update & Install Dependencies ======
echo -e "\n🔧 Updating system and installing dependencies..."
apt update -y
apt install -y python3 python3-pip python3-venv qemu-kvm qemu-utils tmate curl wget git

# ====== STEP 2: Setup bot directory ======
echo -e "\n📁 Creating working directory..."
BOT_DIR="/root/powerdev_vpsbot"
mkdir -p $BOT_DIR
cd $BOT_DIR

# ====== STEP 3: Python environment ======
echo -e "\n🐍 Setting up Python virtual environment..."
python3 -m venv venv
source venv/bin/activate
pip install --upgrade pip
pip install discord.py==2.3.2

# ====== STEP 4: Download main.py ======
echo -e "\n📥 Downloading bot source code..."
cat > main.py << 'EOF'
### >>> Python bot code starts here (paste full code from previous message) <<<
EOF

# ====== STEP 5: Ask for Bot Token and Owner ID ======
echo -e "\n🔑 Enter your Discord Bot Token:"
read -r BOT_TOKEN
echo -e "👑 Enter your Discord Owner ID (your numeric Discord ID):"
read -r OWNER_ID

# ====== STEP 6: Create Environment Variables File ======
echo -e "\n⚙️ Saving environment variables..."
cat > .env <<EOF
export BOT_TOKEN="$BOT_TOKEN"
export OWNER_ID="$OWNER_ID"
EOF

# ====== STEP 7: Systemd Service ======
echo -e "\n🧩 Creating systemd service..."
SERVICE_FILE="/etc/systemd/system/powerdev_vpsbot.service"

cat > $SERVICE_FILE <<EOF
[Unit]
Description=PowerDev Discord VPS Bot
After=network.target

[Service]
Type=simple
User=root
WorkingDirectory=$BOT_DIR
EnvironmentFile=$BOT_DIR/.env
ExecStart=$BOT_DIR/venv/bin/python3 $BOT_DIR/main.py
Restart=always

[Install]
WantedBy=multi-user.target
EOF

# ====== STEP 8: Enable & Start Service ======
echo -e "\n🚀 Starting bot service..."
systemctl daemon-reexec
systemctl daemon-reload
systemctl enable powerdev_vpsbot
systemctl restart powerdev_vpsbot

echo -e "\n✅ Installation complete!"
echo "📁 Bot directory: $BOT_DIR"
echo "⚙️ To check status: systemctl status powerdev_vpsbot"
echo "🧩 To view logs: journalctl -u powerdev_vpsbot -f"
echo "🔄 To restart bot: systemctl restart powerdev_vpsbot"
echo "✅ The bot should now be online on your Discord server!"
