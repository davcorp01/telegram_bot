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
import pandas as pd
from io import BytesIO
from datetime import datetime, timedelta

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
    """Получить пользователя по telegram_id - ДОБАВИМ ОТЛАДКУ"""
    print(f"DEBUG: Searching user with telegram_id={telegram_id}", file=sys.stderr)
    
    conn = get_db_connection()
    if not conn:
        print(f"DEBUG: No DB connection", file=sys.stderr)
        return None
    
    try:
        result = conn.run("""
            SELECT u.*, w.name as warehouse_name 
            FROM users u
            LEFT JOIN warehouses w ON u.warehouse_id = w.id
            WHERE u.telegram_id = :telegram_id
        """, telegram_id=telegram_id)
        
        print(f"DEBUG: Query result: {result}", file=sys.stderr)
        
        if result:
            user = {
                'id': result[0][0],
                'telegram_id': result[0][1],
                'username': result[0][2],
                'full_name': result[0][3],
                'role': result[0][4],
                'warehouse_id': result[0][6],
                'warehouse_name': result[0][7]
            }
            print(f"DEBUG: Found user: {user['full_name']}", file=sys.stderr)
            return user
        
        print(f"DEBUG: User not found", file=sys.stderr)
        return None
    except Exception as e:
        print(f"DEBUG: Error: {e}", file=sys.stderr)
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
    """Получить остатки пользователя (только его склад)"""
    conn = get_db_connection()
    if not conn:
        return []
    
    try:
        user = get_user_by_telegram_id(telegram_id)
        if not user:
            return []
        
        # Определяем склад (если не указан явно - берем склад пользователя)
        target_warehouse = warehouse_id or user['warehouse_id']
        if not target_warehouse:
            return []
        
        # Получаем остатки из таблицы stock (только для этого склада)
        result = conn.run("""
            SELECT p.name, COALESCE(s.quantity, 0) as quantity
            FROM products p
            LEFT JOIN stock s ON p.id = s.product_id AND s.warehouse_id = :warehouse_id
            WHERE COALESCE(s.quantity, 0) > 0
            ORDER BY p.name
        """, warehouse_id=target_warehouse)
        
        balances = []
        for row in result:
            balances.append({
                'product': row[0],
                'quantity': row[1]
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
    """Добавить операцию (списание/пополнение) - НОВАЯ ВЕРСИЯ без user_id в stock"""
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
        
        # Проверяем достаточно ли товара для списания (из таблицы stock)
        if transaction_type == 'out':
            current = conn.run("""
                SELECT quantity FROM stock 
                WHERE product_id = :product_id AND warehouse_id = :warehouse_id
            """, product_id=product_id, warehouse_id=target_warehouse)
            
            if not current or current[0][0] is None or current[0][0] < quantity:
                available = current[0][0] if current and current[0][0] is not None else 0
                return False, f"❌ Недостаточно товара. Доступно: {available} л."
        
        # Обновляем остатки на складе (stock)
        change = quantity if transaction_type == 'in' else -quantity
        
        conn.run("""
            INSERT INTO stock (warehouse_id, product_id, quantity)
            VALUES (:warehouse_id, :product_id, :change)
            ON CONFLICT (warehouse_id, product_id) 
            DO UPDATE SET quantity = stock.quantity + EXCLUDED.quantity
        """, warehouse_id=target_warehouse, product_id=product_id, change=change)
        
        # Добавляем запись в историю (transactions)
        # Теперь нужно получить user_id для записи в историю
        user_result = conn.run("SELECT id FROM users WHERE telegram_id = :telegram_id", 
                              telegram_id=telegram_id)
        
        if user_result:
            user_id = user_result[0][0]
            conn.run("""
                INSERT INTO transactions (product_id, warehouse_id, type, quantity)
                VALUES (:product_id, :warehouse_id, :type, :quantity)
            """, product_id=product_id, warehouse_id=target_warehouse, type=transaction_type, quantity=quantity)
        
        return True, f"✅ Товар успешно {'пополнен' if transaction_type == 'in' else 'списан'} в количестве {quantity} л."
        
    except Exception as e:
        print(f"❌ Error adding transaction: {e}", file=sys.stderr)
        return False, f"❌ Ошибка: {e}"
    finally:
        try:
            conn.close()
        except:
            pass
# ========== ЭКСПОРТ В EXCEL ==========
def export_transactions_to_excel(telegram_id, days=30):
    """Экспорт транзакций в Excel"""
    conn = get_db_connection()
    if not conn:
        return None, "❌ Ошибка подключения к БД"
    
    try:
        user = get_user_by_telegram_id(telegram_id)
        if not user or user['role'] != 'admin':
            return None, "❌ Только для администраторов"
        
        # Вычисляем дату начала
        start_date = datetime.now() - timedelta(days=days)
        
        # Получаем транзакции
        result = conn.run("""
            SELECT 
                t.date,
                COALESCE(u.full_name, 'Неизвестный') as пользователь,
                w.name as склад,
                p.name as товар,
                CASE 
                    WHEN t.type = 'in' THEN 'Приход'
                    ELSE 'Расход'
                END as тип,
                t.quantity as количество,
                t.notes as примечания
            FROM transactions t
            JOIN warehouses w ON t.warehouse_id = w.id
            LEFT JOIN users u ON w.id = u.warehouse_id
            JOIN products p ON t.product_id = p.id
            WHERE t.date >= :start_date
            ORDER BY t.date DESC, w.name
        """, start_date=start_date.date())
        
        if not result:
            return None, f"📊 Нет операций за последние {days} дней"
        
        # Создаем DataFrame
        df = pd.DataFrame(result, columns=[
            'Дата', 'Пользователь', 'Склад', 'Товар', 
            'Тип операции', 'Количество', 'Примечания'
        ])
        
        # Создаем Excel файл в памяти
        output = BytesIO()
        with pd.ExcelWriter(output, engine='openpyxl') as writer:
            df.to_excel(writer, sheet_name='Операции', index=False)
            
            # Добавляем итоги
            summary = df.groupby(['Тип операции', 'Товар'])['Количество'].sum().reset_index()
            summary.to_excel(writer, sheet_name='Итоги', index=False)
        
        output.seek(0)
        return output, f"✅ Экспортировано {len(df)} операций"
        
    except Exception as e:
        print(f"❌ Error exporting transactions: {e}", file=sys.stderr)
        return None, f"❌ Ошибка экспорта: {e}"
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
        markup.row('📤 Экспорт дня', '📤 Экспорт недели')
        markup.row('📤 Экспорт месяца', '📊 Экспорт остатков')
    
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
📋 /products - Список товаров
🗑️ /d_product - Удалить товар
🏢 /add_warehouse - Добавить склад
👤 /add_user - Добавить пользователя
📦 /all_balance - Все остатки
🔄 /add - Пополнить остатки
📋 /warehouses - Список складов
👥 /users - Список пользователей
📤 /export_today - Операции за день
📤 /export_week - Операции за неделю  
📤 /export_month - Операции за месяц
📊 /export_balances - Текущие остатки
"""
    else:
        # ВАЖНО: строки начинаются сразу с текста, без отступов!

        #🗑️ /delete_product - Удалить товар
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
    """Показать остатки пользователя"""
    user = get_user_by_telegram_id(message.from_user.id)
    if not user:
        bot.reply_to(message, "❌ Вы не зарегистрированы.")
        return
    
    # ДЛЯ ВСЕХ пользователей (включая админа) - только их склад
    balances = get_user_balance(message.from_user.id)
    
    if not balances:
        warehouse_name = user['warehouse_name'] or 'не назначен'
        bot.reply_to(message, f"📦 На складе '{warehouse_name}' нет товаров.")
        return
    
    warehouse_name = user['warehouse_name'] or 'не назначен'
    response = f"📦 ОСТАТКИ НА СКЛАДЕ '{warehouse_name}':\n\n"
    total = 0
    
    for item in balances:
        response += f"• {item['product']}: {item['quantity']} л.\n"
        total += item['quantity']
    
    response += f"\n📊 Всего: {total} л."
    bot.reply_to(message, response)


@bot.message_handler(commands=['spend'])
def spend_command(message):
    """Списать товар"""
    user = get_user_by_telegram_id(message.from_user.id)
    if not user:
        bot.reply_to(message, "❌ Вы не зарегистрированы.")
        return
    
    # Для админа - запрашиваем склад
    if user['role'] == 'admin':
        warehouses = get_all_warehouses()
        if not warehouses:
            bot.reply_to(message, "❌ В системе нет складов.")
            return
        
        markup = telebot.types.ReplyKeyboardMarkup(one_time_keyboard=True, resize_keyboard=True)
        for warehouse in warehouses:
            markup.add(f"{warehouse['id']}. {warehouse['name']}")
        markup.add("❌ Отмена")
        
        msg = bot.reply_to(message, "📦 *Выберите склад для списания:*", 
                          parse_mode='Markdown', 
                          reply_markup=markup)
        bot.register_next_step_handler(msg, process_spend_warehouse_admin)
    
    else:
        # Для обычных пользователей - сразу их склад
        if not user['warehouse_id']:
            bot.reply_to(message, "❌ Вам не назначен склад. Обратитесь к администратору.")
            return
        
        # Показываем товары только с их склада
        show_products_for_spend(message, user['warehouse_id'])

def process_spend_warehouse_admin(message):
    """Обработка выбора склада для админа"""
    if message.text == "❌ Отмена":
        bot.reply_to(message, "❌ Отменено", reply_markup=telebot.types.ReplyKeyboardRemove())
        return
    
    try:
        warehouse_id = int(message.text.split('.')[0])
        show_products_for_spend(message, warehouse_id)
    except (ValueError, IndexError):
        bot.reply_to(message, "❌ Неверный формат", reply_markup=telebot.types.ReplyKeyboardRemove())

def show_products_for_spend(message, warehouse_id, user_id=None):
    """Показать товары для списания с конкретного склада"""
    conn = get_db_connection()
    if not conn:
        bot.reply_to(message, "❌ Ошибка подключения к БД")
        return
    
    try:
        # Получаем товары с остатками > 0 на этом складе (из stock)
        result = conn.run("""
            SELECT p.id, p.name, COALESCE(s.quantity, 0) as quantity
            FROM products p
            LEFT JOIN stock s ON p.id = s.product_id AND s.warehouse_id = :warehouse_id
            WHERE COALESCE(s.quantity, 0) > 0
            ORDER BY p.name
        """, warehouse_id=warehouse_id)
        
        if not result:
            # Получаем название склада для сообщения
            warehouse_name = conn.run("SELECT name FROM warehouses WHERE id = :id", id=warehouse_id)
            warehouse_name = warehouse_name[0][0] if warehouse_name else "этом складе"
            
            bot.reply_to(message, f"📦 На складе '{warehouse_name}' нет товаров для списания.")
            return
        
        markup = telebot.types.ReplyKeyboardMarkup(one_time_keyboard=True, resize_keyboard=True)
        
        response = "📝 *Выберите товар для списания:*\n\n"
        for product_id, product_name, quantity in result:
            markup.add(f"{product_id}. {product_name} ({quantity} л.)")
            response += f"*{product_id}.* {product_name} - {quantity} л.\n"
        
        markup.add("❌ Отмена")
        
        # Сохраняем warehouse_id для следующего шага
        msg = bot.send_message(message.chat.id, response, 
                              parse_mode='Markdown', 
                              reply_markup=markup)
        bot.register_next_step_handler(msg, process_spend_product_with_warehouse, warehouse_id)
        
    except Exception as e:
        bot.reply_to(message, f"❌ Ошибка: {e}")
    finally:
        try:
            conn.close()
        except:
            pass

def process_spend_product_with_warehouse(message, warehouse_id):
    """Обработка выбора товара с учетом склада"""
    if message.text == "❌ Отмена":
        bot.reply_to(message, "❌ Отменено", reply_markup=telebot.types.ReplyKeyboardRemove())
        return
    
    try:
        text = message.text.strip()
        
        # Парсим ID товара (формат: "1. Вино Красное (88 л.)" или просто "1")
        if '.' in text:
            product_id = int(text.split('.')[0].strip())
        else:
            product_id = int(text)
        
        # Запрашиваем количество
        msg = bot.reply_to(message, "📝 Введите количество для списания:", 
                          reply_markup=telebot.types.ReplyKeyboardRemove())
        bot.register_next_step_handler(msg, process_spend_quantity_with_warehouse, warehouse_id, product_id)
        
    except (ValueError, IndexError):
        bot.reply_to(message, "❌ Неверный формат. Введите номер товара.", 
                    reply_markup=telebot.types.ReplyKeyboardRemove())

def process_spend_quantity_with_warehouse(message, warehouse_id, product_id):
    """Обработка количества для списания с учетом склада"""
    try:
        quantity = int(message.text)
        if quantity <= 0:
            bot.reply_to(message, "❌ Количество должно быть больше 0")
            return
        
        # Получаем пользователя на этом складе
        conn = get_db_connection()
        if not conn:
            bot.reply_to(message, "❌ Ошибка подключения к БД")
            return
        
        user_result = conn.run("""
            SELECT telegram_id FROM users 
            WHERE warehouse_id = :warehouse_id
            LIMIT 1
        """, warehouse_id=warehouse_id)
        
        conn.close()
        
        if not user_result:
            bot.reply_to(message, "❌ На этом складе нет пользователя")
            return
        
        telegram_id = user_result[0][0]
        
        # Выполняем списание
        success, result_message = add_transaction(telegram_id, product_id, quantity, 'out', warehouse_id)
        bot.reply_to(message, result_message)
        
    except ValueError:
        bot.reply_to(message, "❌ Введите число")


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


def process_spend_quantity(message, product_id):
    """Обработка количества для списания"""
    try:
        quantity = int(message.text)
        if quantity <= 0:
            bot.reply_to(message, "❌ Количество должно быть больше 0")
            return
        
        # Выполняем списание
        success, result_message = add_transaction(message.from_user.id, product_id, quantity, 'out')
        
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
    """Обработка добавления товара с проверкой дубликатов"""
    product_name = message.text.strip()
    if not product_name:
        bot.reply_to(message, "❌ Название не может быть пустым")
        return
    
    conn = get_db_connection()
    if not conn:
        bot.reply_to(message, "❌ Ошибка подключения к БД")
        return
    
    try:
        # ПРОВЕРЯЕМ: есть ли уже товар с таким названием (без учета регистра)
        existing = conn.run("""
            SELECT id, name FROM products 
            WHERE LOWER(name) = LOWER(:product_name)
        """, product_name=product_name)
        
        if existing:
            bot.reply_to(message, f"❌ Товар '{product_name}' уже существует (ID: {existing[0][0]})")
            return
        
        # Добавляем новый товар
        conn.run("INSERT INTO products (name) VALUES (:name)", name=product_name)
        
        # Получаем ID нового товара
        new_product = conn.run("SELECT id FROM products WHERE name = :name", name=product_name)
        
        if new_product:
            product_id = new_product[0][0]
            
            # Создаем нулевые остатки на всех складах для нового товара
            warehouses = conn.run("SELECT id FROM warehouses")
            for warehouse in warehouses:
                conn.run("""
                    INSERT INTO stock (warehouse_id, product_id, quantity)
                    VALUES (:warehouse_id, :product_id, 0)
                    ON CONFLICT (warehouse_id, product_id) DO NOTHING
                """, warehouse_id=warehouse[0], product_id=product_id)
            
            bot.reply_to(message, f"✅ Товар '{product_name}' успешно добавлен (ID: {product_id})")
        else:
            bot.reply_to(message, "❌ Не удалось добавить товар")
        
    except Exception as e:
        error_msg = str(e)
        if "duplicate" in error_msg.lower() or "unique" in error_msg.lower():
            bot.reply_to(message, f"❌ Товар '{product_name}' уже существует в базе данных")
        else:
            bot.reply_to(message, f"❌ Ошибка: {error_msg[:100]}")
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
    
    conn = get_db_connection()
    if not conn:
        bot.reply_to(message, "❌ Ошибка подключения к БД")
        return
    
    try:
        # Получаем все остатки со всех складов
        result = conn.run("""
            SELECT 
                w.name as склад,
                p.name as товар,
                COALESCE(s.quantity, 0) as остаток
            FROM stock s
            JOIN warehouses w ON s.warehouse_id = w.id
            JOIN products p ON s.product_id = p.id
            WHERE s.quantity > 0
            ORDER BY w.name, p.name
        """)
        
        if not result:
            bot.reply_to(message, "📦 В системе нет остатков.")
            return
        
        response = "📦 ОСТАТКИ ПО ВСЕМ СКЛАДАМ:\n\n"
        current_warehouse = None
        warehouse_count = {}
        
        for warehouse_name, product_name, quantity in result:
            if warehouse_name != current_warehouse:
                response += f"\n🏢 *{warehouse_name}:*\n"
                current_warehouse = warehouse_name
                warehouse_count[warehouse_name] = 0
            
            response += f"  • {product_name}: {quantity} л.\n"
            warehouse_count[warehouse_name] += 1
        
        # Добавляем статистику
        response += f"\n📊 *Статистика:*"
        for warehouse_name, count in warehouse_count.items():
            response += f"\n🏢 {warehouse_name}: {count} позиций"
        
        total_items = sum(warehouse_count.values())
        response += f"\n\n📈 Всего позиций в системе: {total_items}"
        
        bot.reply_to(message, response, parse_mode='Markdown')
        
    except Exception as e:
        bot.reply_to(message, f"❌ Ошибка: {e}")
    finally:
        try:
            conn.close()
        except:
            pass

@bot.message_handler(commands=['add'])
def add_stock_command(message):
    """Пополнить склад (админ) - УПРОЩЕННАЯ ВЕРСИЯ"""
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
    
    # Показываем склады с пользователями
    conn = get_db_connection()
    if conn:
        for warehouse in warehouses:
            # Получаем пользователя на этом складе
            user_result = conn.run("""
                SELECT u.full_name FROM users u 
                WHERE u.warehouse_id = :warehouse_id
                LIMIT 1
            """, warehouse_id=warehouse['id'])
            
            user_name = user_result[0][0] if user_result else "нет пользователя"
            markup.add(f"{warehouse['id']}. {warehouse['name']} ({user_name})")
        
        conn.close()
    
    markup.add("❌ Отмена")
    
    msg = bot.reply_to(message, "📦 *Выберите склад для пополнения:*", 
                      parse_mode='Markdown', 
                      reply_markup=markup)
    bot.register_next_step_handler(msg, process_add_warehouse_simple)

def process_add_warehouse_simple(message):
    """Обработка выбора склада (упрощенная)"""
    if message.text == "❌ Отмена":
        bot.reply_to(message, "❌ Отменено", reply_markup=telebot.types.ReplyKeyboardRemove())
        return
    
    try:
        warehouse_id = int(message.text.split('.')[0])
        
        # Получаем пользователя на этом складе
        conn = get_db_connection()
        if not conn:
            bot.reply_to(message, "❌ Ошибка подключения к БД", 
                        reply_markup=telebot.types.ReplyKeyboardRemove())
            return
        
        user_result = conn.run("""
            SELECT u.telegram_id, u.full_name FROM users u 
            WHERE u.warehouse_id = :warehouse_id
            LIMIT 1
        """, warehouse_id=warehouse_id)
        
        conn.close()
        
        if not user_result:
            bot.reply_to(message, "❌ На этом складе нет пользователя", 
                        reply_markup=telebot.types.ReplyKeyboardRemove())
            return
        
        telegram_id, full_name = user_result[0]
        
        # Запрашиваем товар
        products = get_all_products()
        if not products:
            bot.reply_to(message, "❌ В системе нет товаров", 
                        reply_markup=telebot.types.ReplyKeyboardRemove())
            return
        
        markup = telebot.types.ReplyKeyboardMarkup(one_time_keyboard=True, resize_keyboard=True)
        for product in products:
            markup.add(f"{product['id']}. {product['name']}")
        markup.add("❌ Отмена")
        
        msg = bot.reply_to(message, f"📝 Выберите товар для пополнения склада *{full_name}*:", 
                          parse_mode='Markdown', 
                          reply_markup=markup)
        bot.register_next_step_handler(msg, process_add_product_simple, warehouse_id, telegram_id)
        
    except (ValueError, IndexError):
        bot.reply_to(message, "❌ Неверный формат. Выберите склад из списка.", 
                    reply_markup=telebot.types.ReplyKeyboardRemove())

def process_add_product_simple(message, warehouse_id, telegram_id):
    """Обработка выбора товара (упрощенная)"""
    if message.text == "❌ Отмена":
        bot.reply_to(message, "❌ Отменено", reply_markup=telebot.types.ReplyKeyboardRemove())
        return
    
    try:
        product_id = int(message.text.split('.')[0])
        
        # Запрашиваем количество
        msg = bot.reply_to(message, "📝 Введите количество для пополнения:", 
                          reply_markup=telebot.types.ReplyKeyboardRemove())
        bot.register_next_step_handler(msg, process_add_quantity_simple, warehouse_id, telegram_id, product_id)
        
    except (ValueError, IndexError):
        bot.reply_to(message, "❌ Неверный формат. Выберите товар из списка.", 
                    reply_markup=telebot.types.ReplyKeyboardRemove())

def process_add_quantity_simple(message, warehouse_id, telegram_id, product_id):
    """Обработка количества для пополнения (упрощенная)"""
    try:
        quantity = int(message.text)
        if quantity <= 0:
            bot.reply_to(message, "❌ Количество должно быть больше 0")
            return
        
        # Выполняем пополнение
        success, result_message = add_transaction(telegram_id, product_id, quantity, 'in', warehouse_id)
        bot.reply_to(message, result_message)
        
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
        
        print(f"DEBUG: process_add_user_telegram_id: checking telegram_id={telegram_id}", file=sys.stderr)
        
        # Проверяем, не существует ли уже
        existing = get_user_by_telegram_id(telegram_id)
        
        if existing:
            print(f"DEBUG: User EXISTS: {existing['full_name']}", file=sys.stderr)
            bot.reply_to(message, f"❌ Пользователь с ID {telegram_id} уже существует ({existing['full_name']})")
            return
        
        print(f"DEBUG: User NOT found, continuing...", file=sys.stderr)
        
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
            bot.reply_to(message, "❌ Ошибка подключения к БД", reply_markup=telebot.types.ReplyKeyboardRemove())
            return
        
        # Пробуем вставить пользователя
        role = 'admin' if telegram_id in ADMIN_IDS else 'user'
        
        try:
            conn.run("""
                INSERT INTO users (telegram_id, full_name, role, warehouse_id) 
                VALUES (:telegram_id, :full_name, :role, :warehouse_id)
                ON CONFLICT (telegram_id) 
                DO UPDATE SET 
                    full_name = EXCLUDED.full_name,
                    role = EXCLUDED.role,
                    warehouse_id = EXCLUDED.warehouse_id
                RETURNING id
            """, telegram_id=telegram_id, full_name=full_name, role=role, warehouse_id=warehouse_id)
            
            # Получаем ID пользователя
            result = conn.run("SELECT id FROM users WHERE telegram_id = :telegram_id", 
                             telegram_id=telegram_id)
            
            if not result:
                bot.reply_to(message, "❌ Не удалось создать/обновить пользователя", 
                            reply_markup=telebot.types.ReplyKeyboardRemove())
                return
            
            user_id = result[0][0]
            
            # Инициализируем нулевые остатки в stock (НЕ В balances!)
            products = get_all_products()
            for product in products:
                conn.run("""
                    INSERT INTO stock (warehouse_id, product_id, quantity)
                    VALUES (:warehouse_id, :product_id, 0)
                    ON CONFLICT (warehouse_id, product_id) DO NOTHING
                """, warehouse_id=warehouse_id, product_id=product['id'])
            
            bot.reply_to(message, f"✅ Пользователь {full_name} (ID: {telegram_id}) успешно добавлен!", 
                        reply_markup=telebot.types.ReplyKeyboardRemove())
            
        except Exception as insert_error:
            error_str = str(insert_error)
            bot.reply_to(message, f"❌ Ошибка БД: {error_str[:100]}", 
                        reply_markup=telebot.types.ReplyKeyboardRemove())
                
    except ValueError:
        bot.reply_to(message, "❌ Введите номер склада", reply_markup=telebot.types.ReplyKeyboardRemove())
    except Exception as e:
        bot.reply_to(message, f"❌ Неожиданная ошибка: {str(e)[:100]}", 
                    reply_markup=telebot.types.ReplyKeyboardRemove())
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
# ========== КОМАНДЫ ЭКСПОРТА ==========

@bot.message_handler(commands=['export_today', 'export_day'])
def export_today_command(message):
    """Экспорт сегодняшних операций"""
    file_data, message_text = export_transactions_to_excel(message.from_user.id, days=1)
    
    if file_data:
        bot.send_document(message.chat.id, file_data, 
                         caption=message_text,
                         visible_file_name=f"операции_за_{datetime.now().strftime('%d.%m.%Y')}.xlsx")
    else:
        bot.reply_to(message, message_text)

@bot.message_handler(commands=['export_week'])
def export_week_command(message):
    """Экспорт операций за неделю"""
    file_data, message_text = export_transactions_to_excel(message.from_user.id, days=7)
    
    if file_data:
        bot.send_document(message.chat.id, file_data,
                         caption=message_text,
                         visible_file_name=f"операции_неделя_{datetime.now().strftime('%d.%m.%Y')}.xlsx")
    else:
        bot.reply_to(message, message_text)

@bot.message_handler(commands=['export_month'])
def export_month_command(message):
    """Экспорт операций за месяц"""
    file_data, message_text = export_transactions_to_excel(message.from_user.id, days=30)
    
    if file_data:
        bot.send_document(message.chat.id, file_data,
                         caption=message_text,
                         visible_file_name=f"операции_месяц_{datetime.now().strftime('%d.%m.%Y')}.xlsx")
    else:
        bot.reply_to(message, message_text)

@bot.message_handler(commands=['export_balances'])
def export_balances_command(message):
    """Экспорт текущих остатков из таблицы stock"""
    conn = get_db_connection()
    if not conn:
        bot.reply_to(message, "❌ Ошибка подключения к БД")
        return
    
    try:
        user = get_user_by_telegram_id(message.from_user.id)
        if not user or user['role'] != 'admin':
            bot.reply_to(message, "❌ Только для администраторов")
            return
        
        # Получаем остатки из таблицы STOCK (не balances!)
        result = conn.run("""
            SELECT 
                COALESCE(u.full_name, 'Нет пользователя') as пользователь,
                w.name as склад,
                p.name as товар,
                s.quantity as остаток,
                s.updated_at as обновлено
            FROM stock s
            JOIN warehouses w ON s.warehouse_id = w.id
            JOIN products p ON s.product_id = p.id
            LEFT JOIN users u ON w.id = u.warehouse_id
            WHERE s.quantity > 0
            ORDER BY w.name, p.name
        """)
        
        if not result:
            bot.reply_to(message, "📊 Нет данных об остатках")
            return
        
        # Создаем Excel
        df = pd.DataFrame(result, columns=['Пользователь', 'Склад', 'Товар', 'Остаток', 'Обновлено'])
        
        output = BytesIO()
        with pd.ExcelWriter(output, engine='openpyxl') as writer:
            df.to_excel(writer, sheet_name='Остатки', index=False)
            
            # Сводка по складам
            summary = df.groupby(['Склад', 'Товар'])['Остаток'].sum().reset_index()
            summary.to_excel(writer, sheet_name='Сводка', index=False)
        
        output.seek(0)
        
        bot.send_document(message.chat.id, output,
                         caption=f"✅ Экспортировано {len(df)} записей об остатках",
                         visible_file_name=f"остатки_{datetime.now().strftime('%d.%m.%Y')}.xlsx")
        
    except Exception as e:
        bot.reply_to(message, f"❌ Ошибка: {e}")
    finally:
        try:
            conn.close()
        except:
            pass
            
# ========== Показать все продукты ==========

@bot.message_handler(commands=['products'])
def products_command(message):
    """Список всех товаров (админ)"""
    user = get_user_by_telegram_id(message.from_user.id)
    if not user or user['role'] != 'admin':
        bot.reply_to(message, "❌ Только для администраторов")
        return
    
    products = get_all_products()
    if not products:
        bot.reply_to(message, "📦 В системе нет товаров")
        return
    
    response = "📋 СПИСОК ТОВАРОВ:\n\n"
    for product in products:
        response += f"• ID: {product['id']}, Название: {product['name']}\n"
    
    response += f"\n📊 Всего товаров: {len(products)}"
    bot.reply_to(message, response)


# ========== СИНОНИМЫ КОМАНД ==========

@bot.message_handler(commands=['adduser'])
def adduser_alias_command(message):
    """Синоним для /add_user (без подчеркивания)"""
    add_user_command(message)

@bot.message_handler(commands=['addproduct'])
def addproduct_alias_command(message):
    """Синоним для /add_product (без подчеркивания)"""
    add_product_command(message)

@bot.message_handler(commands=['addwarehouse'])
def addwarehouse_alias_command(message):
    """Синоним для /add_warehouse (без подчеркивания)"""
    add_warehouse_command(message)

@bot.message_handler(commands=['allbalance'])
def allbalance_alias_command(message):
    """Синоним для /all_balance (без подчеркивания)"""
    all_balance_command(message)
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
    elif text == '📤 Экспорт дня' and user['role'] == 'admin':
        export_today_command(message)
    elif text == '📤 Экспорт недели' and user['role'] == 'admin':
        export_week_command(message)
    elif text == '📤 Экспорт месяца' and user['role'] == 'admin':
        export_month_command(message)
    elif text == '📊 Экспорт остатков' and user['role'] == 'admin':
        export_balances_command(message)
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
