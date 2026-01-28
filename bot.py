import os
import telebot
from datetime import datetime
from flask import Flask
import threading

# ========== БЕЗОПАСНОЕ ПОЛУЧЕНИЕ ТОКЕНОВ ==========
# Получаем токен ТОЛЬКО из переменных окружения
try:
    TOKEN = os.environ['TELEGRAM_TOKEN']
    ADMIN_IDS = [int(x) for x in os.environ['ADMIN_IDS'].split(',')]
except KeyError as e:
    print(f"❌ ОШИБКА: Не найдена переменная окружения: {e}")
    print("Установите на Render:")
    print("1. TELEGRAM_TOKEN = ваш_токен")
    print("2. ADMIN_IDS = 76657563")
    raise SystemExit(1)

bot = telebot.TeleBot(TOKEN)
app = Flask(__name__)


# ========== БАЗА ДАННЫХ В ПАМЯТИ ==========
db = {
    'users': {},
    'products': [
        {"id": 1, "name": "Красное вино", "description": "Каберне"},
        {"id": 2, "name": "Белое вино", "description": "Шардоне"}
    ],
    'balances': {},
    'transactions': []
}

# ========== КОМАНДЫ БОТА ==========
@bot.message_handler(commands=['start'])
def start(message):
    """Регистрация пользователя"""
    user_id = message.from_user.id
    username = message.from_user.username or ""
    full_name = f"{message.from_user.first_name} {message.from_user.last_name or ''}".strip()
    
    if user_id not in db['users']:
        db['users'][user_id] = {
            "id": user_id,
            "username": username,
            "full_name": full_name,
            "role": "admin" if user_id in ADMIN_IDS else "user",
            "created": datetime.now().strftime("%Y-%m-%d %H:%M")
        }
        # Начальные остатки
        db['balances'][user_id] = {1: 50, 2: 50}
    
    user = db['users'][user_id]
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
        f"/spend - списать вино\n"
        f"/help - справка"
    )
    
    bot.reply_to(message, response)

@bot.message_handler(commands=['balance'])
def balance(message):
    """Показать остатки пользователя"""
    user_id = message.from_user.id
    
    if user_id not in db['balances']:
        bot.reply_to(message, "❌ Сначала зарегистрируйтесь через /start")
        return
    
    response = "📦 ВАШИ ОСТАТКИ:\n\n"
    total = 0
    
    for product in db['products']:
        quantity = db['balances'][user_id].get(product["id"], 0)
        response += f"• {product['name']}: {quantity} л\n"
        total += quantity
    
    response += f"\n📊 Всего: {total} л"
    bot.reply_to(message, response)

@bot.message_handler(commands=['spend'])
def spend(message):
    """Начать процесс списания вина"""
    user_id = message.from_user.id
    
    # Проверяем регистрацию
    if user_id not in db['users']:
        bot.reply_to(message, "❌ Сначала зарегистрируйтесь через /start")
        return
    
    # Проверяем есть ли что списывать
    has_products = False
    for product in db['products']:
        current_qty = db['balances'][user_id].get(product["id"], 0)
        if current_qty > 0:
            has_products = True
            break
    
    if not has_products:
        bot.reply_to(message, "📦 Все товары с нулевым остатком")
        return
    
    # Создаем клавиатуру с товарами
    markup = telebot.types.ReplyKeyboardMarkup(row_width=1, resize_keyboard=True)
    
    for product in db['products']:
        current_qty = db['balances'][user_id].get(product["id"], 0)
        if current_qty > 0:
            markup.add(telebot.types.KeyboardButton(f"Списать {product['name']}"))
    
    markup.add(telebot.types.KeyboardButton("❌ Отмена"))
    
    bot.reply_to(message, "🏷️ Выберите товар для списания:", reply_markup=markup)
    bot.register_next_step_handler(message, process_product_selection)

def process_product_selection(message):
    """Обработка выбора товара"""
    if message.text == "❌ Отмена":
        bot.reply_to(message, "❎ Отменено", reply_markup=telebot.types.ReplyKeyboardRemove())
        return
    
    user_id = message.from_user.id
    
    # Ищем выбранный товар
    selected_product = None
    for product in db['products']:
        if f"Списать {product['name']}" in message.text:
            selected_product = product
            break
    
    if not selected_product:
        bot.reply_to(message, "❌ Товар не найден", reply_markup=telebot.types.ReplyKeyboardRemove())
        return
    
    # Сохраняем выбор и просим количество
    bot.send_message(message.chat.id,
                    f"📝 Выбран: {selected_product['name']}\n"
                    f"💰 Введите количество для списания (в литрах):",
                    reply_markup=telebot.types.ReplyKeyboardRemove())
    
    bot.register_next_step_handler(message,
                                 lambda msg, prod=selected_product: process_quantity(msg, prod, user_id))

def process_quantity(message, product, user_id):
    """Обработка ввода количества"""
    try:
        quantity = float(message.text)
        
        if quantity <= 0:
            bot.reply_to(message, "❌ Введите положительное число")
            return
        
        # Получаем текущий остаток
        current_balance = db['balances'][user_id].get(product["id"], 0)
        
        if current_balance >= quantity:
            # Списание
            db['balances'][user_id][product["id"]] = current_balance - quantity
            
            # Запись операции
            db['transactions'].append({
                'user_id': user_id,
                'product': product['name'],
                'quantity': quantity,
                'type': 'out',
                'date': datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            })
            
            bot.reply_to(message,
                        f"✅ УСПЕШНО СПИСАНО!\n\n"
                        f"📦 Товар: {product['name']}\n"
                        f"📏 Количество: {quantity} л\n"
                        f"💰 Новый остаток: {db['balances'][user_id][product['id']]} л")
        else:
            bot.reply_to(message,
                        f"❌ НЕДОСТАТОЧНО!\n\n"
                        f"📦 Товар: {product['name']}\n"
                        f"📏 Требуется: {quantity} л\n"
                        f"💰 Доступно: {current_balance} л")
            
    except ValueError:
        bot.reply_to(message, "❌ Введите число (например: 2.5)")

@bot.message_handler(commands=['ping'])
def ping(message):
    """Проверка связи"""
    bot.reply_to(message, "🏓 PONG! Бот работает на Render.com")

@bot.message_handler(commands=['help'])
def help_cmd(message):
    """Справка по командам"""
    response = (
        "🆘 СПРАВКА ПО КОМАНДАМ:\n\n"
        "/start - регистрация и начало работы\n"
        "/balance - мои остатки\n"
        "/spend - списать вино\n"
        "/ping - проверка связи\n"
        "/help - эта справка"
    )
    
    if message.from_user.id in ADMIN_IDS:
        response += "\n\n👑 АДМИН КОМАНДЫ:\n/admin - панель управления"
    
    bot.reply_to(message, response)

@bot.message_handler(commands=['admin'])
def admin_panel(message):
    """Панель администратора"""
    if message.from_user.id not in ADMIN_IDS:
        bot.reply_to(message, "⛔ Нет прав доступа")
        return
    
    # Статистика
    total_users = len(db['users'])
    total_transactions = len(db['transactions'])
    total_wine = sum(sum(user_balances.values()) for user_balances in db['balances'].values())
    
    response = (
        f"👑 ПАНЕЛЬ АДМИНИСТРАТОРА\n\n"
        f"📊 СТАТИСТИКА:\n"
        f"• Пользователей: {total_users}\n"
        f"• Транзакций: {total_transactions}\n"
        f"• Всего вина на складе: {total_wine} л\n\n"
        f"📍 Хостинг: Render.com\n"
        f"🕒 Время: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}"
    )
    
    bot.reply_to(message, response)

@bot.message_handler(commands=['products'])
def list_products(message):
    """Список товаров"""
    response = "📋 СПИСОК ТОВАРОВ:\n\n"
    
    for product in db['products']:
        response += f"• {product['name']}"
        if product['description']:
            response += f" - {product['description']}"
        response += "\n"
    
    bot.reply_to(message, response)

# ========== ВЕБ-СЕРВЕР ДЛЯ RENDER ==========
@app.route('/')
def home():
    return """
    <!DOCTYPE html>
    <html>
    <head>
        <title>🍷 Wine Telegram Bot</title>
        <style>
            body {
                font-family: Arial, sans-serif;
                padding: 40px;
                text-align: center;
                background-color: #f5f5f5;
            }
            .container {
                max-width: 600px;
                margin: 0 auto;
                background: white;
                padding: 30px;
                border-radius: 15px;
                box-shadow: 0 2px 10px rgba(0,0,0,0.1);
            }
            h1 {
                color: #4CAF50;
            }
            .status {
                color: #4CAF50;
                font-weight: bold;
            }
            .commands {
                text-align: left;
                margin: 20px 0;
                padding: 15px;
                background: #f9f9f9;
                border-radius: 10px;
            }
        </style>
    </head>
    <body>
        <div class="container">
            <h1>🍷 Telegram Wine Bot</h1>
            <p>Система учета складских остатков вина</p>
            <p>Статус: <span class="status">✅ Активен</span></p>
            
            <div class="commands">
                <h3>📋 Основные команды:</h3>
                <p><code>/start</code> - регистрация</p>
                <p><code>/balance</code> - мои остатки</p>
                <p><code>/spend</code> - списать вино</p>
                <p><code>/products</code> - список товаров</p>
                <p><code>/help</code> - справка по командам</p>
            </div>
            
            <p>Бот: <a href="https://t.me/tonaum_bot">@tonaum_bot</a></p>
            <p>Хостинг: <strong>Render.com</strong></p>
        </div>
    </body>
    </html>
    """

@app.route('/health')
def health():
    return "OK"

@app.route('/test')
def test():
    return "✅ Тестовая страница работает!"

# ========== ЗАПУСК ==========
def run_bot():
    """Запуск Telegram бота в отдельном потоке"""
    print("🤖 Starting Telegram bot...")
    bot.polling(none_stop=True)

if __name__ == '__main__':
    # Запускаем бота в фоне
    bot_thread = threading.Thread(target=run_bot, daemon=True)
    bot_thread.start()
    
    # Запускаем Flask сервер
    port = int(os.environ.get('PORT', 10000))
    app.run(host='0.0.0.0', port=port)


