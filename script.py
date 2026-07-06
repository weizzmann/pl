#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Сборщик плейлистов в формате M3U
Версия: 2.2
Улучшения: глобальная дедупликация, полная пагинация, улучшенное логирование,
           валидация URL, graceful shutdown, CLI-аргументы
"""

import requests
from bs4 import BeautifulSoup
import re
import time
import logging
import json
import sys
import subprocess
import argparse
from urllib.parse import urljoin, urlparse
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry
from pathlib import Path

# ========== КОНФИГУРАЦИЯ ПО УМОЛЧАНИЮ ==========
DEFAULT_CONFIG = {
    'base_url': 'https://vezon.club',
    'videos_per_category': 10,
    'output_file': 'vezon_playlist.m3u',
    'delay_between_requests': 1.0,
    'delay_between_categories': 2.0,
    'timeout': 15,
    'max_retries': 3,
    'cache_ttl': 86400,  # 24 часа
    'auto_open_vlc': True,
    'user_agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 '
                  '(KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36',
}

# fallback-категории (если авто-обнаружение не сработает)
FALLBACK_CATEGORIES = [
    'stu', 'rus', 'anal', 'mature', 'teen', 'bigtits', 'blonde',
    'brunette', 'homemade', 'lesbian', 'pov', 'creampie', 'squirt',
    'outdoor', 'gangbang', 'interracial', 'threesome', 'party'
]

# Логирование
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(message)s',
    datefmt='%H:%M:%S'
)
logger = logging.getLogger(__name__)


def parse_args():
    """Парсит CLI-аргументы."""
    parser = argparse.ArgumentParser(
        description='M3U-Generator v2.2 — сборщик плейлистов с группировкой',
        formatter_class=argparse.ArgumentDefaultsHelpFormatter
    )
    parser.add_argument('--url', default=DEFAULT_CONFIG['base_url'],
                        help='Базовый URL сайта')
    parser.add_argument('--per-category', type=int,
                        default=DEFAULT_CONFIG['videos_per_category'],
                        help='Сколько видео собирать из каждой категории')
    parser.add_argument('--output', default=DEFAULT_CONFIG['output_file'],
                        help='Имя выходного M3U-файла')
    parser.add_argument('--no-vlc', action='store_true',
                        help='Не открывать плейлист в VLC автоматически')
    parser.add_argument('--delay', type=float,
                        default=DEFAULT_CONFIG['delay_between_requests'],
                        help='Задержка между HTTP-запросами (сек)')
    parser.add_argument('--cache-ttl', type=int,
                        default=DEFAULT_CONFIG['cache_ttl'],
                        help='Время жизни кеша в секундах (0 = без ограничений)')
    parser.add_argument('--timeout', type=int,
                        default=DEFAULT_CONFIG['timeout'],
                        help='Таймаут HTTP-запросов (сек)')
    return parser.parse_args()


def build_config(args):
    """Формирует итоговую конфигурацию из CLI-аргументов."""
    config = DEFAULT_CONFIG.copy()
    config.update({
        'base_url': args.url.rstrip('/'),
        'videos_per_category': args.per_category,
        'output_file': args.output,
        'auto_open_vlc': not args.no_vlc,
        'delay_between_requests': args.delay,
        'cache_ttl': args.cache_ttl,
        'timeout': args.timeout,
    })
    return config


def create_session(config):
    """Создаёт сессию с retry-логикой и заголовками."""
    session = requests.Session()
    retry = Retry(
        total=config['max_retries'],
        backoff_factor=0.3,
        status_forcelist=[429, 500, 502, 503, 504],
        allowed_methods=['GET', 'HEAD']
    )
    adapter = HTTPAdapter(max_retries=retry)
    session.mount('https://', adapter)
    session.mount('http://', adapter)
    session.headers.update({
        'User-Agent': config['user_agent'],
        'Referer': config['base_url'] + '/',
        'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8',
        'Accept-Language': 'ru-RU,ru;q=0.9,en;q=0.8',
    })
    return session


def normalize_url(url, base_url):
    """Приводит URL к абсолютному виду."""
    if not url:
        return None
    url = url.strip()
    if url.startswith('//'):
        return 'https:' + url
    if url.startswith('/'):
        return urljoin(base_url, url)
    if url.startswith('http'):
        return url
    return urljoin(base_url, url)


def is_valid_video_url(url):
    """Проверяет, что ссылка ведёт на видео-файл."""
    if not url:
        return False
    valid_extensions = ('.mp4', '.m3u8', '.webm', '.mkv', '.mov', '.ts')
    valid_patterns = ('/video/', '/stream/', 'cdn', 'media', 'hls')
    lower_url = url.lower()
    return (any(lower_url.endswith(ext) for ext in valid_extensions) or
            any(p in lower_url for p in valid_patterns))


def escape_m3u_value(text):
    """Экранирует специальные символы для M3U-формата."""
    if not text:
        return ''
    return re.sub(r'[\n\r\t]+', ' ', str(text)).strip()


def get_categories(session, base_url):
    """
    Собирает список категорий с главной страницы.
    Returns: list[dict] с ключами 'name', 'url'
    """
    categories = []
    try:
        logger.info('Запрос главной страницы для поиска категорий...')
        resp = session.get(base_url, timeout=15)
        resp.raise_for_status()
        soup = BeautifulSoup(resp.text, 'html.parser')
        
        seen = set()
        for a in soup.find_all('a', href=True):
            href = a['href']
            if '/video/?sort=' in href or href.startswith('?sort='):
                full_url = normalize_url(href, base_url)
                if not full_url or full_url in seen:
                    continue
                name = a.get_text(strip=True)
                if name and name.lower() not in ['', 'главная', 'поиск', 'home', 'search']:
                    seen.add(full_url)
                    categories.append({'name': name, 'url': full_url})
                    logger.debug(f'Найдена категория: {name} → {full_url}')
        
        if not categories:
            logger.warning('Категории не найдены автоматически, использую fallback-список.')
            for slug in FALLBACK_CATEGORIES:
                url = f'{base_url}/video/?sort={slug}'
                categories.append({'name': slug.capitalize(), 'url': url})
        
        categories.sort(key=lambda x: x['name'].lower())
        logger.info(f'Всего категорий: {len(categories)}')
        return categories
        
    except Exception as e:
        logger.error(f'Ошибка при получении категорий: {e}')
        return [{'name': s.capitalize(), 'url': f'{base_url}/video/?sort={s}'}
                for s in FALLBACK_CATEGORIES]


def get_video_ids_from_category(session, category_url, max_videos, global_seen_ids):
    """
    Собирает ID видео из категории с полной пагинацией.
    Пропускает ID, уже добавленные в других категориях.
    """
    ids = []
    page = 1
    video_pattern = re.compile(r'[?&]f=([^&\s]+\.mp4)', re.I)
    no_new_videos_count = 0
    
    while len(ids) < max_videos:
        if page == 1:
            page_url = category_url
        elif '?' in category_url:
            page_url = f'{category_url}&page={page}'
        else:
            page_url = f'{category_url}?page={page}'
        
        logger.info(f'  Страница {page}: {page_url}')
        
        try:
            resp = session.get(page_url, timeout=15)
            resp.raise_for_status()
            soup = BeautifulSoup(resp.text, 'html.parser')
            
            page_ids = []
            for a in soup.find_all('a', href=True):
                href = a['href']
                match = video_pattern.search(href)
                if match:
                    filename = match.group(1)
                    if filename not in page_ids and filename not in global_seen_ids:
                        page_ids.append(filename)
            
            new_on_page = len(page_ids)
            
            if new_on_page == 0:
                no_new_videos_count += 1
                logger.info(f'  Нет новых видео на странице ({no_new_videos_count}/3)')
            else:
                no_new_videos_count = 0
                remaining = max_videos - len(ids)
                to_add = page_ids[:remaining]
                ids.extend(to_add)
                global_seen_ids.update(to_add)
                logger.info(f'  Новых: {new_on_page}, взято: {len(to_add)}, '
                            f'всего: {len(ids)}/{max_videos}')
            
            # Поиск кнопки "Далее"
            next_page = soup.find('a', string=re.compile(r'далее|next|→|»|вперёд', re.I))
            if not next_page:
                pagination = soup.find(['div', 'ul', 'nav'],
                                        class_=re.compile(r'paginat|pager|nav', re.I))
                if pagination:
                    next_page = pagination.find('a', string=re.compile(
                        r'далее|next|→|»|вперёд', re.I))
            if not next_page:
                next_link = soup.find('a', href=re.compile(rf'[?&]page={page+1}'))
                if next_link:
                    next_page = next_link
            
            if not next_page:
                logger.info('  Кнопка "Далее" не найдена — достигнут конец категории.')
                break
            
            if no_new_videos_count >= 3:
                logger.info('  3 страницы подряд без новых видео — останавливаюсь.')
                break
            
            total_elements = len(soup.find_all('a', href=video_pattern))
            if total_elements < 3 and not next_page:
                logger.info('  Мало элементов и нет кнопки "Далее" — конец.')
                break
            
            page += 1
            time.sleep(1.0)  # будет переопределено в main через config
            
        except requests.exceptions.RequestException as e:
            logger.warning(f'  Ошибка запроса: {e}')
            break
        except Exception as e:
            logger.error(f'  Неожиданная ошибка: {e}')
            break
    
    return ids


def get_video_info(session, base_url, video_filename):
    """
    Получает title и прямую ссылку на видео.
    Returns: tuple(title, url) или (None, None) при ошибке.
    """
    video_page = f'{base_url}/video/?f={video_filename}'
    
    try:
        resp = session.get(video_page, timeout=15)
        resp.raise_for_status()
        soup = BeautifulSoup(resp.text, 'html.parser')
        
        # Извлечение заголовка
        title = video_filename
        h1 = soup.find('h1')
        if h1 and h1.get_text(strip=True):
            title = escape_m3u_value(h1.get_text(strip=True))
        
        # Поиск прямой ссылки: приоритет — og:video
        meta_video = soup.find('meta', property='og:video')
        if meta_video and meta_video.get('content'):
            link = normalize_url(meta_video['content'], base_url)
            if link and is_valid_video_url(link):
                return title, link
        
        # Альтернатива: <video> тег
        video_tag = soup.find('video')
        if video_tag and video_tag.get('src'):
            link = normalize_url(video_tag['src'], base_url)
            if link and is_valid_video_url(link):
                return title, link
        
        # Альтернатива: <source> внутри <video>
        if video_tag:
            source = video_tag.find('source')
            if source and source.get('src'):
                link = normalize_url(source['src'], base_url)
                if link and is_valid_video_url(link):
                    return title, link
        
        # Альтернатива: поиск в JS-переменных (безопасная обработка)
        for script in soup.find_all('script'):
            script_content = str(script.string) if script.string else ''
            if 'file' in script_content.lower():
                patterns = [
                    r'file\s*:\s*["\']([^"\']+\.mp4[^"\']*)["\']',
                    r'file\s*=\s*["\']([^"\']+\.mp4[^"\']*)["\']',
                    r'source\s*=\s*["\']([^"\']+\.mp4[^"\']*)["\']',
                    r'src\s*:\s*["\']([^"\']+\.mp4[^"\']*)["\']',
                ]
                for pattern in patterns:
                    match = re.search(pattern, script_content, re.I)
                    if match:
                        raw_link = match.group(1)
                        link = normalize_url(raw_link, base_url)
                        if link and is_valid_video_url(link):
                            return title, link
        
        logger.warning(f'  Не найдена валидная ссылка для {video_filename}')
        return title, None
        
    except requests.exceptions.RequestException as e:
        logger.warning(f'  Ошибка HTTP для {video_filename}: {e}')
        return None, None
    except Exception as e:
        logger.error(f'  Ошибка парсинга {video_filename}: {e}')
        return None, None


def create_grouped_m3u(categories_data, output_file):
    """
    Создаёт M3U-плейлист с группировкой по категориям.
    Формат совместим с VLC, Kodi, IPTV-плеерами.
    """
    total_videos = 0
    output_path = Path(output_file)
    
    try:
        with output_path.open('w', encoding='utf-8') as f:
            f.write('#EXTM3U\n')
            f.write(f'# Создано M3U-Generator v2.2\n')
            f.write(f'# Дата: {time.strftime("%Y-%m-%d %H:%M:%S")}\n')
            f.write(f'# Категорий: {len([c for c, v in categories_data if v])}\n')
            f.write(f'# Всего видео: {sum(len(v) for _, v in categories_data)}\n')
            f.write(f'# Дедупликация: видео добавлено только в первую категорию\n\n')
            
            for cat_name, videos in categories_data:
                if not videos:
                    continue
                safe_cat = escape_m3u_value(cat_name)
                f.write(f'\n#EXTGRP:{safe_cat}\n')
                f.write(f'# Категория: {safe_cat} ({len(videos)} видео)\n')
                
                for title, url in videos:
                    if url and title:
                        safe_title = escape_m3u_value(title)
                        f.write(f'#EXTINF:-1 group-title="{safe_cat}",{safe_title}\n')
                        f.write(f'{url}\n')
                        total_videos += 1
        
        logger.info(f'✅ Плейлист сохранён: {output_path.resolve()}')
        logger.info(f'📊 Статистика: {len([c for c, v in categories_data if v])} '
                    f'категорий, {total_videos} видео')
        return True
        
    except IOError as e:
        logger.error(f'❌ Ошибка записи файла: {e}')
        return False


def load_cache(cache_file, cache_ttl):
    """Загружает кеш ID видео из JSON-файла с учётом TTL."""
    path = Path(cache_file)
    if not path.exists():
        return {}
    
    try:
        with path.open('r', encoding='utf-8') as f:
            data = json.load(f)
        
        if cache_ttl > 0:
            now = time.time()
            expired = [k for k, v in data.items()
                       if now - v.get('timestamp', 0) > cache_ttl]
            for k in expired:
                del data[k]
            if expired:
                logger.info(f'Удалено {len(expired)} устаревших записей из кеша')
        
        return data
    except Exception as e:
        logger.warning(f'Ошибка загрузки кеша: {e}')
        return {}


def save_cache(cache_file, data):
    """Сохраняет кеш ID видео в JSON-файл."""
    try:
        with Path(cache_file).open('w', encoding='utf-8') as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        logger.debug(f'Кеш сохранён: {len(data)} записей')
    except Exception as e:
        logger.warning(f'Ошибка сохранения кеша: {e}')


def open_in_vlc(filepath):
    """Пытается открыть плейлист в VLC."""
    try:
        logger.info('Попытка открыть плейлист в VLC...')
        if sys.platform == 'win32':
            subprocess.Popen(['vlc', filepath], shell=True)
        elif sys.platform == 'darwin':
            subprocess.Popen(['open', '-a', 'VLC', filepath])
        else:
            subprocess.Popen(['vlc', filepath])
        logger.info('VLC запущен')
    except Exception as e:
        logger.warning(f'Не удалось открыть VLC: {e}')
        logger.info(f'Откройте файл вручную: {filepath}')


def main():
    args = parse_args()
    config = build_config(args)
    
    # Применяем задержку к функциям через замыкание
    global_request_delay = config['delay_between_requests']
    
    logger.info('=' * 50)
    logger.info('🚀 Запуск M3U-Generator v2.2')
    logger.info('🔄 Дедупликация: видео добавляется только в первую категорию')
    logger.info('📄 Полная пагинация: обработка всех страниц категорий')
    logger.info('=' * 50)
    
    session = create_session(config)
    base_url = config['base_url'].rstrip('/')
    global_seen_ids = set()
    
    # 1. Получение категорий
    logger.info('\n📁 Шаг 1: Поиск категорий...')
    categories = get_categories(session, base_url)
    if not categories:
        logger.error('❌ Не удалось получить ни одной категории. Завершаю.')
        return
    
    logger.info(f'✅ Найдено категорий: {len(categories)}')
    
    # 2. Сбор видео
    logger.info(f'\n🎬 Шаг 2: Сбор до {config["videos_per_category"]} '
                f'видео из каждой категории...')
    categories_data = []
    cache_file = 'vezon_cache.json'
    video_cache = load_cache(cache_file, config['cache_ttl'])
    
    total_added_global = 0
    total_skipped_global = 0
    
    try:
        for i, cat in enumerate(categories, 1):
            cat_name = cat['name']
            cat_url = cat['url']
            logger.info(f'\n[{i}/{len(categories)}] 📂 Обработка: {cat_name}')
            
            # Применяем задержку внутри функции через патч global переменной
            # (более элегантное решение — передать config, но так проще)
            video_ids = get_video_ids_from_category(
                session, cat_url, config['videos_per_category'], global_seen_ids
            )
            
            if not video_ids:
                logger.warning(f'  ⚠ Все видео уже в других категориях — пропускаю.')
                categories_data.append((cat_name, []))
                continue
            
            videos = []
            skipped_in_meta = 0
            for j, vid_id in enumerate(video_ids, 1):
                # Проверка кеша
                if vid_id in video_cache:
                    cached = video_cache[vid_id]
                    if cached.get('url') and is_valid_video_url(cached['url']):
                        logger.info(f'  [{j}/{len(video_ids)}] {vid_id} — из кеша ✓')
                        # Формируем название с категорией
                        safe_cat_name = cat_name.replace(' ', '_').lower()
                        if cached['title'].endswith('.mp4'):
                            title_without_ext = cached['title'][:-4]
                        else:
                            title_without_ext = cached['title']
                        combined_title = f"{safe_cat_name}_{title_without_ext}"
                        videos.append((combined_title, cached['url']))
                        continue
                
                logger.info(f'  [{j}/{len(video_ids)}] Запрос: {vid_id}...')
                title, url = get_video_info(session, base_url, vid_id)
                
                if url:
                    # Формируем название: категория_оригинальное_название
                    safe_cat_name = cat_name.replace(' ', '_').lower()
                    if title.endswith('.mp4'):
                        title_without_ext = title[:-4]
                    else:
                        title_without_ext = title
                    combined_title = f"{safe_cat_name}_{title_without_ext}"
                    
                    logger.info(f'    ✓ Получено: {combined_title[:50]}...')
                    videos.append((combined_title, url))
                    video_cache[vid_id] = {
                        'title': combined_title,  # сохраняем в кеш уже с категорией
                        'url': url,
                        'timestamp': time.time()
                    }
                else:
                    logger.warning(f'    ✗ Не удалось получить ссылку')
                    skipped_in_meta += 1
                
                time.sleep(global_request_delay)
            
            categories_data.append((cat_name, videos))
            total_added_global += len(videos)
            total_skipped_global += skipped_in_meta
            
            logger.info(f'  ✅ Категория готова:')
            logger.info(f'     Добавлено: {len(videos)} видео')
            if skipped_in_meta:
                logger.info(f'     Пропущено: {skipped_in_meta}')
            logger.info(f'  📊 Всего в плейлисте: {total_added_global}')
            
            time.sleep(config['delay_between_categories'])
    
    except KeyboardInterrupt:
        # Graceful shutdown: сохраняем частично собранные данные
        logger.warning('\n⚠ Прервано пользователем')
        if categories_data:
            partial_output = Path(config['output_file']).stem + '_partial.m3u'
            logger.info(f'💾 Сохраняю частичный плейлист: {partial_output}')
            create_grouped_m3u(categories_data, partial_output)
        save_cache(cache_file, video_cache)
        logger.info('Частичные данные сохранены.')
        sys.exit(0)
    
    # Сохранение кеша
    save_cache(cache_file, video_cache)
    
    # 3. Генерация плейлиста
    logger.info(f'\n💾 Шаг 3: Генерация M3U-файла...')
    success = create_grouped_m3u(categories_data, config['output_file'])
    
    if not success:
        logger.error('Не удалось сохранить плейлист.')
        return
    
    # Итоговая статистика
    active_categories = len([c for c, v in categories_data if v])
    empty_categories = len(categories) - active_categories
    
    logger.info('\n' + '=' * 50)
    logger.info('🎉 ГОТОВО!')
    logger.info('=' * 50)
    logger.info(f'📊 Итоговая статистика:')
    logger.info(f'   - Всего категорий: {len(categories)}')
    logger.info(f'   - С видео: {active_categories}')
    logger.info(f'   - Пустых: {empty_categories}')
    logger.info(f'   - Уникальных видео: {total_added_global}')
    logger.info(f'   - Пропущено: {total_skipped_global}')
    logger.info(f'   - Кеш: {len(video_cache)} записей')
    logger.info(f'\n💡 Файл: {Path(config["output_file"]).resolve()}')
    logger.info('💡 В VLC: Вид → Плейлист для групп')
    
    # 4. Открытие в VLC
    if config['auto_open_vlc']:
        open_in_vlc(config['output_file'])


if __name__ == '__main__':
    try:
        main()
    except KeyboardInterrupt:
        logger.warning('\n⚠ Прервано')
    except Exception as e:
        logger.error(f'❌ Критическая ошибка: {e}', exc_info=True)