import os
import sys
import time
import logging
import re
import asyncio
import httpx
from dotenv import load_dotenv
from telethon import TelegramClient, events, Button

# Load environment variables
load_dotenv()

# Pending interactive search selections map
PENDING_SEARCHES = {}

# Active Telegram media downloads tracker
# { chat_id: { filename, file_size, received, started_at, pct } }
ACTIVE_DOWNLOADS = {}

def format_size(size_bytes: int) -> str:
    if not size_bytes:
        return ""
    if size_bytes >= 1073741824:
        return f"{size_bytes / 1073741824:.2f} GB"
    elif size_bytes >= 1048576:
        return f"{size_bytes / 1048576:.1f} MB"
    return f"{size_bytes} B"

def extract_movie_key(title: str) -> str:
    clean = re.sub(r'[\._\-\[\]\(\)]+', ' ', title)
    clean = re.sub(r'\s+', ' ', clean).strip().lower()
    return clean




# Setup logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(message)s',
    handlers=[
        logging.StreamHandler(sys.stdout)
    ]
)

# Load configuration
TELEGRAM_API_ID = os.getenv("TELEGRAM_API_ID")
TELEGRAM_API_HASH = os.getenv("TELEGRAM_API_HASH")
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHANNELS_STR = os.getenv("TELEGRAM_CHANNELS", "")
JACKETT_URL = os.getenv("JACKETT_URL", "http://localhost:9117").rstrip('/')
JACKETT_API_KEY = os.getenv("JACKETT_API_KEY")
QBITTORRENT_URL = os.getenv("QBITTORRENT_URL", "http://localhost:8080").rstrip('/')
QBITTORRENT_USERNAME = os.getenv("QBITTORRENT_USERNAME", "admin")
QBITTORRENT_PASSWORD = os.getenv("QBITTORRENT_PASSWORD", "adminadmin")
DOWNLOAD_DIR = os.getenv("DOWNLOAD_DIR", "U:\\movies")

# Validate required variables
if not TELEGRAM_API_ID or not TELEGRAM_API_HASH:
    logging.critical("CRITICAL: TELEGRAM_API_ID and TELEGRAM_API_HASH must be set in the environment.")
    sys.exit(1)

try:
    TELEGRAM_API_ID = int(TELEGRAM_API_ID)
except ValueError:
    logging.critical("CRITICAL: TELEGRAM_API_ID must be a valid integer.")
    sys.exit(1)

# Helper to parse channels list
def parse_channels(channels_str):
    if not channels_str:
        return []
    channels = []
    for ch in channels_str.split(','):
        ch = ch.strip()
        if not ch:
            continue
        # Check if it's an ID
        if ch.startswith('-100') or ch.isdigit() or (ch.startswith('-') and ch[1:].isdigit()):
            try:
                channels.append(int(ch))
            except ValueError:
                channels.append(ch)
        else:
            channels.append(ch)
    return channels

TELEGRAM_CHANNELS = parse_channels(TELEGRAM_CHANNELS_STR)
logging.info(f"Configured Telegram channels for search: {TELEGRAM_CHANNELS}")

# --- qBittorrent Client ---
class QBittorrentClient:
    def __init__(self, base_url, username, password):
        self.base_url = base_url
        self.username = username
        self.password = password
        self.client = httpx.AsyncClient(timeout=30.0, follow_redirects=True)
        self.authenticated = False

    async def login(self):
        url = f"{self.base_url}/api/v2/auth/login"
        data = {"username": self.username, "password": self.password}
        try:
            response = await self.client.post(url, data=data)
            if response.status_code in (200, 204):
                logging.info("Successfully authenticated with qBittorrent.")
                self.authenticated = True
                return True
            else:
                logging.error(f"qBittorrent auth failed with code {response.status_code}: {response.text}")
                return False
        except Exception as e:
            logging.error(f"Error connecting to qBittorrent at {url}: {e}")
            return False

    async def add_torrent(self, torrent_url, save_path):
        if not self.authenticated:
            success = await self.login()
            if not success:
                logging.error("Cannot add torrent: qBittorrent authentication failed.")
                return False

        url = f"{self.base_url}/api/v2/torrents/add"
        # We pass both urls and savepath parameters. qBittorrent uses form-encoded parameters.
        data = {
            "urls": torrent_url,
            "savepath": save_path
        }
        try:
            response = await self.client.post(url, data=data)
            if response.status_code in (200, 202):
                logging.info(f"Torrent successfully added to qBittorrent. Savepath: {save_path}")
                return "added"
            elif response.status_code == 409:
                logging.info(f"Torrent is already present in qBittorrent download list. Savepath: {save_path}")
                return "exists"
            elif response.status_code == 403:
                logging.warning("qBittorrent returned authorization error (403). Re-authenticating...")
                self.authenticated = False
                if await self.login():
                    response = await self.client.post(url, data=data)
                    if response.status_code in (200, 202):
                        logging.info("Torrent successfully added after re-authentication.")
                        return "added"
                    elif response.status_code == 409:
                        logging.info("Torrent is already present in qBittorrent download list.")
                        return "exists"
            logging.error(f"Failed to add torrent to qBittorrent: Status {response.status_code}, Response: {response.text}")
            return False
        except Exception as e:
            logging.error(f"Exception raised when adding torrent to qBittorrent: {e}")
            return False

    async def close(self):
        await self.client.aclose()


# --- Jackett Search Client ---
async def fetch_single_jackett_query(client, url, q):
    try:
        params = {"apikey": JACKETT_API_KEY, "Query": q}
        resp = await client.get(url, params=params, timeout=75.0)
        if resp.status_code == 200:
            res = resp.json().get("Results", [])
            logging.info(f"Jackett query '{q}' returned {len(res)} results.")
            return res
        else:
            logging.error(f"Jackett returned status {resp.status_code} for query '{q}': {resp.text[:200]}")
    except httpx.ReadTimeout:
        logging.warning(f"Jackett query for '{q}' timed out after 75s (some slow indexers or challenges may have delayed response).")
    except Exception as e:
        logging.error(f"Error querying Jackett for '{q}': {repr(e)}")
    return []

async def search_jackett(movie_name: str, quality: str = None) -> list:
    """
    Search Jackett for torrents matching movie_name and quality.
    """
    if not JACKETT_API_KEY:
        logging.warning("JACKETT_API_KEY is not set. Skipping Jackett search.")
        return []

    url = f"{JACKETT_URL}/api/v2.0/indexers/all/results"
    base_query = f"{movie_name} {quality}" if quality else movie_name

    async with httpx.AsyncClient(timeout=80.0) as client:
        results = await fetch_single_jackett_query(client, url, base_query)
        
        # If quality was specified but returned no results, retry with just movie_name
        if not results and quality:
            logging.info(f"No results for '{base_query}'. Retrying Jackett search for base title '{movie_name}'...")
            results = await fetch_single_jackett_query(client, url, movie_name)

    logging.info(f"Total Jackett results for '{movie_name}': {len(results)}")
    return results


# --- DuckDuckGo Spelling Suggestions ---
async def get_spelling_suggestion(query: str) -> str:
    """
    Fetch autocomplete spelling suggestions using DuckDuckGo keyless API.
    """
    # Suggest spelling for the movie name portion specifically
    search_query = query.split(":::")[0].strip()
    url = "https://ac.duckduckgo.com/ac/"
    params = {"q": search_query, "type": "json"}
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            response = await client.get(url, params=params)
            if response.status_code == 200:
                suggestions = response.json()
                if suggestions and isinstance(suggestions, list):
                    # The first suggestion's phrase
                    first_phrase = suggestions[0].get("phrase", "")
                    if first_phrase.lower() != search_query.lower():
                        return first_phrase
    except Exception as e:
        logging.error(f"Failed to fetch spelling suggestion from DuckDuckGo: {e}")
    return None


# --- Telethon Channel Downloader Fallback ---
async def search_and_download_telegram(client: TelegramClient, movie_name: str, quality: str = None) -> str:
    """
    Searches preconfigured Telegram channels for the movie.
    If matching media document is found, downloads it to DOWNLOAD_DIR.
    """
    if not TELEGRAM_CHANNELS:
        logging.warning("No fallback Telegram channels configured. Skipping Telegram search.")
        return None

    words = [w.strip().lower() for w in movie_name.split() if w.strip()]
    if not words:
        return None

    logging.info(f"Starting Telegram search fallback for '{movie_name}' across {len(TELEGRAM_CHANNELS)} channels...")
    
    for channel in TELEGRAM_CHANNELS:
        try:
            logging.info(f"Resolving channel entity: {channel}")
            channel_entity = await client.get_entity(channel)
        except Exception as e:
            logging.warning(f"Could not resolve channel {channel}: {e}. Skipping...")
            continue

        logging.info(f"Searching channel {channel_entity.title if hasattr(channel_entity, 'title') else channel} for '{movie_name}'...")
        
        # Iterate messages matching movie name (server-side search)
        try:
            async for message in client.iter_messages(channel_entity, search=movie_name, limit=50):
                if not message.file:
                    continue

                filename = message.file.name or ""
                filename_lower = filename.lower()
                caption = (message.message or "").lower()

                # Verify all keywords from the movie title match the file name
                if not all(word in filename_lower for word in words):
                    continue

                # If quality is specified, ensure it is in the filename or post caption
                if quality:
                    quality_lower = quality.lower()
                    if quality_lower not in filename_lower and quality_lower not in caption:
                        continue

                # Found a matching media item!
                logging.info(f"Match found in channel: {filename}")
                os.makedirs(DOWNLOAD_DIR, exist_ok=True)
                download_path = os.path.join(DOWNLOAD_DIR, filename)

                # Track download progress
                last_logged_percentage = 0.0
                def progress_callback(received, total):
                    nonlocal last_logged_percentage
                    if not total:
                        return
                    pct = (received / total) * 100
                    if pct - last_logged_percentage >= 10.0 or received == total:
                        logging.info(f"Telegram Download Progress for {filename}: {pct:.1f}% ({received}/{total} bytes)")
                        last_logged_percentage = pct

                logging.info(f"Starting direct Telegram media download to {download_path}...")
                await client.download_media(message, file=download_path, progress_callback=progress_callback)
                logging.info(f"Finished downloading Telegram media file to {download_path}")
                return download_path

        except Exception as e:
            logging.error(f"Error during search/download on channel {channel}: {e}")
            continue

    logging.info("Telegram fallback search complete. No matches found.")
    return None


# --- Telegram Command Listener Event Handler Setup ---
# --- Telegram Command Listener Event Handler Setup ---
# --- Telegram Command Listener Event Handler Setup ---
def setup_handlers(client: TelegramClient, me_id: int = None):

    async def schedule_auto_delete(message, delay_seconds=60):
        await asyncio.sleep(delay_seconds)
        try:
            await message.delete()
        except Exception:
            pass

    def render_page_content(results, movie_name, page_num=0, items_per_page=5):
        total_items = len(results)
        total_pages = (total_items + items_per_page - 1) // items_per_page
        page_num = max(0, min(page_num, total_pages - 1))
        
        start_idx = page_num * items_per_page
        end_idx = min(start_idx + items_per_page, total_items)
        page_items = results[start_idx:end_idx]
        
        msg_lines = [f"🎬 **Results for '{movie_name}' (Page {page_num + 1}/{total_pages}):**\n"]
        buttons = []
        
        for i, t in enumerate(page_items):
            item_idx = start_idx + i
            title = t.get("Title", "Unknown")
            seeders = t.get("Seeders", 0)
            size_str = format_size(t.get("Size", 0))
            display_num = item_idx + 1
            
            msg_lines.append(f"**{display_num}.** `{title}`\n   📊 {seeders} seeders | 💾 {size_str}\n")
            btn_label = f"{display_num}. {title[:32]}..." if len(title) > 35 else f"{display_num}. {title}"
            buttons.append([Button.inline(btn_label, data=f"sel_{item_idx}".encode('utf-8'))])
            
        nav_row = []
        if page_num > 0:
            nav_row.append(Button.inline("⏪ Prev", data=f"page_{page_num - 1}".encode('utf-8')))
        nav_row.append(Button.inline("❌ Cancel", data=b"cancel"))
        if page_num < total_pages - 1:
            nav_row.append(Button.inline("Next ⏩", data=f"page_{page_num + 1}".encode('utf-8')))
            
        buttons.append(nav_row)
        msg_lines.append(f"👇 **Reply with a number (1-{total_items}), or type:**")
        msg_lines.append("• **`next`** (or **`n`**) -> Next Page")
        msg_lines.append("• **`prev`** (or **`p`**) -> Previous Page")
        msg_lines.append("• **`cancel`** -> Cancel Search")
        msg_lines.append("\n⏱️ *Menu auto-deletes in 180 seconds.*")
        
        return "\n".join(msg_lines), buttons, page_num

    async def execute_download(reply_msg, torrent):
        magnet_link = torrent.get('MagnetUri') or torrent.get('Link')
        title = torrent.get('Title')
        seeders = torrent.get('Seeders', 0)
        
        await reply_msg.edit(
            f"📥 Selected torrent ({seeders} seeders):\n`{title}`\n\nAdding to qBittorrent...",
            buttons=None
        )
        
        qb = QBittorrentClient(QBITTORRENT_URL, QBITTORRENT_USERNAME, QBITTORRENT_PASSWORD)
        result = await qb.add_torrent(magnet_link, DOWNLOAD_DIR)
        await qb.close()
        
        if result == "added" or result is True:
            done_msg = await reply_msg.edit(
                f"✅ Successfully added torrent to qBittorrent:\n`{title}`\n\nSaving to: `{DOWNLOAD_DIR}`\n\n⏱️ *Message auto-deletes in 15 seconds.*",
                buttons=None
            )
            asyncio.create_task(schedule_auto_delete(done_msg, 15))
        elif result == "exists":
            done_msg = await reply_msg.edit(
                f"ℹ️ Torrent is already present in qBittorrent queue:\n`{title}`\n\nSaving to: `{DOWNLOAD_DIR}`\n\n⏱️ *Message auto-deletes in 15 seconds.*",
                buttons=None
            )
            asyncio.create_task(schedule_auto_delete(done_msg, 15))
        else:
            await reply_msg.edit("⚠️ Failed to push torrent to qBittorrent.", buttons=None)

    @client.on(events.CallbackQuery)
    async def handle_callback(event):
        data = event.data.decode('utf-8')
        chat_id = event.chat_id
        
        if data == "cancel":
            if chat_id in PENDING_SEARCHES:
                timer = PENDING_SEARCHES[chat_id].get("timer")
                if timer:
                    timer.cancel()
                del PENDING_SEARCHES[chat_id]
            msg = await event.edit("❌ Search cancelled.", buttons=None)
            asyncio.create_task(schedule_auto_delete(msg, 5))
            return

        if data.startswith("page_"):
            page_num = int(data.split("_")[1])
            pending = PENDING_SEARCHES.get(chat_id)
            if pending:
                results = pending.get("results", [])
                movie_name = pending.get("movie_name", "")
                text, buttons, new_page = render_page_content(results, movie_name, page_num)
                pending["page"] = new_page
                await event.edit(text, buttons=buttons)
            else:
                msg = await event.edit("⚠️ Selection expired. Please search again.", buttons=None)
                asyncio.create_task(schedule_auto_delete(msg, 5))
            return

        if data.startswith("sel_"):
            idx = int(data.split("_")[1])
            pending = PENDING_SEARCHES.get(chat_id)
            if pending and idx < len(pending.get("results", [])):
                timer = pending.get("timer")
                if timer:
                    timer.cancel()
                selected_torrent = pending["results"][idx]
                reply_msg = pending["reply_msg"]
                del PENDING_SEARCHES[chat_id]
                await execute_download(reply_msg, selected_torrent)
            else:
                msg = await event.edit("⚠️ Selection expired. Please search again.", buttons=None)
                asyncio.create_task(schedule_auto_delete(msg, 5))

    # --- Shared File / Forwarded Media Auto-Downloader ---
    @client.on(events.NewMessage(func=lambda e: e.is_private and bool(e.file)))
    async def handle_media_message(event):
        """Auto-download any file shared/forwarded to Saved Messages."""
        if me_id and event.sender_id != me_id:
            return

        file = event.message.file
        # Derive a sensible filename
        filename = (file.name or "").strip()
        if not filename:
            ext = ""
            if file.mime_type:
                ext_map = {
                    "video/mp4": ".mp4", "video/x-matroska": ".mkv",
                    "video/x-msvideo": ".avi", "video/quicktime": ".mov",
                    "audio/mpeg": ".mp3", "audio/flac": ".flac",
                    "audio/x-wav": ".wav", "application/zip": ".zip",
                    "application/x-rar-compressed": ".rar",
                    "application/pdf": ".pdf",
                }
                ext = ext_map.get(file.mime_type, "")
            filename = f"telegram_{event.message.id}{ext}"

        file_size = file.size or 0
        chat_id = event.chat_id
        started_at = time.monotonic()
        logging.info(f"Auto-download triggered: '{filename}' ({format_size(file_size)})")

        # Register in global tracker so /status can query it
        ACTIVE_DOWNLOADS[chat_id] = {
            "filename": filename,
            "file_size": file_size,
            "received": 0,
            "pct": 0.0,
            "started_at": started_at,
        }

        reply_msg = await event.reply(
            f"📥 **File detected in Saved Messages!**\n"
            f"📄 `{filename}`\n"
            f"💾 Size: {format_size(file_size)}\n"
            f"⏳ Starting download... (send `status` anytime for a progress update)"
        )

        os.makedirs(DOWNLOAD_DIR, exist_ok=True)
        download_path = os.path.join(DOWNLOAD_DIR, filename)

        # --- Resume support ---
        # Telethon's iter_download offset must be aligned to CHUNK_SIZE (512 KB).
        # Any extra bytes beyond the aligned boundary are truncated and re-downloaded.
        CHUNK_SIZE = 512 * 1024  # 512 KB — matches Telethon's default request_size

        existing_bytes = os.path.getsize(download_path) if os.path.exists(download_path) else 0
        resume_offset = (existing_bytes // CHUNK_SIZE) * CHUNK_SIZE  # align down

        if file_size and resume_offset >= file_size:
            # File is already fully present on disk
            ACTIVE_DOWNLOADS.pop(chat_id, None)
            logging.info(f"File already complete on disk: {download_path}")
            done_msg = await reply_msg.edit(
                f"\u2705 **Already downloaded!**\n"
                f"\ud83d\udcc4 `{filename}`\n"
                f"\ud83d\udcbe Size: {format_size(file_size)}\n"
                f"\ud83d\udcc2 Saved to: `{DOWNLOAD_DIR}`\n\n"
                f"\u23f1\ufe0f *This message auto-deletes in 15 seconds.*"
            )
            asyncio.create_task(schedule_auto_delete(done_msg, 15))
            return

        if resume_offset > 0:
            logging.info(
                f"Resuming '{filename}' from {format_size(resume_offset)} "
                f"(had {format_size(existing_bytes)}, aligned to chunk boundary)"
            )
            await reply_msg.edit(
                f"\u23e9 **Resuming download...**\n"
                f"\ud83d\udcc4 `{filename}`\n"
                f"\ud83d\udcbe Already downloaded: {format_size(resume_offset)} / {format_size(file_size)} "
                f"({resume_offset / file_size * 100:.1f}%)\n"
                f"\u23f3 Continuing... (send `status` anytime)"
            )

        # Update tracker with resume starting point
        ACTIVE_DOWNLOADS[chat_id]["received"] = resume_offset
        ACTIVE_DOWNLOADS[chat_id]["pct"] = (resume_offset / file_size * 100) if file_size else 0.0

        # Progress callback — called manually in the iter_download loop below
        last_pct = [ACTIVE_DOWNLOADS[chat_id]["pct"]]
        last_log_ts = [0.0]
        last_edit_ts = [0.0]

        async def progress_callback(received, total):
            if not total:
                return
            pct = (received / total) * 100
            now = time.monotonic()

            # Always keep global tracker up-to-date (used by /status)
            if chat_id in ACTIVE_DOWNLOADS:
                ACTIVE_DOWNLOADS[chat_id]["received"] = received
                ACTIVE_DOWNLOADS[chat_id]["pct"] = pct

            # Log every 5% OR every 30 s — whichever fires first
            should_log = (pct - last_pct[0] >= 5.0 or now - last_log_ts[0] >= 30.0 or received == total)
            # Throttle Telegram message edits to once every 5 s (avoid FloodWait)
            should_edit = should_log and (now - last_edit_ts[0] >= 5.0)

            if should_log:
                last_pct[0] = pct
                last_log_ts[0] = now
                bar_filled = int(pct / 10)
                bar = "\u2588" * bar_filled + "\u2591" * (10 - bar_filled)
                logging.info(
                    f"Telegram DL [{bar}] {pct:.1f}% \u2014 "
                    f"{format_size(received)} / {format_size(file_size)}  '{filename}'"
                )
                if should_edit:
                    last_edit_ts[0] = now
                    resume_note = f" *(resumed from {format_size(resume_offset)})*" if resume_offset > 0 else ""
                    try:
                        await reply_msg.edit(
                            f"\ud83d\udce5 **Downloading from Telegram...**{resume_note}\n"
                            f"\ud83d\udcc4 `{filename}`\n"
                            f"\n[{bar}] {pct:.1f}%\n"
                            f"\ud83d\udcbe {format_size(received)} / {format_size(file_size)}"
                        )
                    except Exception as edit_err:
                        logging.warning(f"Could not update progress message: {edit_err}")

        try:
            # Open file: truncate-to-aligned-boundary if resuming, else create fresh
            if resume_offset > 0:
                with open(download_path, 'r+b') as f:
                    f.seek(resume_offset)
                    f.truncate()
            file_handle = open(download_path, 'ab' if resume_offset > 0 else 'wb')

            received_total = resume_offset
            try:
                async for chunk in client.iter_download(
                    event.message,
                    offset=resume_offset,
                    request_size=CHUNK_SIZE,
                    file_size=file_size,
                ):
                    file_handle.write(chunk)
                    received_total += len(chunk)
                    await progress_callback(received_total, file_size)
            finally:
                file_handle.close()

            ACTIVE_DOWNLOADS.pop(chat_id, None)
            logging.info(f"Successfully downloaded to {download_path}")
            done_msg = await reply_msg.edit(
                f"\u2705 **Download complete!**\n"
                f"\ud83d\udcc4 `{filename}`\n"
                f"\ud83d\udcbe Size: {format_size(file_size)}\n"
                f"\ud83d\udcc2 Saved to: `{DOWNLOAD_DIR}`\n\n"
                f"\u23f1\ufe0f *This message auto-deletes in 15 seconds.*"
            )
            asyncio.create_task(schedule_auto_delete(done_msg, 15))
        except Exception as e:
            ACTIVE_DOWNLOADS.pop(chat_id, None)
            logging.error(f"Error downloading '{filename}': {e}")
            await reply_msg.edit(
                f"\u274c **Download failed!**\n"
                f"\ud83d\udcc4 `{filename}`\n"
                f"Error: `{e}`\n\n"
                f"\ud83d\udca1 Partial file kept at `{download_path}` \u2014 re-forward the file to resume."
            )

    @client.on(events.NewMessage(pattern=r'(?i).+'))
    async def handle_new_message(event):
        if not event.is_private:
            return
            
        if me_id and event.sender_id != me_id:
            return

        text = event.message.text.strip()
        if not text:
            return

        chat_id = event.chat_id

        # 0a. STATUS CHECK — send "status" or "/status" to get live download progress
        if text.lower().strip() in ("status", "/status", "progress", "/progress"):
            if chat_id in ACTIVE_DOWNLOADS:
                dl = ACTIVE_DOWNLOADS[chat_id]
                pct = dl["pct"]
                received = dl["received"]
                file_size = dl["file_size"]
                filename = dl["filename"]
                elapsed = time.monotonic() - dl["started_at"]
                bar_filled = int(pct / 10)
                bar = "█" * bar_filled + "░" * (10 - bar_filled)
                elapsed_str = f"{int(elapsed // 60)}m {int(elapsed % 60)}s"
                # Estimate remaining time
                if received > 0 and file_size > received:
                    speed = received / elapsed  # bytes/s
                    remaining_bytes = file_size - received
                    eta_s = remaining_bytes / speed
                    eta_str = f"{int(eta_s // 60)}m {int(eta_s % 60)}s"
                else:
                    eta_str = "calculating..."
                status_msg = await event.reply(
                    f"📊 **Download Status**\n"
                    f"📄 `{filename}`\n\n"
                    f"[{bar}] **{pct:.1f}%**\n"
                    f"💾 {format_size(received)} / {format_size(file_size)}\n"
                    f"⏱️ Elapsed: {elapsed_str}\n"
                    f"⏳ ETA: ~{eta_str}"
                )
                asyncio.create_task(schedule_auto_delete(status_msg, 30))
            else:
                status_msg = await event.reply("✅ No active downloads right now.")
                asyncio.create_task(schedule_auto_delete(status_msg, 10))
            try:
                await event.delete()
            except Exception:
                pass
            return

        # 0b. DIRECT MAGNET LINK SUPPORT: e.g. "/magnet ::: magnet:?xt=..." or "magnet:?xt=..."
        if "magnet:?xt=" in text or text.startswith("/magnet"):
            magnet_link = text
            if ":::" in text:
                magnet_link = text.split(":::", 1)[1].strip()
            
            magnet_link = re.sub(r'^/magnet\s*', '', magnet_link).strip()
            
            reply_msg = await event.reply("📥 Adding magnet link directly to qBittorrent...")
            qb = QBittorrentClient(QBITTORRENT_URL, QBITTORRENT_USERNAME, QBITTORRENT_PASSWORD)
            result = await qb.add_torrent(magnet_link, DOWNLOAD_DIR)
            await qb.close()
            
            if result == "added" or result is True:
                done_msg = await reply_msg.edit(
                    f"✅ Magnet link successfully added to qBittorrent!\nSaving to: `{DOWNLOAD_DIR}`\n\n⏱️ *Message auto-deletes in 15 seconds.*"
                )
                asyncio.create_task(schedule_auto_delete(done_msg, 15))
            elif result == "exists":
                done_msg = await reply_msg.edit(
                    f"ℹ️ Magnet link is already present in qBittorrent queue!\nSaving to: `{DOWNLOAD_DIR}`\n\n⏱️ *Message auto-deletes in 15 seconds.*"
                )
                asyncio.create_task(schedule_auto_delete(done_msg, 15))
            else:
                await reply_msg.edit("❌ Failed to add magnet link to qBittorrent.")
            return

        # 1. PENDING SEARCH INTERACTIVE COMMANDS (Number selection, Next, Prev, Cancel)
        if chat_id in PENDING_SEARCHES:
            t_low = text.lower()
            pending = PENDING_SEARCHES[chat_id]
            results = pending.get("results", [])
            movie_name = pending.get("movie_name", "")
            current_page = pending.get("page", 0)
            reply_msg = pending.get("reply_msg")

            if t_low in ("next", "n", ">", ">>"):
                text_c, buttons_c, new_p = render_page_content(results, movie_name, current_page + 1)
                pending["page"] = new_p
                await reply_msg.edit(text_c, buttons=buttons_c)
                try:
                    await event.delete()
                except Exception:
                    pass
                return

            if t_low in ("prev", "p", "<", "<<"):
                text_c, buttons_c, new_p = render_page_content(results, movie_name, current_page - 1)
                pending["page"] = new_p
                await reply_msg.edit(text_c, buttons=buttons_c)
                try:
                    await event.delete()
                except Exception:
                    pass
                return

            if t_low in ("cancel", "c", "exit", "stop"):
                timer = pending.get("timer")
                if timer:
                    timer.cancel()
                del PENDING_SEARCHES[chat_id]
                msg = await reply_msg.edit("❌ Search cancelled.", buttons=None)
                asyncio.create_task(schedule_auto_delete(msg, 5))
                try:
                    await event.delete()
                except Exception:
                    pass
                return

            if text.isdigit():
                idx = int(text) - 1
                if 0 <= idx < len(results):
                    timer = pending.get("timer")
                    if timer:
                        timer.cancel()
                    selected_torrent = results[idx]
                    del PENDING_SEARCHES[chat_id]
                    try:
                        await event.delete()
                    except Exception:
                        pass
                    await execute_download(reply_msg, selected_torrent)
                    return

        # Require ::: format to start a new search
        if ":::" not in text:
            return

        # Parse "Movie Name ::: Quality" or "Movie Name ::: search"
        parts = text.split(":::", 1)
        movie_name = parts[0].strip()
        raw_quality = parts[1].strip() if len(parts) > 1 else None
        
        quality = None
        if raw_quality and raw_quality.lower() not in ("search", "all", "any", "full", "none", ""):
            quality = raw_quality

        reply_msg = await event.reply(
            f"🔍 Processing query: **{movie_name}**" + (f" (Quality: {quality})" if quality else "") + "..."
        )
        
        try:
            # 1. Search Jackett
            await reply_msg.edit(f"🔍 Searching Jackett for torrents...")
            torrent_results = await search_jackett(movie_name, quality)
            
            # Filter and sort results by seeders
            valid_results = [r for r in torrent_results if r.get('Seeders', 0) > 0]
            valid_results.sort(key=lambda x: x.get('Seeders', 0), reverse=True)
            
            # Deduplicate exact duplicate links while retaining all distinct releases/parts
            distinct_results = []
            seen_links = set()
            for r in valid_results:
                link = r.get("MagnetUri") or r.get("Link") or r.get("Title")
                if link not in seen_links:
                    seen_links.add(link)
                    distinct_results.append(r)

            top_results = distinct_results if distinct_results else valid_results

            if len(top_results) == 1:
                # Only 1 match found - download immediately
                await execute_download(reply_msg, top_results[0])
                return
            elif len(top_results) > 1:
                # Multiple matches found - render Paginated Menu with Next/Prev
                text_content, buttons, current_page = render_page_content(top_results, movie_name, page_num=0)
                
                async def auto_delete_task():
                    await asyncio.sleep(180)
                    if chat_id in PENDING_SEARCHES and PENDING_SEARCHES[chat_id]["reply_msg"].id == reply_msg.id:
                        del PENDING_SEARCHES[chat_id]
                        try:
                            await reply_msg.delete()
                        except Exception:
                            pass

                PENDING_SEARCHES[chat_id] = {
                    "results": top_results,
                    "movie_name": movie_name,
                    "page": current_page,
                    "reply_msg": reply_msg,
                    "timer": asyncio.create_task(auto_delete_task())
                }
                
                await reply_msg.edit(
                    text_content,
                    buttons=buttons
                )
                return

            # 2. Telegram Fallback Search
            await reply_msg.edit(f"⚠️ Torrent not found or low seeders. Searching Telegram fallback channels...")
            downloaded_file = await search_and_download_telegram(client, movie_name, quality)
            
            if downloaded_file:
                filename = os.path.basename(downloaded_file)
                done_msg = await reply_msg.edit(
                    f"✅ Successfully downloaded via Telegram fallback:\n`{filename}`\n\nSaved to: `{DOWNLOAD_DIR}`\n\n⏱️ *Message auto-deletes in 15 seconds.*",
                    buttons=None
                )
                asyncio.create_task(schedule_auto_delete(done_msg, 15))
                return

            # 3. Spelling Suggestion Fallback
            await reply_msg.edit("🔍 Searching for spelling suggestions...")
            suggestion = await get_spelling_suggestion(movie_name)
            
            if suggestion:
                err_msg = await reply_msg.edit(
                    f"❌ No matches found for **{movie_name}**.\n\nDid you mean: **{suggestion}**?\n"
                    f"Please try searching again with the corrected name.\n\n⏱️ *Message auto-deletes in 30 seconds.*",
                    buttons=None
                )
                asyncio.create_task(schedule_auto_delete(err_msg, 30))
            else:
                err_msg = await reply_msg.edit(
                    f"❌ No matches found for **{movie_name}** in Jackett or preconfigured Telegram channels.\n\n⏱️ *Message auto-deletes in 30 seconds.*",
                    buttons=None
                )
                asyncio.create_task(schedule_auto_delete(err_msg, 30))

        except Exception as e:
            logging.exception("Error during command execution:")
            await reply_msg.edit(f"❌ An unexpected error occurred: {e}", buttons=None)


async def main():
    logging.info("Starting Hybrid Media Downloader Bot...")
    
    # Initialize the client. The session file will be written locally.
    client = TelegramClient('media_downloader_session', TELEGRAM_API_ID, TELEGRAM_API_HASH)
    
    if TELEGRAM_BOT_TOKEN:
        logging.info("Logging in using provided Bot Token...")
        await client.start(bot_token=TELEGRAM_BOT_TOKEN)
    else:
        logging.info("Logging in using User Account...")
        await client.start()
        
    me = await client.get_me()
    logging.info(f"Bot started successfully as: {me.first_name} (@{me.username if me.username else ''})")
    
    # Setup message handlers restricted to account owner
    setup_handlers(client, me.id)
    
    # Run the client until disconnected
    await client.run_until_disconnected()

if __name__ == '__main__':
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logging.info("Application stopped by user.")
