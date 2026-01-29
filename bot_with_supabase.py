import os
import sys
import telebot
from datetime import datetime
from flask import Flask, request
import pg8000
from pg8000.native import Connection
import json
import time
from telebot import types

print("=" * 60, file=sys.stderr)
print("🤖 WINE WAREHOUSE BOT WITH SUPABASE", file=sys.stderr)
print(f"Python: {sys.version}", file=sys.stderr)
print("=" * 60, file=sys.stderr)

# Получаем настройки
TOKEN = os.environ['TELEGRAM_TOKEN']
ADMIN_IDS = [int(x) for x in os.environ['ADMIN_IDS'].split(',')]
DATABASE_URL = os.environ['SUPABASE_DB_URL']

bot = telebot.TeleBot(TOKEN)
app = Flask(__name__)

# ========== БАЗА ДАННЫХ ==========
def parse_db_url(url):
    """Разбираем URL подключения"""
    url = url.replace('postgresql://', '')
    
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
    
    database = database.split('?')[0]
    
    return {
        'user': user,
        'password': password,
        'host': host,
        'port': port,
        'database': database
    }

def get_db_connection():
    """Создаем подключение к БД"""
    try:
        params = parse_db_url(DATABASE_URL)
        conn = Connection(**params)
        return conn
    except Exception as e:
        print(f"❌ DB connection error: {e}", file=sys.stderr)
        return None

# ========== ПОЛЬЗОВАТЕЛИ ==========
def get_user_by_telegram_id(telegram_id):
    """Получить пользователя по telegram_id"""
    conn = get_db_connection()
    if not conn:
        return None
    
    try:
        result = conn.run("""
            SELECT u.*, w.name as warehouse_name 
            FROM users u
            LEFT JOIN warehouses w ON u.warehouse_id = w.id
            WHERE u.telegram_id = :telegram_id
        """, telegram_id=telegram_id)
        
        if result:
            return {
                'id': result[0][0],
                'telegram_id': result[0][1],
                'username': result[0][2],
                'full_name': result[0][3],
                'role': result[0][4],
                'warehouse_id': result[0][6],
                'warehouse_name': result[0][7]
            }
        return None
    except Exception as e:
        print(f"❌ Error getting user: {e}", file=sys.stderr)
        return None
    finally:
        try:
            conn.close()
        except:
            pass

# ========== СКЛАДЫ ==========
def get_all_warehouses():
    """Получить все склады (для админа)"""
    conn = get_db_connection()
    if not conn:
        return []
    
    try:
        result = conn.run("SELECT id, name FROM warehouses ORDER BY name")
        return [{'id': row[0], 'name': row[1]} for row in result]
    except Exception as e:
        print(f"❌ Error getting warehouses: {e}", file=sys.stderr)
        return []
    finally:
        try:
            conn.close()
        except:
            pass

# ========== ТОВАРЫ ==========
def get_all_products():
    """Получить все товары"""
    conn = get_db_connection()
    if not conn:
        return []
    
    try:
        result = conn.run("SELECT id, name FROM products ORDER BY name")
        return [{'id': row[0], 'name': row[1]} for row in result]
    except Exception as e:
        print(f"❌ Error getting products: {e}", file=sys.stderr)
        return []
    finally:
        try:
            conn.close()
        except:
            pass

# ========== ОСТАТКИ ==========
def get_user_balance(telegram_id, warehouse_id=None):
    """Получить остатки пользователя"""
    conn = get_db_connection()
    if not conn:
        return []
    
    try:
        user = get_user_by_telegram_id(telegram_id)
        if not user:
            return []
        
        # Для обычных пользователей - только их склад
        # Для админов - все склады или конкретный, если указан
        if user['role'] == 'admin' and not warehouse_id:
            # Админ видит все
            result = conn.run("""
                SELECT w.name, p.name, SUM(b.quantity)
                FROM balances b
                JOIN warehouses w ON b.warehouse_id = w.id
                JOIN products p ON b.product_id = p.id
                GROUP BY w.name, p.name
                ORDER BY w.name, p.name
            """)
            balances = []
            for row in result:
                balances.append({
                    'warehouse': row[0],
                    'product': row[1],
                    'quantity': row[2] or 0
                })
            return balances
        else:
            # Для обычного пользователя или админа с указанным складом
            target_warehouse = warehouse_id or user['warehouse_id']
            if not target_warehouse:
                return []
            
            result = conn.run("""
                SELECT p.name, b.quantity
                FROM balances b
                JOIN products p ON b.product_id = p.id
                WHERE b.user_id = :user_id AND b.warehouse_id = :warehouse_id
                ORDER BY p.name
            """, user_id=user['id'], warehouse_id=target_warehouse)
            
            balances = []
            for row in result:
                balances.append({
                    'product': row[0],
                    'quantity': row[1] or 0
                })
            return balances
            
    except Exception as e:
        print(f"❌ Error getting balance: {e}", file=sys.stderr)
        return []
    finally:
        try:
            conn.close()
        except:
            pass

# ========== ОПЕРАЦИИ ==========
def add_transaction(telegram_id, product_id, quantity, transaction_type, warehouse_id=None):
    """Добавить операцию (списание/пополнение)"""
    conn = get_db_connection()
    if not conn:
        return False, "❌ Ошибка подключения к БД"
    
    try:
        user = get_user_by_telegram_id(telegram_id)
        if not user:
            return False, "❌ Пользователь не найден"
        
        # Определяем склад
        target_warehouse = warehouse_id or user['warehouse_id']
        if not target_warehouse:
            return False, "❌ Склад не назначен"
        
        # Проверяем достаточно ли товара для списания
        if transaction_type == 'out':
            current_result = conn.run("""
                SELECT quantity FROM balances 
                WHERE user_id = :user_id AND product_id = :product_id AND warehouse_id = :warehouse_id
            """, user_id=user['id'], product_id=product_id, warehouse_id=target_warehouse)
            
            # Проверяем, есть ли запись вообще
            if not current_result or not current_result[0]:
                return False, "❌ У вас нет этого товара на складе"
            
            current_quantity = current_result[0][0] or 0
            
            if current_quantity < quantity:
                return False, f"❌ Недостаточно товара. Доступно: {current_quantity} шт., а вы хотите списать: {quantity} шт."
        
        # Обновляем баланс
        conn.run("""
            INSERT INTO balances (user_id, product_id, warehouse_id, quantity)
            VALUES (:user_id, :product_id, :warehouse_id, :quantity)
            ON CONFLICT (user_id, product_id, warehouse_id) 
            DO UPDATE SET quantity = balances.quantity + :change
        """, 
        user_id=user['id'], 
        product_id=product_id,
        warehouse_id=target_warehouse,
        quantity=quantity if transaction_type == 'in' else -quantity,
        change=quantity if transaction_type == 'in' else -quantity)
        
        # Добавляем запись в историю
        conn.run("""
            INSERT INTO transactions (user_id, product_id, warehouse_id, type, quantity)
            VALUES (:user_id, :product_id, :warehouse_id, :type, :quantity)
        """,
        user_id=user['id'],
        product_id=product_id,
        warehouse_id=target_warehouse,
        type=transaction_type,
        quantity=quantity)
        
        return True, f"✅ Товар успешно {'пополнен' if transaction_type == 'in' else 'списан'} в количестве {quantity} шт."
        
    except Exception as e:
        print(f"❌ Error adding transaction: {e}", file=sys.stderr)
        return False, f"❌ Ошибка: {e}"
    finally:
        try:
            conn.close()
        except:
            pass
# ========== КОМАНДЫ БОТА ==========
@bot.message_handler(commands=['start'])
def start(message):
    """Начало работы с кнопками"""
    user = get_user_by_telegram_id(message.from_user.id)
    
    if not user:
        bot.reply_to(message, "❌ Вы не зарегистрированы в системе. Обратитесь к администратору.")
        return
    
    role = "👑 Администратор" if user['role'] == 'admin' else "👤 Пользователь"
    warehouse = f"📦 Склад: {user['warehouse_name']}" if user['warehouse_name'] else "📦 Склад не назначен"
    
    # Создаем клавиатуру с командами
    markup = telebot.types.ReplyKeyboardMarkup(resize_keyboard=True)
    
    # Общие команды для всех
    markup.row('📊 Мои остатки', '📤 Списать')
    
    if user['role'] == 'admin':
        # Команды только для админа
        markup.row('➕ Товар', '🏢 Склад', '👤 Пользователь')
        markup.row('📦 Все остатки', '📋 Список складов', '👥 Список пользователей')
        markup.row('🔄 Пополнить остатки')
    
    # Формируем ответ БЕЗ лишних отступов внутри строки
    response = f"""✅ *Добро пожаловать, {user['full_name']}!*

{role}
{warehouse}

*Используйте кнопки ниже или команды:*
"""
    
    if user['role'] == 'admin':
        response += """
*📋 Все команды:*

📊 /balance - Мои остатки
📤 /spend - Списать товар
➕ /add_product - Добавить товар
🏢 /add_warehouse - Добавить склад
👤 /add_user - Добавить пользователя
📦 /all_balance - Все остатки
🔄 /add - Пополнить остатки
📋 /warehouses - Список складов
👥 /users - Список пользователей
"""
    else:
        # ВАЖНО: строки начинаются сразу с текста, без отступов!
        response += """
📊 /balance - Мои остатки
📤 /spend - Списать товар
"""
    
    bot.send_message(message.chat.id, response, 
                     parse_mode='Markdown', 
                     reply_markup=markup)
#=======================================
#========================================================

@bot.message_handler(commands=['balance'])
def balance(message):
    """Показать остатки"""
    user = get_user_by_telegram_id(message.from_user.id)
    if not user:
        bot.reply_to(message, "❌ Вы не зарегистрированы.")
        return
    
    balances = get_user_balance(message.from_user.id)
    
    if not balances:
        bot.reply_to(message, "📦 На вашем складе нет товаров.")
        return
    
    response = f"📦 ОСТАТКИ НА СКЛАДЕ '{user['warehouse_name'] or 'не назначен'}':\n\n"
    total = 0
    
    if user['role'] == 'admin' and len(balances) > 0 and 'warehouse' in balances[0]:
        # Админ видит все склады
        current_warehouse = None
        for item in balances:
            if item['warehouse'] != current_warehouse:
                response += f"\n🏢 {item['warehouse']}:\n"
                current_warehouse = item['warehouse']
            response += f"  • {item['product']}: {item['quantity']} шт.\n"
            total += item['quantity']
    else:
        # Обычный пользователь
        for item in balances:
            response += f"• {item['product']}: {item['quantity']} шт.\n"
            total += item['quantity']
    
    response += f"\n📊 Всего позиций: {len(balances)}"
    bot.reply_to(message, response)

@bot.message_handler(commands=['spend'])
def spend_command(message):
    """Списать товар"""
    user = get_user_by_telegram_id(message.from_user.id)
    if not user:
        bot.reply_to(message, "❌ Вы не зарегистрированы.")
        return
    
    if not user['warehouse_id']:
        bot.reply_to(message, "❌ Вам не назначен склад. Обратитесь к администратору.")
        return
    
    # Запрашиваем товар
    products = get_all_products()
    if not products:
        bot.reply_to(message, "❌ В системе нет товаров.")
        return
    
    # Создаем клавиатуру с товарами
    markup = telebot.types.ReplyKeyboardMarkup(one_time_keyboard=True, resize_keyboard=True)
    for product in products:
        markup.add(f"{product['id']}. {product['name']}")
    markup.add("❌ Отмена")
    
    msg = bot.reply_to(message, "📝 Выберите товар для списания:", reply_markup=markup)
    bot.register_next_step_handler(msg, process_spend_product)

def process_spend_product(message):
    """Обработка выбора товара для списания"""
    if message.text == "❌ Отмена":
        bot.reply_to(message, "❌ Отменено", reply_markup=telebot.types.ReplyKeyboardRemove())
        return
    
    try:
        # Пробуем разные форматы ввода
        text = message.text.strip()
        
        # Формат 1: "2. Вино Белое" -> берем первое число
        if '.' in text:
            product_id = int(text.split('.')[0].strip())
        # Формат 2: просто число "1"
        else:
            product_id = int(text)
        
        # Запрашиваем количество
        msg = bot.reply_to(message, "📝 Введите количество для списания:", 
                          reply_markup=telebot.types.ReplyKeyboardRemove())
        bot.register_next_step_handler(msg, process_spend_quantity, product_id)
        
    except (ValueError, IndexError):
        # Если не удалось распарсить
        bot.reply_to(message, "❌ Неверный формат. Введите номер товара или выберите из списка.", 
                    reply_markup=telebot.types.ReplyKeyboardRemove())

def process_add_quantity(message, warehouse_id, target_telegram_id, product_id):
    """Обработка количества для пополнения"""
    try:
        quantity = int(message.text)
        if quantity <= 0:
            bot.reply_to(message, "❌ Количество должно быть больше 0")
            return
        
        # Выполняем пополнение
        success, result_message = add_transaction(target_telegram_id, product_id, quantity, 'in', warehouse_id)
        bot.reply_to(message, result_message)
    except ValueError:
        bot.reply_to(message, "❌ Введите число")

# ========== АДМИН КОМАНДЫ ==========
@bot.message_handler(commands=['add_product'])
def add_product_command(message):
    """Добавить товар (админ)"""
    user = get_user_by_telegram_id(message.from_user.id)
    if not user or user['role'] != 'admin':
        bot.reply_to(message, "❌ Только для администраторов")
        return
    
    msg = bot.reply_to(message, "📝 Введите название нового товара:")
    bot.register_next_step_handler(msg, process_add_product)

def process_add_product(message):
    """Обработка добавления товара"""
    product_name = message.text.strip()
    if not product_name:
        bot.reply_to(message, "❌ Название не может быть пустым")
        return
    
    conn = get_db_connection()
    if not conn:
        bot.reply_to(message, "❌ Ошибка подключения к БД")
        return
    
    try:
        conn.run("INSERT INTO products (name) VALUES (:name)", name=product_name)
        bot.reply_to(message, f"✅ Товар '{product_name}' успешно добавлен")
    except Exception as e:
        bot.reply_to(message, f"❌ Ошибка: {e}")
    finally:
        try:
            conn.close()
        except:
            pass

@bot.message_handler(commands=['add_warehouse'])
def add_warehouse_command(message):
    """Добавить склад (админ)"""
    user = get_user_by_telegram_id(message.from_user.id)
    if not user or user['role'] != 'admin':
        bot.reply_to(message, "❌ Только для администраторов")
        return
    
    msg = bot.reply_to(message, "📝 Введите название нового склада:")
    bot.register_next_step_handler(msg, process_add_warehouse)

def process_add_warehouse(message):
    """Обработка добавления склада"""
    warehouse_name = message.text.strip()
    if not warehouse_name:
        bot.reply_to(message, "❌ Название не может быть пустым")
        return
    
    conn = get_db_connection()
    if not conn:
        bot.reply_to(message, "❌ Ошибка подключения к БД")
        return
    
    try:
        conn.run("INSERT INTO warehouses (name) VALUES (:name)", name=warehouse_name)
        bot.reply_to(message, f"✅ Склад '{warehouse_name}' успешно добавлен")
    except Exception as e:
        bot.reply_to(message, f"❌ Ошибка: {e}")
    finally:
        try:
            conn.close()
        except:
            pass

@bot.message_handler(commands=['all_balance'])
def all_balance_command(message):
    """Все остатки (админ)"""
    user = get_user_by_telegram_id(message.from_user.id)
    if not user or user['role'] != 'admin':
        bot.reply_to(message, "❌ Только для администраторов")
        return
    
    # Используем функцию баланса без указания склада
    balances = get_user_balance(message.from_user.id)
    
    if not balances:
        bot.reply_to(message, "📦 В системе нет остатков.")
        return
    
    response = "📦 ОСТАТКИ ПО ВСЕМ СКЛАДАМ:\n\n"
    current_warehouse = None
    total_all = 0
    
    for item in balances:
        if item['warehouse'] != current_warehouse:
            response += f"\n🏢 {item['warehouse']}:\n"
            current_warehouse = item['warehouse']
        response += f"  • {item['product']}: {item['quantity']} шт.\n"
        total_all += item['quantity']
    
    response += f"\n📊 Всего товаров в системе: {total_all} шт."
    bot.reply_to(message, response)

@bot.message_handler(commands=['add'])
def add_stock_command(message):
    """Пополнить остатки (админ)"""
    user = get_user_by_telegram_id(message.from_user.id)
    if not user or user['role'] != 'admin':
        bot.reply_to(message, "❌ Только для администраторов")
        return
    
    # Запрашиваем склад
    warehouses = get_all_warehouses()
    if not warehouses:
        bot.reply_to(message, "❌ В системе нет складов. Сначала /add_warehouse")
        return
    
    markup = telebot.types.ReplyKeyboardMarkup(one_time_keyboard=True, resize_keyboard=True)
    for warehouse in warehouses:
        markup.add(f"{warehouse['id']}. {warehouse['name']}")
    markup.add("❌ Отмена")
    
    msg = bot.reply_to(message, "📦 Выберите склад для пополнения:", reply_markup=markup)
    bot.register_next_step_handler(msg, process_add_warehouse_selection)

def process_add_warehouse_selection(message):
    """Обработка выбора склада"""
    if message.text == "❌ Отмена":
        bot.reply_to(message, "❌ Отменено", reply_markup=telebot.types.ReplyKeyboardRemove())
        return
    
    try:
        warehouse_id = int(message.text.split('.')[0])
        
        # Запрашиваем пользователя (кому пополняем)
        msg = bot.reply_to(message, "👤 Введите telegram_id пользователя:", 
                          reply_markup=telebot.types.ReplyKeyboardRemove())
        bot.register_next_step_handler(msg, process_add_user_selection, warehouse_id)
    except:
        bot.reply_to(message, "❌ Неверный формат", reply_markup=telebot.types.ReplyKeyboardRemove())

def process_add_user_selection(message, warehouse_id):
    """Обработка выбора пользователя"""
    try:
        target_telegram_id = int(message.text)
        
        # Проверяем существование пользователя
        target_user = get_user_by_telegram_id(target_telegram_id)
        if not target_user:
            bot.reply_to(message, f"❌ Пользователь с ID {target_telegram_id} не найден")
            return
        
        # Запрашиваем товар
        products = get_all_products()
        if not products:
            bot.reply_to(message, "❌ В системе нет товаров")
            return
        
        markup = telebot.types.ReplyKeyboardMarkup(one_time_keyboard=True, resize_keyboard=True)
        for product in products:
            markup.add(f"{product['id']}. {product['name']}")
        markup.add("❌ Отмена")
        
        msg = bot.reply_to(message, "📝 Выберите товар для пополнения:", reply_markup=markup)
        bot.register_next_step_handler(msg, process_add_product_selection, warehouse_id, target_telegram_id)
    except ValueError:
        bot.reply_to(message, "❌ Введите числовой ID")

def process_add_product_selection(message, warehouse_id, target_telegram_id):
    """Обработка выбора товара"""
    if message.text == "❌ Отмена":
        bot.reply_to(message, "❌ Отменено", reply_markup=telebot.types.ReplyKeyboardRemove())
        return
    
    try:
        product_id = int(message.text.split('.')[0])
        
        # Запрашиваем количество
        msg = bot.reply_to(message, "📝 Введите количество для пополнения:", 
                          reply_markup=telebot.types.ReplyKeyboardRemove())
        bot.register_next_step_handler(msg, process_add_quantity, warehouse_id, target_telegram_id, product_id)
    except:
        bot.reply_to(message, "❌ Неверный формат", reply_markup=telebot.types.ReplyKeyboardRemove())

def process_add_quantity(message, warehouse_id, target_telegram_id, product_id):
    """Обработка количества для пополнения"""
    try:
        quantity = int(message.text)
        if quantity <= 0:
            bot.reply_to(message, "❌ Количество должно быть больше 0")
            return
        
        # Выполняем пополнение
        if add_transaction(target_telegram_id, product_id, quantity, 'in', warehouse_id):
            bot.reply_to(message, f"✅ Товар успешно пополнен в количестве {quantity} шт.")
        else:
            bot.reply_to(message, "❌ Не удалось пополнить товар")
    except ValueError:
        bot.reply_to(message, "❌ Введите число")

@bot.message_handler(commands=['add_user'])
def add_user_command(message):
    """Добавить пользователя (админ)"""
    user = get_user_by_telegram_id(message.from_user.id)
    if not user or user['role'] != 'admin':
        bot.reply_to(message, "❌ Только для администраторов")
        return
    
    msg = bot.reply_to(message, "👤 Введите telegram_id нового пользователя:")
    bot.register_next_step_handler(msg, process_add_user_telegram_id)

def process_add_user_telegram_id(message):
    """Обработка telegram_id нового пользователя"""
    try:
        telegram_id = int(message.text)
        
        # Проверяем, не существует ли уже
        existing = get_user_by_telegram_id(telegram_id)
        if existing:
            bot.reply_to(message, f"❌ Пользователь с ID {telegram_id} уже существует")
            return
        
        msg = bot.reply_to(message, "📝 Введите имя нового пользователя:")
        bot.register_next_step_handler(msg, process_add_user_name, telegram_id)
    except ValueError:
        bot.reply_to(message, "❌ Введите числовой ID")

def process_add_user_name(message, telegram_id):
    """Обработка имени нового пользователя"""
    full_name = message.text.strip()
    if not full_name:
        bot.reply_to(message, "❌ Имя не может быть пустым")
        return
    
    # Запрашиваем склад
    warehouses = get_all_warehouses()
    if not warehouses:
        bot.reply_to(message, "❌ В системе нет складов. Сначала /add_warehouse")
        return
    
    markup = telebot.types.ReplyKeyboardMarkup(one_time_keyboard=True, resize_keyboard=True)
    for warehouse in warehouses:
        markup.add(f"{warehouse['id']}. {warehouse['name']}")
    markup.add("❌ Отмена")
    
    msg = bot.reply_to(message, "📦 Выберите склад для пользователя:", reply_markup=markup)
    bot.register_next_step_handler(msg, process_add_user_warehouse, telegram_id, full_name)

def process_add_user_warehouse(message, telegram_id, full_name):
    """Обработка выбора склада для нового пользователя"""
    if message.text == "❌ Отмена":
        bot.reply_to(message, "❌ Отменено", reply_markup=telebot.types.ReplyKeyboardRemove())
        return
    
    try:
        warehouse_id = int(message.text.split('.')[0])
        
        conn = get_db_connection()
        if not conn:
            bot.reply_to(message, "❌ Ошибка подключения к БД")
            return
        
        # Определяем роль (можно добавить выбор роли, но пока user)
        role = 'admin' if telegram_id in ADMIN_IDS else 'user'
        
        conn.run("""
            INSERT INTO users (telegram_id, full_name, role, warehouse_id) 
            VALUES (:telegram_id, :full_name, :role, :warehouse_id)
        """, telegram_id=telegram_id, full_name=full_name, role=role, warehouse_id=warehouse_id)
        
        # Инициализируем нулевые остатки для всех товаров
        products = get_all_products()
        for product in products:
            conn.run("""
                INSERT INTO balances (user_id, product_id, warehouse_id, quantity)
                SELECT u.id, :product_id, :warehouse_id, 0
                FROM users u
                WHERE u.telegram_id = :telegram_id
                ON CONFLICT (user_id, product_id, warehouse_id) DO NOTHING
            """, product_id=product['id'], warehouse_id=warehouse_id, telegram_id=telegram_id)
        
        bot.reply_to(message, f"✅ Пользователь {full_name} (ID: {telegram_id}) успешно добавлен!", 
                    reply_markup=telebot.types.ReplyKeyboardRemove())
        
    except Exception as e:
        bot.reply_to(message, f"❌ Ошибка: {e}", reply_markup=telebot.types.ReplyKeyboardRemove())
    finally:
        try:
            conn.close()
        except:
            pass
@bot.message_handler(commands=['warehouses'])
def warehouses_command(message):
    """Список складов с пользователями (админ)"""
    user = get_user_by_telegram_id(message.from_user.id)
    if not user or user['role'] != 'admin':
        bot.reply_to(message, "❌ Только для администраторов")
        return
    
    conn = get_db_connection()
    if not conn:
        bot.reply_to(message, "❌ Ошибка подключения к БД")
        return
    
    try:
        # Получаем склады с пользователями
        result = conn.run("""
            SELECT w.id, w.name, 
                   COALESCE(u.count, 0) as user_count,
                   STRING_AGG(u.full_name, ', ') as users
            FROM warehouses w
            LEFT JOIN (
                SELECT warehouse_id, 
                       COUNT(*) as count,
                       STRING_AGG(full_name, ', ') as full_name
                FROM users 
                GROUP BY warehouse_id
            ) u ON w.id = u.warehouse_id
            GROUP BY w.id, w.name, u.count
            ORDER BY w.name
        """)
        
        if not result:
            bot.reply_to(message, "📦 В системе нет складов")
            return
        
        response = "📋 СПИСОК СКЛАДОВ:\n\n"
        
        for row in result:
            warehouse_id, name, user_count, users = row
            users_list = users if users else "нет пользователей"
            response += f"🏢 {name} (ID: {warehouse_id})\n"
            response += f"   👥 Пользователей: {user_count}\n"
            response += f"   📝 Пользователи: {users_list}\n\n"
        
        bot.reply_to(message, response)
        
    except Exception as e:
        bot.reply_to(message, f"❌ Ошибка: {e}")
    finally:
        try:
            conn.close()
        except:
            pass


@bot.message_handler(commands=['users'])
def users_command(message):
    """Список пользователей (админ)"""
    user = get_user_by_telegram_id(message.from_user.id)
    if not user or user['role'] != 'admin':
        bot.reply_to(message, "❌ Только для администраторов")
        return
    
    conn = get_db_connection()
    if not conn:
        bot.reply_to(message, "❌ Ошибка подключения к БД")
        return
    
    try:
        result = conn.run("""
            SELECT u.telegram_id, u.full_name, u.role, w.name as warehouse_name
            FROM users u
            LEFT JOIN warehouses w ON u.warehouse_id = w.id
            ORDER BY u.full_name
        """)
        
        if not result:
            bot.reply_to(message, "👥 В системе нет пользователей")
            return
        
        response = "📋 СПИСОК ПОЛЬЗОВАТЕЛЕЙ:\n\n"
        
        for row in result:
            telegram_id, full_name, role, warehouse_name = row
            role_icon = "👑" if role == 'admin' else "👤"
            warehouse = warehouse_name if warehouse_name else "склад не назначен"
            response += f"{role_icon} {full_name}\n"
            response += f"   ID: {telegram_id}\n"
            response += f"   📦 Склад: {warehouse}\n\n"
        
        bot.reply_to(message, response)
        
    except Exception as e:
        bot.reply_to(message, f"❌ Ошибка: {e}")
    finally:
        try:
            conn.close()
        except:
            pass

# ========== ОБРАБОТКА КНОПОК ==========
@bot.message_handler(func=lambda message: True)
def handle_buttons(message):
    """Обработка нажатий на кнопки и текстовых сообщений"""
    
    # Если сообщение пустое - пропускаем
    if not message.text:
        return
    
    # Если это команда (начинается с /) - ПРОПУСКАЕМ
    if message.text.startswith('/'):
        return
    
    user = get_user_by_telegram_id(message.from_user.id)
    if not user:
        bot.reply_to(message, "Сначала /start")
        return
    
    text = message.text
    
    # Обработка кнопок
    if text == '📊 Мои остатки':
        balance(message)
    elif text == '📤 Списать':
        spend_command(message)
    elif text == '📦 Все остатки' and user['role'] == 'admin':
        all_balance_command(message)
    elif text == '➕ Товар' and user['role'] == 'admin':
        add_product_command(message)
    elif text == '🏢 Склад' and user['role'] == 'admin':
        add_warehouse_command(message)
    elif text == '👤 Пользователь' and user['role'] == 'admin':
        add_user_command(message)
    elif text == '📋 Список складов' and user['role'] == 'admin':
        warehouses_command(message)
    elif text == '👥 Список пользователей' and user['role'] == 'admin':
        users_command(message)
    elif text == '🔄 Пополнить остатки' and user['role'] == 'admin':
        add_stock_command(message)
    else:
        # Обработка обычного текста
        if text.lower() in ['привет', 'hello', 'hi', 'здравствуй']:
            bot.reply_to(message, f"Привет, {user['full_name']}! 👋\nИспользуйте кнопки или команды.")
        elif text.lower() in ['помощь', 'help', 'справка']:
            bot.reply_to(message, "Используйте кнопки или команды из меню. /start - для списка команд.")
        else:
            bot.reply_to(message, "Не понимаю команду. Используйте кнопки ниже или команды из меню.\n/start - для помощи.")

# ========== WEBHOOK И ЗАПУСК ==========
@app.route('/')
def index():
    """Корневая страница"""
    return '🤖 Wine Warehouse Bot is running!', 200

@app.route('/health')
def health_check():
    """Для UptimeRobot"""
    return 'OK!', 200

@app.route('/webhook', methods=['POST', 'GET'])
def webhook():
    """Обработчик вебхука от Telegram"""
    if request.method == 'GET':
        return 'Webhook is active!', 200
    
    try:
        json_str = request.get_data().decode('UTF-8')
        update = telebot.types.Update.de_json(json_str)
        bot.process_new_updates([update])
        return 'ok', 200
    except Exception as e:
        print(f"❌ Webhook error: {e}", file=sys.stderr)
        return 'error', 500

if __name__ == '__main__':
    # Тест подключения к БД
    print("🔍 Testing database...", file=sys.stderr)
    conn = get_db_connection()
    if conn:
        try:
            result = conn.run("SELECT version()")
            print(f"✅ Database: {result[0][0][:50]}...", file=sys.stderr)
            conn.close()
        except Exception as e:
            print(f"⚠️ Database test warning: {e}", file=sys.stderr)
    
    # Настройка вебхука
    try:
        bot.remove_webhook()
        time.sleep(1)
        
        webhook_url = f"https://wine-telegram-bot.onrender.com/webhook"
        bot.set_webhook(url=webhook_url)
        print(f"✅ Webhook установлен: {webhook_url}", file=sys.stderr)
    except Exception as e:
        print(f"❌ Webhook setup error: {e}", file=sys.stderr)
    
    # Запуск Flask
    port = int(os.environ.get('PORT', 10000))
    print(f"🌐 Starting Flask server on port {port}...", file=sys.stderr)
    app.run(host='0.0.0.0', port=port)
