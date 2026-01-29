import psycopg2

DATABASE_URL = "postgresql://postgres.wosihzgrfecxxjfaanxp:ТВОЙ_ПАРОЛЬ@aws-1-eu-west-1.pooler.supabase.com:5432/postgres"

try:
    conn = psycopg2.connect(DATABASE_URL, sslmode='require')
    print("✅ Подключение успешно!")
    
    # Проверяем таблицы
    with conn.cursor() as cur:
        cur.execute("SELECT table_name FROM information_schema.tables WHERE table_schema = 'public'")
        tables = cur.fetchall()
        print(f"📊 Найдено таблиц: {len(tables)}")
        for table in tables:
            print(f"  - {table[0]}")
    
    conn.close()
    
except Exception as e:
    print(f"❌ Ошибка: {e}")
