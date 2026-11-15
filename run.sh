#!/bin/zsh
# 啟動下注網站（只聽本機，對外由 ngrok 轉發）
cd "$(dirname "$0")"
[ -d .venv ] || { python3 -m venv .venv && .venv/bin/pip install -q -r requirements.txt; }
.venv/bin/python -c "import app" >/dev/null   # 第一次執行時產生管理員密碼
echo "管理員密碼：$(cat instance/admin_password.txt 2>/dev/null || echo '（使用 ADMIN_PASSWORD 環境變數）')"
echo "本機後台：http://127.0.0.1:8000/admin"
exec .venv/bin/uvicorn app:app --host 0.0.0.0 --port 8000 --proxy-headers
