import os
import sys
import telebot
from datetime import datetime
from flask import Flask
import threading
import pg8000
from pg8000.native import Connection, DatabaseError
import json

print("=" * 60, file=sys.stderr)
print("🤖 WINE BOT WITH SUPABASE (pg8000)", file=sys.stderr)
print(f"Python: {sys.version}", file=sys.stderr)
print("=" * 60, file=sys.stderr)

# Получаем настройки
TOKEN = os.environ['TELEGRAM_TOKEN']
ADMIN_IDS = [int(x) for x in os.environ['ADMIN_IDS'].split(',')]
DATABASE_URL = os.environ['SUPABASE_DB_URL']

bot = telebot.TeleBot(TOKEN)
app = Flask(__name__)

# ========== ПОДКЛЮЧЕНИЕ К SUPABASE (pg8000) ==========
def parse_db_url(url):
    """Разбираем URL подключения"""
    # Убираем префикс
    url = url.replace('postgresql://', '')
    
    # Разделяем логин:пароль@хост:порт/база
    if '@' in url:
        auth, rest = url.split('@', 1)
        user, password = auth.split(':', 1)
    else:
        user, password = 'postgres', ''
        rest = url
    
    if ':' in rest:
        host_port, database = rest.split('/', 1)
        if ':' in host_port:
            host, port = host_port.split(':', 1)
            port = int(port)
        else:
            host, port = host_port, 5432
    else:
        host, port = rest, 5432
        database = 'postgres'
    
    # Убираем параметры из database
    database = database.split('?')[0]
    
    return {
        'user': user,
        'password': password,
        'host': host,
        'port': port,
        'database': database
    }

def get_db_connection():
    """Создаем подключение через pg8000"""
    try:
        params = parse_db_url(DATABASE_URL)
        
        # Для отладки
        masked_params = params.copy()
        masked_params['password'] = '****'
        print(f"DB params: {masked_params}", file=sys.stderr)
        
        conn = Connection(**params)
        print("✅ Database connection established", file=sys.stderr)
        return conn
    except Exception as e:
        print(f"❌ Database connection error: {e}", file=sys.stderr)
        return None

# ========== ПРОСТЫЕ ФУНКЦИИ БАЗЫ ==========
def register_user(telegram_id, username, full_name):
    """Регистрация пользователя"""
    conn = get_db_connection()
    if not conn:
        return None
    
    try:
        # Проверяем есть ли пользователь
        result = conn.run("SELECT * FROM users WHERE telegram_id = :telegram_id", 
                         telegram_id=telegram_id)
        
        if result:
            # Пользователь уже есть
            user = {
                'id': result[0][0],
                'telegram_id': result[0][1],
                'username': result[0][2],
                'full_name': result[0][3],
                'role': result[0][4]
            }
            return user
        
        # Создаем нового
        role = 'admin' if telegram_id in ADMIN_IDS else 'user'
        conn.run("""
            INSERT INTO users (telegram_id, username, full_name, role) 
            VALUES (:telegram_id, :username, :full_name, :role)
            RETURNING id, telegram_id, username, full_name, role
        """, telegram_id=telegram_id, username=username, full_name=full_name, role=role)
        
        result = conn.run("SELECT * FROM users WHERE telegram_id = :telegram_id", 
                         telegram_id=telegram_id)
        
        if result:
            user = {
                'id': result[0][0],
                'telegram_id': result[0][1],
                'username': result[0][2],
                'full_name': result[0][3],
                'role': result[0][4]
            }
            
            # Создаем начальные остатки
            products = conn.run("SELECT id FROM products")
            for product in products:
                conn.run("""
                    INSERT INTO balances (user_id, product_id, quantity)
                    VALUES (:user_id, :product_id, 50)
                    ON CONFLICT (user_id, product_id) DO NOTHING
                """, user_id=user['id'], product_id=product[0])
            
            return user
        
    except Exception as e:
        print(f"❌ Error registering user: {e}", file=sys.stderr)
        return None
    finally:
        try:
            conn.close()
        except:
            pass

def get_user_balance(telegram_id):
    """Получить остатки"""
    conn = get_db_connection()
    if not conn:
        return []
    
    try:
        result = conn.run("""
            SELECT p.name, b.quantity 
            FROM balances b
            JOIN products p ON b.product_id = p.id
            JOIN users u ON b.user_id = u.id
            WHERE u.telegram_id = :telegram_id
            ORDER BY p.name
        """, telegram_id=telegram_id)
        
        balances = []
        for row in result:
            balances.append({'name': row[0], 'quantity': row[1]})
        
        return balances
        
    except Exception as e:
        print(f"❌ Error getting balance: {e}", file=sys.stderr)
        return []
    finally:
        try:
            conn.close()
        except:
            pass

# ========== КОМАНДЫ БОТА ==========
@bot.message_handler(commands=['start'])
def start(message):
    user = register_user(
        message.from_user.id,
        message.from_user.username or "",
        f"{message.from_user.first_name} {message.from_user.last_name or ''}".strip()
    )
    
    if user:
        role = "👑 Администратор" if user['role'] == 'admin' else "👤 Пользователь"
        response = f"✅ Добро пожаловать, {user['full_name']}!\n{role}\nИспользуйте /balance"
    else:
        response = "❌ Ошибка. Попробуйте позже."
    
    bot.reply_to(message, response)

@bot.message_handler(commands=['balance'])
def balance(message):
    balances = get_user_balance(message.from_user.id)
    
    if not balances:
        bot.reply_to(message, "❌ Нет данных. Сначала /start")
        return
    
    response = "📦 ВАШИ ОСТАТКИ:\n\n"
    total = 0
    
    for item in balances:
        response += f"• {item['name']}: {item['quantity']} л\n"
        total += item['quantity']
    
    response += f"\n📊 Всего: {total} л"
    bot.reply_to(message, response)

@bot.message_handler(commands=['ping'])
def ping(message):
    bot.reply_to(message, "🏓 PONG! Бот с Supabase работает!")

# ... остальной код (Flask, запуск)

def run_bot():
    print("🤖 Starting bot...", file=sys.stderr)
    bot.polling(none_stop=True)

if __name__ == '__main__':
    # Тест подключения
    print("🔍 Testing database...", file=sys.stderr)
    conn = get_db_connection()
    if conn:
        try:
            result = conn.run("SELECT version()")
            print(f"✅ Database: {result[0][0][:50]}...", file=sys.stderr)
            conn.close()
        except:
            pass
    
    # Запуск
    bot_thread = threading.Thread(target=run_bot, daemon=True)
    bot_thread.start()
    
    port = int(os.environ.get('PORT', 10000))
    app.run(host='0.0.0.0', port=port)
