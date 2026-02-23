#!/bin/bash
echo "🎓 Запуск системаи идоракунии донишгоҳ (Supabase/PostgreSQL)..."
echo "=================================================="
echo "Database: Supabase PostgreSQL"
echo "Host: db.ktgcncmrmpsktsaspdkp.supabase.co"
echo "=================================================="
uvicorn main:app --host 0.0.0.0 --port 8001 --reload