import os
import sys
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
                return True
            elif response.status_code in (403, 409):
                logging.warning("qBittorrent returned authorization error. Re-authenticating...")
                self.authenticated = False
                if await self.login():
                    response = await self.client.post(url, data=data)
                    if response.status_code in (200, 202):
                        logging.info("Torrent successfully added after re-authentication.")
                        return True
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
        resp = await client.get(url, params=params, timeout=12.0)
        if resp.status_code == 200:
            res = resp.json().get("Results", [])
            logging.info(f"Jackett query '{q}' returned {len(res)} results.")
            return res
    except Exception as e:
        logging.error(f"Error querying Jackett for '{q}': {e}")
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
    queries = [base_query]
    
    # If query is a general base title (e.g. 'Hotel Transylvania'), also search parts 1, 2, 3, 4 in parallel
    if not re.search(r'\b(1|2|3|4|5|6|7|8|9|part|vol|chapter)\b', movie_name.lower()):
        for sp in ["1", "2", "3", "4"]:
            queries.append(f"{movie_name} {sp} {quality}" if quality else f"{movie_name} {sp}")

    all_results = []
    async with httpx.AsyncClient(timeout=20.0) as client:
        tasks = [fetch_single_jackett_query(client, url, q) for q in queries]
        responses = await asyncio.gather(*tasks)
        for r in responses:
            all_results.extend(r)

    logging.info(f"Total combined Jackett results: {len(all_results)}")
    return all_results


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
        success = await qb.add_torrent(magnet_link, DOWNLOAD_DIR)
        await qb.close()
        
        if success:
            done_msg = await reply_msg.edit(
                f"✅ Successfully added torrent to qBittorrent:\n`{title}`\n\nSaving to: `{DOWNLOAD_DIR}`\n\n⏱️ *Message auto-deletes in 15 seconds.*",
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

        # 0. DIRECT MAGNET LINK SUPPORT: e.g. "/magnet ::: magnet:?xt=..." or "magnet:?xt=..."
        if "magnet:?xt=" in text or text.startswith("/magnet"):
            magnet_link = text
            if ":::" in text:
                magnet_link = text.split(":::", 1)[1].strip()
            
            magnet_link = re.sub(r'^/magnet\s*', '', magnet_link).strip()
            
            reply_msg = await event.reply("📥 Adding magnet link directly to qBittorrent...")
            qb = QBittorrentClient(QBITTORRENT_URL, QBITTORRENT_USERNAME, QBITTORRENT_PASSWORD)
            success = await qb.add_torrent(magnet_link, DOWNLOAD_DIR)
            await qb.close()
            
            if success:
                done_msg = await reply_msg.edit(
                    f"✅ Magnet link successfully added to qBittorrent!\nSaving to: `{DOWNLOAD_DIR}`\n\n⏱️ *Message auto-deletes in 15 seconds.*"
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
