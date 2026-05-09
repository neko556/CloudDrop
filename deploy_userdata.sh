#!/bin/bash
# ═══════════════════════════════════════════════════════════════════════════
# CloudDrop — EC2 User-Data Bootstrap Script
# This runs automatically when an EC2 instance launches in the ASG.
# ═══════════════════════════════════════════════════════════════════════════

set -euo pipefail
exec > >(tee /var/log/clouddrop-setup.log) 2>&1

echo "═══ CloudDrop Setup Starting ═══"

# ── 1. System Update ─────────────────────────────────────────────────────
yum update -y
yum install -y python3.11 python3.11-pip git

# ── 2. Create app directory ──────────────────────────────────────────────
APP_DIR="/opt/clouddrop"
mkdir -p "$APP_DIR"

# ── 3. Download app code from S3 (pre-uploaded as a zip) ─────────────────
# Upload your code first:  aws s3 cp clouddrop-app.zip s3://clouddrop-deploy-556684850112/
aws s3 cp s3://clouddrop-deploy-556684850112/clouddrop-app.zip /tmp/clouddrop-app.zip
cd "$APP_DIR"
unzip -o /tmp/clouddrop-app.zip -d "$APP_DIR" || true

# Fix: If the zip was created by right-clicking the folder, it extracts into a nested "CloudDrop" folder.
# We need to move the contents up one directory level.
if [ -d "$APP_DIR/CloudDrop" ]; then
    mv $APP_DIR/CloudDrop/* $APP_DIR/
    rm -rf $APP_DIR/CloudDrop
fi

# ── 4. Install Python dependencies ──────────────────────────────────────
pip3.11 install -r "$APP_DIR/backend/requirements.txt"

# ── 5. Write environment config ─────────────────────────────────────────
cat > "$APP_DIR/.env" << 'EOF'
AWS_REGION=ap-south-1
S3_BUCKET=clouddrop-files-556684850112
KMS_KEY_ID=alias/clouddrop-key
REDIS_HOST=master.clouddrop-redis.ezyqzx.aps1.cache.amazonaws.com
REDIS_PORT=6379
REDIS_USE_TLS=true
MAX_FILE_SIZE_MB=50
MAX_DOWNLOADS=5
BASE_URL=https://d2vkvqf4pmsk1k.cloudfront.net
COGNITO_CLIENT_ID=qgrs9s76h2f8cvili0vajhb1t
COGNITO_CLIENT_SECRET=19jia4ukfrq0e16peofknb8acrevkstetnn3b0bosg87n62v07ii
COGNITO_DOMAIN=ap-south-1bnqedrrir.auth.ap-south-1.amazoncognito.com
COGNITO_USER_POOL_ID=ap-south-1_bNqedrrir
DATABASE_URL=postgresql://clouddrop_admin:clouddrop123@clouddrop-db.cnuk6w4yq4ob.ap-south-1.rds.amazonaws.com:5432/clouddrop
DATABASE_READ_URL=postgresql://clouddrop_admin:clouddrop123@clouddrop-db-replica.cnuk6w4yq4ob.ap-south-1.rds.amazonaws.com:5432/clouddrop
DB_SYNC_INTERVAL_SECONDS=10
EOF

# Strip Windows carriage returns (\r) just in case!
sed -i 's/\r//g' "$APP_DIR/.env"

# ── 6. Create systemd service ───────────────────────────────────────────
cat > /etc/systemd/system/clouddrop.service << EOF
[Unit]
Description=CloudDrop FastAPI Application
After=network.target

[Service]
Type=simple
User=ec2-user
WorkingDirectory=$APP_DIR/backend
EnvironmentFile=$APP_DIR/.env
ExecStart=/usr/bin/python3.11 -m uvicorn main:app --host 0.0.0.0 --port 8000 --workers 2
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
EOF

# ── 7. Start the service ────────────────────────────────────────────────
systemctl daemon-reload
systemctl enable clouddrop
systemctl start clouddrop

echo "═══ CloudDrop Setup Complete ═══"
echo "App running on port 8000"
