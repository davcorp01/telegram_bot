import os
import telebot
from datetime import datetime

# Получаем токен из переменных окружения Render
TOKEN = os.environ.get('TELEGRAM_TOKEN', 'ваш_токен')
ADMIN_IDS = [int(x) for x in os.environ.get('ADMIN_IDS', '76657563').split(',')]

bot = telebot.TeleBot(TOKEN)

# Простая база в памяти
class WineDB:
    def __init__(self):
        self.users = {}
        self.products = [
            {"id": 1, "name": "Красное вино", "description": "Каберне"},
            {"id": 2, "name": "Белое вино", "description": "Шардоне"}
        ]
        self.balances = {}
        self.transactions = []
    
    def register_user(self, user_id, username, full_name):
        if user_id not in self.users:
            self.users[user_id] = {
                "id": user_id,
                "username": username,
                "full_name": full_name,
                "role": "admin" if user_id in ADMIN_IDS else "user",
                "created": datetime.now().strftime("%Y-%m-%d %H:%M")
            }
            # Начальные остатки
            self.balances[user_id] = {1: 50, 2: 50}
        return self.users[user_id]
    
    def get_balance(self, user_id):
        return self.balances.get(user_id)

db = WineDB()

# Команды бота
@bot.message_handler(commands=['start'])
def start(message):
    user = db.register_user(
        message.from_user.id,
        message.from_user.username or "",
        f"{message.from_user.first_name} {message.from_user.last_name or ''}".strip()
    )
    
    role = "👑 Администратор" if user['role'] == 'admin' else "👤 Пользователь"
    
    response = (
        f"✅ Добро пожаловать, {user['full_name']}!\n\n"
        f"{role}\n"
        f"Дата: {user['created']}\n\n"
        f"📦 Начальные остатки:\n"
        f"• Красное вино: 50 л\n"
        f"• Белое вино: 50 л\n\n"
        f"💡 Команды:\n"
        f"/balance - мои остатки\n"
        f"/help - справка"
    )
    
    bot.reply_to(message, response)

@bot.message_handler(commands=['balance'])
def balance(message):
    user_id = message.from_user.id
    balances = db.get_balance(user_id)
    
    if not balances:
        bot.reply_to(message, "❌ Сначала /start")
        return
    
    response = "📦 ВАШИ ОСТАТКИ:\n\n"
    total = 0
    
    for product in db.products:
        quantity = balances.get(product["id"], 0)
        response += f"• {product['name']}: {quantity} л\n"
        total += quantity
    
    response += f"\n📊 Всего: {total} л"
    bot.reply_to(message, response)

@bot.message_handler(commands=['ping'])
def ping(message):
    bot.reply_to(message, "🏓 PONG! Бот работает на Render.com")

@bot.message_handler(commands=['help'])
def help_cmd(message):
    response = (
        "🆘 СПРАВКА:\n\n"
        "/start - регистрация\n"
        "/balance - мои остатки\n"
        "/ping - проверка связи\n"
        "/help - эта справка"
    )
    
    if message.from_user.id in ADMIN_IDS:
        response += "\n\n👑 АДМИН:\n/admin - панель управления"
    
    bot.reply_to(message, response)

@bot.message_handler(commands=['admin'])
def admin_panel(message):
    if message.from_user.id not in ADMIN_IDS:
        bot.reply_to(message, "⛔ Нет прав")
        return
    
    total_wine = sum(sum(balances.values()) for balances in db.balances.values())
    
    response = (
        f"👑 АДМИН ПАНЕЛЬ\n\n"
        f"📊 Статистика:\n"
        f"• Пользователей: {len(db.users)}\n"
        f"• Операций: {len(db.transactions)}\n"
        f"• Всего вина: {total_wine} л\n\n"
        f"📍 Хостинг: Render.com"
    )
    
    bot.reply_to(message, response)

# Простой веб-сервер для Render (обязательно!)
from flask import Flask
app = Flask(__name__)

@app.route('/')
def home():
    return """
    <!DOCTYPE html>
    <html>
    <head>
        <title>🍷 Wine Bot</title>
        <style>
            body { font-family: Arial; padding: 40px; text-align: center; }
            .box { border: 2px solid #4CAF50; padding: 30px; max-width: 500px; margin: 0 auto; border-radius: 15px; }
        </style>
    </head>
    <body>
        <div class="box">
            <h1>🍷 Telegram Wine Bot</h1>
            <p>Бот работает на Render.com</p>
            <p>Напишите: @tonaum_bot</p>
        </div>
    </body>
    </html>
    """

@app.route('/health')
def health():
    return "OK"

# Запускаем бота в фоне при старте Flask
import threading
def run_bot():
    print("🤖 Starting Telegram bot...")
    bot.polling(none_stop=True)

if __name__ == '__main__':
    # Запускаем бота в отдельном потоке
    bot_thread = threading.Thread(target=run_bot, daemon=True)
    bot_thread.start()
    
    # Запускаем Flask сервер
    port = int(os.environ.get('PORT', 10000))
    app.run(host='0.0.0.0', port=port)