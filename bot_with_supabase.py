import os
import sys
import telebot
from datetime import datetime
from flask import Flask
import threading
import psycopg2
from psycopg2.extras import RealDictCursor

print("=" * 60, file=sys.stderr)
print("🔍 Testing Supabase connection...", file=sys.stderr)

# Маскируем пароль в логах для безопасности
if 'SUPABASE_DB_URL' in os.environ:
    db_url = os.environ['SUPABASE_DB_URL']
    # Маскируем пароль
    import re
    masked_url = re.sub(r':([^@]+)@', ':****@', db_url)
    print(f"Database URL: {masked_url}", file=sys.stderr)
    
    try:
        conn = psycopg2.connect(db_url, sslmode='require')
        with conn.cursor() as cur:
            cur.execute("SELECT version();")
            version = cur.fetchone()
            print(f"✅ Supabase connected: {version[0][:50]}...", file=sys.stderr)
        conn.close()
    except Exception as e:
        print(f"❌ Database connection failed: {e}", file=sys.stderr)
else:
    print("❌ SUPABASE_DB_URL not found in environment", file=sys.stderr)

print("=" * 60, file=sys.stderr)
print("🤖 WINE BOT WITH SUPABASE", file=sys.stderr)
print("=" * 60, file=sys.stderr)

# Получаем настройки из переменных окружения Render
TOKEN = os.environ['TELEGRAM_TOKEN']
ADMIN_IDS = [int(x) for x in os.environ['ADMIN_IDS'].split(',')]
DATABASE_URL = os.environ['SUPABASE_DB_URL']  # Добавь эту переменную в Render!

bot = telebot.TeleBot(TOKEN)
app = Flask(__name__)

# ========== ПОДКЛЮЧЕНИЕ К SUPABASE ==========
def get_db_connection():
    """Создаем подключение к Supabase"""
    try:
        conn = psycopg2.connect(DATABASE_URL, sslmode='require')
        return conn
    except Exception as e:
        print(f"❌ Database connection error: {e}", file=sys.stderr)
        return None

# ========== ФУНКЦИИ РАБОТЫ С БАЗОЙ ==========
def register_user(telegram_id, username, full_name):
    """Регистрация пользователя в базе"""
    conn = get_db_connection()
    if not conn:
        return None
    
    try:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            # Проверяем, есть ли уже пользователь
            cur.execute("SELECT * FROM users WHERE telegram_id = %s", (telegram_id,))
            user = cur.fetchone()
            
            if user:
                return user
            
            # Создаем нового пользователя
            role = 'admin' if telegram_id in ADMIN_IDS else 'user'
            cur.execute("""
                INSERT INTO users (telegram_id, username, full_name, role) 
                VALUES (%s, %s, %s, %s)
                RETURNING *
            """, (telegram_id, username, full_name, role))
            
            user = cur.fetchone()
            conn.commit()
            
            # Создаем начальные остатки
            cur.execute("SELECT id FROM products")
            products = cur.fetchall()
            
            for product in products:
                cur.execute("""
                    INSERT INTO balances (user_id, product_id, quantity)
                    VALUES (%s, %s, 50)
                    ON CONFLICT (user_id, product_id) DO NOTHING
                """, (user['id'], product['id']))
            
            conn.commit()
            return user
            
    except Exception as e:
        print(f"❌ Error registering user: {e}", file=sys.stderr)
        conn.rollback()
        return None
    finally:
        conn.close()

def get_user_balance(telegram_id):
    """Получить остатки пользователя"""
    conn = get_db_connection()
    if not conn:
        return []
    
    try:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute("""
                SELECT p.name, b.quantity 
                FROM balances b
                JOIN products p ON b.product_id = p.id
                JOIN users u ON b.user_id = u.id
                WHERE u.telegram_id = %s
                ORDER BY p.name
            """, (telegram_id,))
            return cur.fetchall()
    except Exception as e:
        print(f"❌ Error getting balance: {e}", file=sys.stderr)
        return []
    finally:
        conn.close()

def spend_wine(telegram_id, product_name, quantity):
    """Списать вино"""
    conn = get_db_connection()
    if not conn:
        return False, "Ошибка подключения к базе"
    
    try:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            # 1. Находим пользователя и товар
            cur.execute("SELECT id FROM users WHERE telegram_id = %s", (telegram_id,))
            user = cur.fetchone()
            if not user:
                return False, "Пользователь не найден"
            
            cur.execute("SELECT id FROM products WHERE name = %s", (product_name,))
            product = cur.fetchone()
            if not product:
                return False, "Товар не найден"
            
            # 2. Проверяем остаток
            cur.execute("""
                SELECT quantity FROM balances 
                WHERE user_id = %s AND product_id = %s
            """, (user['id'], product['id']))
            balance = cur.fetchone()
            
            if not balance or balance['quantity'] < quantity:
                return False, f"Недостаточно. Остаток: {balance['quantity'] if balance else 0} л"
            
            # 3. Списание
            cur.execute("""
                UPDATE balances 
                SET quantity = quantity - %s
                WHERE user_id = %s AND product_id = %s
            """, (quantity, user['id'], product['id']))
            
            # 4. Запись операции
            cur.execute("""
                INSERT INTO transactions (user_id, product_id, type, quantity, notes)
                VALUES (%s, %s, 'out', %s, 'Списание через бота')
            """, (user['id'], product['id'], quantity))
            
            conn.commit()
            return True, "Успешно списано"
            
    except Exception as e:
        conn.rollback()
        print(f"❌ Error spending wine: {e}", file=sys.stderr)
        return False, f"Ошибка базы: {e}"
    finally:
        conn.close()

# ========== КОМАНДЫ БОТА ==========
@bot.message_handler(commands=['start'])
def start(message):
    """Регистрация в системе"""
    user = register_user(
        message.from_user.id,
        message.from_user.username or "",
        f"{message.from_user.first_name} {message.from_user.last_name or ''}".strip()
    )
    
    if user:
        role = "👑 Администратор" if user['role'] == 'admin' else "👤 Пользователь"
        response = f"✅ Добро пожаловать, {user['full_name']}!\n{role}\nИспользуйте /balance"
    else:
        response = "❌ Ошибка регистрации. Попробуйте позже."
    
    bot.reply_to(message, response)

@bot.message_handler(commands=['balance'])
def balance(message):
    """Показать остатки"""
    balances = get_user_balance(message.from_user.id)
    
    if not balances:
        bot.reply_to(message, "❌ Сначала /start или нет остатков")
        return
    
    response = "📦 ВАШИ ОСТАТКИ:\n\n"
    total = 0
    
    for item in balances:
        response += f"• {item['name']}: {item['quantity']} л\n"
        total += item['quantity']
    
    response += f"\n📊 Всего: {total} л"
    bot.reply_to(message, response)

@bot.message_handler(commands=['spend'])
def spend_command(message):
    """Начать списание"""
    # Здесь будет клавиатура с товарами
    # Пока просто заглушка
    bot.reply_to(message, "Функция списания будет добавлена. Используйте /balance")

@bot.message_handler(commands=['ping'])
def ping(message):
    bot.reply_to(message, "🏓 PONG! Бот работает с Supabase!")

@bot.message_handler(commands=['admin'])
def admin_panel(message):
    if message.from_user.id not in ADMIN_IDS:
        bot.reply_to(message, "⛔ Нет прав")
        return
    
    conn = get_db_connection()
    if not conn:
        bot.reply_to(message, "❌ База данных недоступна")
        return
    
    try:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute("SELECT COUNT(*) as count FROM users")
            users = cur.fetchone()['count']
            
            cur.execute("SELECT COUNT(*) as count FROM transactions")
            transactions = cur.fetchone()['count']
            
            cur.execute("SELECT SUM(quantity) as total FROM balances")
            total_wine = cur.fetchone()['total'] or 0
            
        response = (
            f"👑 АДМИН ПАНЕЛЬ\n\n"
            f"📊 Статистика:\n"
            f"• 👥 Пользователей: {users}\n"
            f"• 📝 Операций: {transactions}\n"
            f"• 🍷 Всего вина: {total_wine} л\n\n"
            f"📍 База: Supabase"
        )
        
    except Exception as e:
        response = f"❌ Ошибка: {e}"
    finally:
        conn.close()
    
    bot.reply_to(message, response)

# ... остальной код (Flask, запуск) такой же как в предыдущем боте ...

# ========== ЗАПУСК ==========
def run_bot():
    print("🤖 Starting bot with Supabase...", file=sys.stderr)
    bot.polling(none_stop=True)

if __name__ == '__main__':
    # Проверяем подключение к базе
    print("🔍 Testing database connection...", file=sys.stderr)
    conn = get_db_connection()
    if conn:
        print("✅ Supabase connected successfully", file=sys.stderr)
        conn.close()
    else:
        print("❌ Cannot connect to Supabase", file=sys.stderr)
        print("Check SUPABASE_DB_URL in Render environment variables", file=sys.stderr)
    
    # Запускаем бота
    bot_thread = threading.Thread(target=run_bot, daemon=True)
    bot_thread.start()
    
    # Запускаем Flask
    port = int(os.environ.get('PORT', 10000))
    app.run(host='0.0.0.0', port=port)
