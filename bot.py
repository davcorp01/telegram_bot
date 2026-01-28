# Вставьте этот код в ваш bot.py после команды /balance

@bot.message_handler(commands=['spend'])
def spend(message):
    """Начать процесс списания вина"""
    user_id = message.from_user.id
    
    if user_id not in db.balances:
        bot.reply_to(message, "❌ Сначала /start")
        return
    
    # Создаем клавиатуру с товарами
    markup = telebot.types.ReplyKeyboardMarkup(row_width=1, resize_keyboard=True)
    
    for product in db.products:
        current_qty = db.balances[user_id].get(product["id"], 0)
        if current_qty > 0:
            markup.add(telebot.types.KeyboardButton(f"Списать {product['name']}"))
    
    markup.add(telebot.types.KeyboardButton("❌ Отмена"))
    
    bot.reply_to(message, "🏷️ Выберите товар для списания:", reply_markup=markup)
    bot.register_next_step_handler(message, process_spend_selection)

def process_spend_selection(message):
    """Обработка выбора товара"""
    if message.text == "❌ Отмена":
        bot.reply_to(message, "❎ Отменено", reply_markup=telebot.types.ReplyKeyboardRemove())
        return
    
    selected_product = None
    for product in db.products:
        if f"Списать {product['name']}" in message.text:
            selected_product = product
            break
    
    if not selected_product:
        bot.reply_to(message, "❌ Товар не найден", reply_markup=telebot.types.ReplyKeyboardRemove())
        return
    
    bot.send_message(message.chat.id,
                    f"📝 Выбран: {selected_product['name']}\n"
                    f"💰 Введите количество (литры):",
                    reply_markup=telebot.types.ReplyKeyboardRemove())
    
    bot.register_next_step_handler(message,
                                 lambda msg, prod=selected_product: process_spend_quantity(msg, prod))

def process_spend_quantity(message, product):
    """Обработка количества"""
    try:
        quantity = float(message.text)
        user_id = message.from_user.id
        
        if quantity <= 0:
            bot.reply_to(message, "❌ Введите положительное число")
            return
        
        current_balance = db.balances[user_id].get(product["id"], 0)
        
        if current_balance >= quantity:
            # Списание
            db.balances[user_id][product["id"]] = current_balance - quantity
            
            # Запись операции
            db.transactions.append({
                "user_id": user_id,
                "product": product["name"],
                "quantity": quantity,
                "type": "out",
                "date": datetime.now().strftime("%Y-%m-%d %H:%M")
            })
            
            bot.reply_to(message,
                        f"✅ УСПЕШНО СПИСАНО!\n\n"
                        f"📦 Товар: {product['name']}\n"
                        f"📏 Количество: {quantity} л\n"
                        f"💰 Новый остаток: {db.balances[user_id][product['id']]} л")
        else:
            bot.reply_to(message,
                        f"❌ НЕДОСТАТОЧНО!\n\n"
                        f"📦 Товар: {product['name']}\n"
                        f"📏 Требуется: {quantity} л\n"
                        f"💰 Доступно: {current_balance} л")
            
    except ValueError:
        bot.reply_to(message, "❌ Введите число (например: 2.5)")

# Обновите команду /help
@bot.message_handler(commands=['help'])
def help_cmd(message):
    response = (
        "🆘 СПРАВКА:\n\n"
        "/start - регистрация\n"
        "/balance - мои остатки\n"
        "/spend - списать вино\n"
        "/ping - проверка связи\n"
        "/help - эта справка"
    )
    
    if message.from_user.id in ADMIN_IDS:
        response += "\n\n👑 АДМИН:\n/admin - панель управления"
    
    bot.reply_to(message, response)
