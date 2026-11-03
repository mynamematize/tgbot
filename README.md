# FREE STARS Telegram bot

## Files
- `main.py` — bot source code
- `requirements.txt` — Python dependencies
- `Procfile` — worker start command
- `.env.example` — environment variable names (no secrets)

## Deploy to Render
1. Create a new **GitHub repository** and upload these files to its root.
2. In Render, create a **Background Worker** connected to that new repository.
3. Build command: `pip install -r requirements.txt`
4. Start command: `python main.py`
5. Add these Environment Variables in Render:
   - `BOT_TOKEN` — token from BotFather
   - `ADMIN_ID` — your numeric Telegram user ID
   - `OCR_SPACE_API_KEY` — your OCR.Space API key
6. Deploy the worker.

Do not upload `.env`, bot tokens, API keys, or database files to GitHub.

Important: run only one active polling worker with this bot token. Stop/suspend old deployments before starting the new worker, otherwise Telegram may return `Conflict: terminated by other getUpdates request`.
