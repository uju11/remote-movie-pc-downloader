import os
import asyncio
import logging
import sys
from dotenv import load_dotenv

# Load environment variables
load_dotenv()

# Setup logging to console
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(message)s',
    handlers=[logging.StreamHandler(sys.stdout)]
)

# Import functions from bot.py
# Add parent dir to path if necessary
sys.path.append(os.path.dirname(os.path.abspath(__file__)))
try:
    from bot import search_jackett, get_spelling_suggestion, QBittorrentClient, parse_channels
except ImportError as e:
    logging.error(f"Failed to import from bot.py: {e}")
    sys.exit(1)

async def test_integration():
    logging.info("==============================================")
    logging.info("Starting Automated Test for Downloader Bot")
    logging.info("==============================================")

    # 1. Test Spelling Suggestion Engine
    misspelled = "hotl transilvania"
    logging.info(f"Testing Spelling Suggestion with query: '{misspelled}'")
    suggestion = await get_spelling_suggestion(misspelled)
    if suggestion:
        logging.info(f"✅ Success! Suggestion for '{misspelled}' is: '{suggestion}'")
    else:
        logging.warning("⚠️ Spelling Suggestion returned no results.")

    # 2. Test Jackett Search for "Hotel Transylvania"
    target_movie = "Hotel Transylvania"
    logging.info(f"Testing Jackett Search for: '{target_movie}'")
    
    jackett_url = os.getenv("JACKETT_URL")
    jackett_api_key = os.getenv("JACKETT_API_KEY")
    logging.info(f"Jackett URL: {jackett_url}")
    logging.info(f"Jackett API Key: {'Set' if jackett_api_key else 'Not Set'}")

    results = []
    if jackett_api_key:
        results = await search_jackett(target_movie)
        if results:
            logging.info(f"✅ Success! Jackett returned {len(results)} torrents.")
            # Print top 3 torrent results
            for idx, r in enumerate(results[:3]):
                title = r.get("Title")
                seeders = r.get("Seeders", 0)
                peers = r.get("Peers", 0)
                logging.info(f"  [{idx+1}] {title} | Seeders: {seeders} | Peers: {peers}")
        else:
            logging.warning("⚠️ Jackett returned 0 results. Check your indexers and Jackett settings.")
    else:
        logging.warning("⚠️ Skipping Jackett Search test because JACKETT_API_KEY is not configured.")

    # 3. Evaluate Torrent Results
    best_torrent = None
    if results:
        results.sort(key=lambda x: x.get('Seeders', 0), reverse=True)
        top_result = results[0]
        seeders = top_result.get('Seeders', 0)
        logging.info(f"Evaluating top result: '{top_result.get('Title')}' with {seeders} seeders.")
        if seeders > 10:
            best_torrent = top_result
            logging.info("✅ Top result meets seeder threshold (>10). Would proceed to qBittorrent.")
        else:
            logging.info("⚠️ Seeders count is <= 10. Would fall back to Telegram channel search.")
    else:
        logging.info("No torrents found. Would fall back to Telegram channel search.")

    # 4. Test qBittorrent connection
    qb_url = os.getenv("QBITTORRENT_URL")
    qb_user = os.getenv("QBITTORRENT_USERNAME")
    qb_pwd = os.getenv("QBITTORRENT_PASSWORD")
    download_dir = os.getenv("DOWNLOAD_DIR", "U:\\movies")

    logging.info(f"Testing qBittorrent Login at: {qb_url} (User: {qb_user})")
    qb_client = QBittorrentClient(qb_url, qb_user, qb_pwd)
    qb_connected = await qb_client.login()
    if qb_connected:
        logging.info("✅ Success! Authenticated with qBittorrent Web UI.")
        
        # If we have a healthy torrent, let's try to add it as a test (optional)
        if best_torrent:
            magnet_link = best_torrent.get('MagnetUri') or best_torrent.get('Link')
            logging.info(f"Attempting to add '{target_movie}' torrent to qBittorrent...")
            # We can perform a dry-run or attempt to add it. Let's attempt adding it!
            add_success = await qb_client.add_torrent(magnet_link, download_dir)
            if add_success:
                logging.info(f"✅ Success! Torrent added to qBittorrent to download to {download_dir}.")
            else:
                logging.error("❌ Failed to add torrent to qBittorrent.")
        await qb_client.close()
    else:
        logging.warning("⚠️ Could not connect or authenticate to qBittorrent. Please ensure qBittorrent is running and credentials are correct.")

    # 5. Check Telegram fallback configurations
    telegram_channels_str = os.getenv("TELEGRAM_CHANNELS", "")
    parsed_channels = parse_channels(telegram_channels_str)
    logging.info(f"Preconfigured Telegram channels: {parsed_channels}")
    if not parsed_channels:
        logging.warning("⚠️ No fallback Telegram channels are configured.")
    else:
        logging.info(f"Telegram channels verified. Found {len(parsed_channels)} target channels.")

    logging.info("==============================================")
    logging.info("Test Run Complete!")
    logging.info("==============================================")

if __name__ == "__main__":
    asyncio.run(test_integration())
